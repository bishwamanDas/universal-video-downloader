"""Tier 2: static HTML parsing.

Looks at video/source tags, OpenGraph/Twitter metadata, JSON-LD VideoObject
nodes, one level of iframes, and media URLs embedded in inline scripts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..config import Settings
from ..errors import (
    AppError,
    CaptchaBlocked,
    DRMProtected,
    DurationTooLong,
    FileTooLarge,
    LoginRequired,
)
from ..http_utils import base_headers, fetch_limited, origin_for
from ..security import (
    extension_for_content_type,
    looks_like_media_url,
    media_kind_from_url,
)
from .drm import apply_manifest_info, scan_manifest
from .media_probe import probe_media
from .sources import Source, human_bytes, rank_sources

_ABS_MEDIA_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>\\]+?\.(?:m3u8|mpd|mp4|webm|mov|m4v)(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)
_REL_MEDIA_RE = re.compile(
    r"""(?<![\w:/])/[^\s"'<>\\]+?\.(?:m3u8|mpd|mp4|webm|mov|m4v)(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)
_RESOLUTION_RE = re.compile(r"(2160|1440|1080|720|480|360|240)p", re.IGNORECASE)
_META_VIDEO_KEYS = {
    "og:video",
    "og:video:url",
    "og:video:secure_url",
    "twitter:player:stream",
}
_EMBED_META_KEYS = {"og:video:url", "og:video:secure_url", "twitter:player"}
_LOGIN_MARKERS = (
    "sign in to continue",
    "sign in to watch",
    "log in to watch",
    "login required",
    "this video is private",
    "members only",
)
_CAPTCHA_MARKERS = ("recaptcha", "hcaptcha", "captcha", "verify you are human", "cf-challenge")


@dataclass
class Candidate:
    url: str
    page_url: str
    trusted: bool
    note: str = ""


async def extract(client: httpx.AsyncClient, job, settings: Settings) -> list[Source]:
    job.begin_tier(2)
    page_headers = base_headers(
        settings.user_agent,
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    )
    cookie = job.cookie_header_for(job.url)
    if cookie:
        page_headers["Cookie"] = cookie

    job.log("Fetching the page HTML without JavaScript.", tier=2)
    try:
        page = await fetch_limited(
            client,
            "GET",
            job.url,
            headers=page_headers,
            max_bytes=settings.max_page_bytes,
            allow_private=settings.allow_private_hosts,
        )
    except AppError as exc:
        if exc.code == "blocked_url":
            raise
        job.log(f"Page fetch failed: {exc.message}", level="warning", tier=2)
        return []
    except httpx.HTTPError as exc:
        job.log(f"Page fetch failed: {exc}", level="warning", tier=2)
        return []

    if page.status in {401, 402}:
        raise LoginRequired()
    if page.status == 451:
        from ..errors import GeoBlocked

        raise GeoBlocked()
    if page.status in {403, 429}:
        job.flags.add("blocked")
        job.log(f"The page returned HTTP {page.status}; it may be bot-protected.", level="warning", tier=2)
        return []
    if page.status >= 400:
        job.log(f"The page returned HTTP {page.status}.", level="warning", tier=2)
        return []

    content_type = page.content_type
    if "html" not in content_type and not page.body.lstrip().startswith(b"<"):
        job.log("The response is not HTML; Tier 2 has nothing to parse.", tier=2)
        return []

    html_text = page.text
    soup = BeautifulSoup(html_text, "html.parser")
    page_url = page.url
    title = _extract_title(soup)
    if title:
        job.set_title(title)

    body_low = html_text[:300000].lower()
    if any(marker in body_low for marker in _CAPTCHA_MARKERS):
        job.flags.add("captcha")
        raise CaptchaBlocked()
    if any(marker in body_low for marker in _LOGIN_MARKERS):
        job.flags.add("login")

    candidates, iframes = _candidates_from_html(html_text, soup, page_url)
    job.log(
        f"Static HTML yielded {len(candidates)} raw media hint(s) and {len(iframes)} iframe(s).",
        tier=2,
    )

    for iframe_url in iframes[:4]:
        if iframe_url == page_url:
            continue
        try:
            frame = await fetch_limited(
                client,
                "GET",
                iframe_url,
                headers=_media_headers(settings, iframe_url, page_url),
                max_bytes=settings.max_page_bytes,
                allow_private=settings.allow_private_hosts,
            )
        except AppError as exc:
            if exc.code == "blocked_url":
                job.log("Skipped an iframe pointing at a private address.", level="warning", tier=2)
            continue
        if frame.status >= 400 or "html" not in frame.content_type:
            continue
        frame_soup = BeautifulSoup(frame.text, "html.parser")
        nested, _ = _candidates_from_html(frame.text, frame_soup, frame.url, include_iframes=False)
        for candidate in nested:
            # The referer the player expects is the iframe document, not the top page.
            candidate.page_url = frame.url
            candidates.append(candidate)
        if title is None:
            title = _extract_title(frame_soup)
            if title:
                job.set_title(title)

    # Deduplicate while preferring trusted hints from real tags/meta.
    deduped: dict[str, Candidate] = {}
    for candidate in candidates:
        if not candidate.url.lower().startswith(("http://", "https://")):
            continue
        existing = deduped.get(candidate.url)
        if existing is None or (candidate.trusted and not existing.trusted):
            deduped[candidate.url] = candidate
    ordered = sorted(
        deduped.values(),
        key=lambda c: (0 if c.trusted or media_kind_from_url(c.url) in {"hls", "dash"} else 1),
    )

    sources: list[Source] = []
    drm_reasons: list[str] = []
    too_large: list[int] = []
    probes = 0
    manifest_budget = 6
    for candidate in ordered[:24]:
        if probes >= 10 and manifest_budget <= 0:
            break
        url = candidate.url
        page_ref = candidate.page_url
        headers = _media_headers(settings, url, page_ref)
        cookie_header = job.cookie_header_for(url)
        if cookie_header:
            headers["Cookie"] = cookie_header
        kind = media_kind_from_url(url)

        if kind not in {"hls", "dash"}:
            if probes >= 10:
                continue
            probes += 1
            try:
                probe = await probe_media(client, url, headers, settings)
            except AppError as exc:
                if exc.code == "blocked_url":
                    job.log("Skipped a media URL pointing at a private address.", level="warning", tier=2)
                    continue
                if candidate.trusted and looks_like_media_url(url):
                    sources.append(
                        _direct_source(
                            url,
                            page_ref,
                            headers,
                            settings,
                            None,
                            extension_for_content_type(None, url),
                        )
                    )
                continue
            if probe.kind in {"hls", "dash"}:
                if manifest_budget <= 0:
                    continue
                manifest_budget -= 1
                source, reason = await _manifest_source(
                    client, job, settings, probe.url, page_ref, headers, probe.kind
                )
                if source is not None:
                    sources.append(source)
                if reason:
                    drm_reasons.append(reason)
                continue
            if probe.kind != "direct":
                continue
            if probe.filesize and probe.filesize > settings.max_file_size_bytes:
                too_large.append(probe.filesize)
                continue
            sources.append(
                _direct_source(
                    probe.url,
                    page_ref,
                    headers,
                    settings,
                    probe.filesize,
                    extension_for_content_type(probe.content_type, probe.url),
                )
            )
            continue

        if manifest_budget <= 0:
            continue
        manifest_budget -= 1
        source, reason = await _manifest_source(
            client, job, settings, url, page_ref, headers, kind
        )
        if source is not None:
            sources.append(source)
        if reason:
            drm_reasons.append(reason)

    if sources:
        sources = rank_sources(sources)
        job.log(
            f"Static HTML found {len(sources)} usable source(s). Best: {sources[0].label}.",
            level="success",
            tier=2,
        )
        return sources

    if drm_reasons:
        job.log(drm_reasons[0], level="warning", tier=2)
        raise DRMProtected()
    if too_large:
        raise FileTooLarge(
            f"The only media found is {human_bytes(max(too_large))}, above the configured {settings.max_file_size_mb} MB limit."
        )
    job.log("No media URLs survived validation in the static HTML.", level="warning", tier=2)
    return []


def _candidates_from_html(
    html_text: str,
    soup: BeautifulSoup,
    base_url: str,
    *,
    include_iframes: bool = True,
) -> tuple[list[Candidate], list[str]]:
    candidates: list[Candidate] = []
    iframes: list[str] = []

    def add(url: str | None, *, trusted: bool, note: str = "", page_url: str | None = None) -> None:
        if not url:
            return
        url = url.strip()
        if url.lower().startswith(("javascript:", "data:", "blob:", "about:")):
            return
        absolute = urljoin(base_url, url)
        if not absolute.lower().startswith(("http://", "https://")):
            return
        candidates.append(Candidate(absolute, page_url or base_url, trusted, note))

    def add_iframe(url: str) -> None:
        absolute = urljoin(base_url, url.strip())
        if absolute.lower().startswith(("http://", "https://")) and absolute not in iframes:
            iframes.append(absolute)

    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").strip().lower()
        content = tag.get("content")
        if not key or not content:
            continue
        if key in _META_VIDEO_KEYS:
            if media_kind_from_url(content) or looks_like_media_url(content):
                add(content, trusted=True, note=key)
            elif key in _EMBED_META_KEYS:
                add_iframe(content)
        elif key in _EMBED_META_KEYS:
            if media_kind_from_url(content):
                add(content, trusted=True, note=key)
            else:
                add_iframe(content)

    for video in soup.find_all("video"):
        add(video.get("src"), trusted=True, note="video src")
        add(video.get("data-src"), trusted=True, note="video data-src")
        for source in video.find_all("source"):
            if _source_is_audio(source):
                continue
            add(source.get("src"), trusted=True, note="source src")
    for source in soup.find_all("source"):
        if source.find_parent("video") is not None:
            continue
        if _source_is_audio(source):
            continue
        add(source.get("src"), trusted=True, note="source src")

    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        as_attr = (link.get("as") or "").lower()
        if "video" in as_attr or "video" in rel:
            add(link.get("href"), trusted=True, note="link preload")

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.IGNORECASE)}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for video_object in _walk_video_objects(data):
            content_url = video_object.get("contentUrl")
            if isinstance(content_url, dict):
                content_url = content_url.get("@id") or content_url.get("url")
            if isinstance(content_url, str):
                add(content_url, trusted=True, note="JSON-LD contentUrl")
            embed_url = video_object.get("embedUrl")
            if isinstance(embed_url, dict):
                embed_url = embed_url.get("@id") or embed_url.get("url")
            if isinstance(embed_url, str):
                if media_kind_from_url(embed_url):
                    add(embed_url, trusted=True, note="JSON-LD embedUrl")
                else:
                    add_iframe(embed_url)

    if include_iframes:
        for frame in soup.find_all("iframe"):
            add_iframe(frame.get("src") or frame.get("data-src") or "")

    scan_text = html_text.replace("\\/", "/").replace("\\u002F", "/").replace("&amp;", "&")
    for match in _ABS_MEDIA_RE.finditer(scan_text):
        add(match.group(0), trusted=False, note="inline script")
    for match in _REL_MEDIA_RE.finditer(scan_text):
        add(match.group(0), trusted=False, note="inline script relative")

    return candidates, iframes


def _walk_video_objects(data, depth: int = 0):
    if depth > 5:
        return
    if isinstance(data, list):
        for item in data:
            yield from _walk_video_objects(item, depth + 1)
        return
    if not isinstance(data, dict):
        return
    types = data.get("@type")
    type_list = types if isinstance(types, list) else [types]
    if any(str(item).lower() == "videoobject" for item in type_list if item):
        yield data
    for key in ("@graph", "itemListElement", "mainEntity", "hasPart"):
        if key in data:
            yield from _walk_video_objects(data[key], depth + 1)


def _source_is_audio(source) -> bool:
    type_attr = (source.get("type") or "").lower()
    return type_attr.startswith("audio/")


def _extract_title(soup: BeautifulSoup) -> str | None:
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        if key in {"og:title", "twitter:title"}:
            content = (tag.get("content") or "").strip()
            if content:
                return content[:180]
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        if title:
            return title[:180]
    return None


def _media_headers(settings: Settings, url: str, referer: str | None) -> dict[str, str]:
    headers = base_headers(settings.user_agent, referer=referer)
    origin = origin_for(referer or url)
    if origin:
        headers["Origin"] = origin
    return headers


def _direct_source(
    url: str,
    page_url: str,
    headers: dict[str, str],
    settings: Settings,
    filesize: int | None,
    extension: str,
) -> Source:
    ext = extension or ".mp4"
    match = _RESOLUTION_RE.search(url)
    height = int(match.group(1)) if match else None
    quality = f"{height}p" if height else ""
    label = f"{ext.lstrip('.').upper()} · progressive"
    if quality:
        label += f" · {quality}"
    return Source(
        url=url,
        kind="direct",
        tier=2,
        label=label,
        method="direct",
        quality=quality,
        height=height,
        ext=ext,
        filesize=filesize,
        headers=headers,
        page_url=page_url,
    )


async def _manifest_source(
    client: httpx.AsyncClient,
    job,
    settings: Settings,
    url: str,
    page_url: str,
    headers: dict[str, str],
    kind: str,
) -> tuple[Source | None, str | None]:
    try:
        manifest = await fetch_limited(
            client,
            "GET",
            url,
            headers=headers,
            max_bytes=2 * 1024 * 1024,
            allow_private=settings.allow_private_hosts,
        )
    except AppError as exc:
        if exc.code == "blocked_url":
            job.log("Skipped a manifest on a private address.", level="warning", tier=2)
            return None, None
        job.log(f"Could not read a manifest: {exc.message}", level="warning", tier=2)
        return None, None

    if manifest.status in {401, 402}:
        job.flags.add("login")
        return None, None
    if manifest.status >= 400:
        return None, None

    info = scan_manifest(manifest.url, manifest.text)
    if info.drm:
        job.log(info.reason or "A manifest declares DRM.", level="warning", tier=2)
        return None, info.reason or "DRM manifest detected."
    if info.duration and info.duration > settings.max_duration_seconds:
        raise DurationTooLong(
            f"That stream is {info.duration / 60:.0f} minutes long, above the configured {settings.max_duration_seconds // 60}-minute limit."
        )

    source = Source(
        url=manifest.url,
        kind=kind,
        tier=2,
        label="HLS · adaptive" if kind == "hls" else "DASH · adaptive",
        method="ytdlp",
        quality="",
        ext="mp4",
        filesize=None,
        headers=headers,
        page_url=page_url,
        duration=info.duration,
    )
    apply_manifest_info(source, info)
    return source, None
