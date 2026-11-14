"""Tier 0: the submitted URL itself is a media file or manifest."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from ..config import Settings
from ..errors import (
    AppError,
    DRMProtected,
    DurationTooLong,
    FileTooLarge,
    LoginRequired,
)
from ..http_utils import base_headers, fetch_limited
from ..security import extension_for_content_type, looks_like_media_url
from .drm import ManifestInfo, apply_manifest_info, scan_manifest
from .media_probe import probe_media
from .sources import Source, human_bytes


async def extract(client: httpx.AsyncClient, job, settings: Settings) -> list[Source]:
    job.begin_tier(0)
    url = job.url
    headers = base_headers(settings.user_agent)
    cookie = job.cookie_header_for(url)
    if cookie:
        headers["Cookie"] = cookie

    job.log(f"Probing the response headers of {url}", tier=0)
    try:
        probe = await probe_media(client, url, headers, settings)
    except AppError as exc:
        job.log(f"Probe failed: {exc.message}", level="warning", tier=0)
        # A direct media URL with a hard access error will not be rescued by later tiers.
        if exc.code in {"blocked_url"}:
            raise
        return []
    except httpx.HTTPError as exc:
        job.log(f"Probe failed: {exc}", level="warning", tier=0)
        return []

    if probe.status in {401, 402}:
        raise LoginRequired()
    if probe.status == 451:
        from ..errors import GeoBlocked

        raise GeoBlocked()
    if probe.status in {403, 429}:
        job.log(f"The server returned HTTP {probe.status}; later tiers may still succeed.", level="warning", tier=0)
        if looks_like_media_url(url):
            raise LoginRequired(
                f"The media server refused the request (HTTP {probe.status}).",
                hint="The file may require cookies, a fresh referer, or a different region. Attach cookies.txt for your own account if applicable.",
            )

    kind = probe.kind
    if kind is None:
        job.log("This URL is not direct media. Moving to Tier 1.", tier=0)
        return []

    if kind in {"hls", "dash"}:
        manifest_headers = base_headers(settings.user_agent)
        if cookie:
            manifest_headers["Cookie"] = cookie
        try:
            manifest = await fetch_limited(
                client,
                "GET",
                probe.url,
                headers=manifest_headers,
                max_bytes=2 * 1024 * 1024,
                allow_private=settings.allow_private_hosts,
            )
        except AppError as exc:
            if exc.code in {"blocked_url"}:
                raise
            job.log(f"Could not read the manifest yet: {exc.message}", level="warning", tier=0)
            manifest = None

        info = ManifestInfo(drm=False)
        if manifest is not None:
            if manifest.status in {401, 402}:
                raise LoginRequired()
            if manifest.status == 451:
                from ..errors import GeoBlocked

                raise GeoBlocked()
            if manifest.status < 400:
                info = scan_manifest(manifest.url, manifest.text)
                if info.drm:
                    job.log(info.reason or "The manifest declares DRM.", level="warning", tier=0)
                    raise DRMProtected()
                if info.duration and info.duration > settings.max_duration_seconds:
                    raise DurationTooLong(
                        f"That video is {info.duration / 60:.0f} minutes long, above the configured {settings.max_duration_seconds // 60}-minute limit."
                    )
            else:
                job.log(f"Manifest returned HTTP {manifest.status}.", level="warning", tier=0)

        label = "HLS · adaptive" if kind == "hls" else "DASH · adaptive"
        source = Source(
            url=probe.url,
            kind=kind,
            tier=0,
            label=label,
            method="ytdlp",
            quality="",
            ext="mp4",
            filesize=None,
            headers={},
            page_url=job.url,
            duration=info.duration,
        )
        apply_manifest_info(source, info)
        if source.duration and source.duration > settings.max_duration_seconds:
            raise DurationTooLong()
        job.set_title(_title_from_url(url))
        job.log(
            f"Found a {kind.upper()} manifest ({source.quality or 'adaptive'}).",
            level="success",
            tier=0,
        )
        return [source]

    if probe.status == 200 and probe.content_type.startswith("text/html"):
        job.log("The server returned an HTML page, not a video.", tier=0)
        return []

    if probe.filesize and probe.filesize > settings.max_file_size_bytes:
        raise FileTooLarge(
            f"That file is {human_bytes(probe.filesize)}, above the configured {settings.max_file_size_mb} MB limit."
        )

    ext = extension_for_content_type(probe.content_type, probe.url)
    if ext == ".m3u8":
        # Content-type said HLS even though the URL had no manifest suffix.
        return await _manifest_fallback(client, job, settings, probe.url, headers)

    quality_match = re.search(r"(2160|1440|1080|720|480|360|240)p", url, re.IGNORECASE)
    quality = f"{int(quality_match.group(1))}p" if quality_match else ""
    label = f"Progressive {ext.lstrip('.').upper()} file"
    if quality:
        label += f" · {quality}"
    source = Source(
        url=probe.url,
        kind="direct",
        tier=0,
        label=label,
        method="direct",
        quality=quality,
        height=int(quality_match.group(1)) if quality_match else None,
        ext=ext,
        filesize=probe.filesize,
        headers={},
        page_url=job.url,
    )
    job.set_title(_title_from_url(url))
    job.log(
        f"Found a direct media file ({probe.content_type or 'unknown type'}, {human_bytes(probe.filesize)}).",
        level="success",
        tier=0,
    )
    return [source]


async def _manifest_fallback(client, job, settings: Settings, url: str, headers: dict[str, str]) -> list[Source]:
    result = await fetch_limited(
        client,
        "GET",
        url,
        headers=headers,
        max_bytes=2 * 1024 * 1024,
        allow_private=settings.allow_private_hosts,
    )
    info = scan_manifest(result.url, result.text)
    if info.drm:
        raise DRMProtected()
    source = Source(
        url=url,
        kind="hls",
        tier=0,
        label="HLS · adaptive",
        method="ytdlp",
        ext="mp4",
        page_url=job.url,
        duration=info.duration,
    )
    apply_manifest_info(source, info)
    job.set_title(_title_from_url(url))
    job.log("Found an HLS stream.", level="success", tier=0)
    return [source]


def _title_from_url(url: str) -> str:
    try:
        stem = Path(unquote(urlsplit(url).path)).stem
    except Exception:
        stem = ""
    return stem[:160] or "video"
