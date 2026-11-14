"""Tier 1: yt-dlp with the generic extractor enabled.

yt-dlp ships the generic extractor by default and falls back to it for unknown
sites. We only list formats here; downloading happens in the job worker.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Settings
from ..errors import DRMProtected, DurationTooLong, FileTooLarge
from .sources import Source

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _YtdlpLog:
    def __init__(self, job):
        self.job = job

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        text = _ANSI_RE.sub("", str(message)).strip()
        if text:
            self.job.log(text[:400], level="warning", tier=1)

    def error(self, message: str) -> None:
        text = _ANSI_RE.sub("", str(message)).strip()
        if text:
            self.job.log(text[:400], level="warning", tier=1)


def extract_sync(job, settings: Settings) -> list[Source]:
    job.begin_tier(1)
    try:
        import yt_dlp
    except ImportError:
        job.log("yt-dlp is not installed; skipping Tier 1.", level="warning", tier=1)
        return []

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": settings.request_timeout_seconds,
        "retries": 2,
        "extractor_retries": 1,
        "http_headers": {
            "User-Agent": settings.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "logger": _YtdlpLog(job),
    }
    if settings.proxy_url:
        options["proxy"] = settings.proxy_url
    if job.cookies_path:
        options["cookiefile"] = job.cookies_path

    job.log("Running yt-dlp generic extraction…", tier=1)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(job.url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        _handle_extract_error(job, str(exc))
        return []
    except Exception as exc:  # extractors can raise arbitrary exceptions
        job.log(f"yt-dlp could not process this page: {exc}", level="warning", tier=1)
        return []

    if not info:
        job.log("yt-dlp returned no metadata.", level="warning", tier=1)
        return []

    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            job.log("yt-dlp found a playlist with no entries.", level="warning", tier=1)
            return []
        info = entries[0]
        job.log("The URL is a playlist; using its first video.", level="warning", tier=1)

    if info.get("_has_drm"):
        raise DRMProtected()

    title = info.get("title")
    if title:
        job.set_title(str(title))

    duration = info.get("duration")
    if duration and float(duration) > settings.max_duration_seconds:
        raise DurationTooLong(
            f"That video is {float(duration) / 60:.0f} minutes long, above the configured {settings.max_duration_seconds // 60}-minute limit."
        )

    if info.get("is_live"):
        job.log("Live streams are not supported by this downloader.", level="warning", tier=1)
        return []

    formats = list(info.get("formats") or [])
    if not formats and info.get("url"):
        formats = [info]

    shared_headers = _clean_headers(info.get("http_headers") or {})
    candidates: dict[tuple, Source] = {}
    drm_formats = 0
    oversized_formats: list[int] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if fmt.get("has_drm"):
            drm_formats += 1
            continue
        if str(fmt.get("vcodec") or "none") == "none":
            continue
        if str(fmt.get("ext") or "").lower() in {"mhtml", "json"}:
            continue
        if "storyboard" in str(fmt.get("format_note") or "").lower():
            continue
        if not fmt.get("url"):
            continue
        if fmt.get("is_live"):
            continue

        height = _as_int(fmt.get("height"))
        width = _as_int(fmt.get("width"))
        acodec = str(fmt.get("acodec") or "none")
        has_audio = acodec not in {"", "none"}
        ext = str(fmt.get("ext") or "mp4").lower()
        protocol = str(fmt.get("protocol") or "").lower()
        fmt_url = str(fmt.get("url") or "")
        if "m3u8" in protocol or ".m3u8" in fmt_url:
            kind = "hls"
        elif "dash" in protocol or ".mpd" in fmt_url:
            kind = "dash"
        else:
            kind = "direct"

        filesize = _as_int(fmt.get("filesize")) or _as_int(fmt.get("filesize_approx"))
        if filesize and filesize > settings.max_file_size_bytes:
            oversized_formats.append(filesize)
            continue

        note = str(fmt.get("format_note") or "")
        quality = f"{height}p" if height else (note if note and note.lower() != "default" else "Video")
        label_parts = [quality, ext.upper()]
        if not has_audio:
            label_parts.append("video only · audio merged")
        elif note and note.lower() not in {quality.lower(), "default"} and len(note) <= 24:
            label_parts.append(note)

        key = (height, ext, has_audio)
        if key in candidates:
            previous = candidates[key]
            if (filesize or 0) <= (previous.filesize or 0):
                continue

        candidates[key] = Source(
            url=job.url,
            kind=kind,
            tier=1,
            label=" · ".join(part for part in label_parts if part),
            method="ytdlp",
            quality=quality,
            height=height,
            width=width,
            ext="mp4",
            filesize=filesize,
            headers=shared_headers,
            ytdlp_format_id=str(fmt.get("format_id") or ""),
            ytdlp_has_audio=has_audio,
            ytdlp_video_only=not has_audio,
            page_url=job.url,
            duration=float(duration) if duration else None,
        )

    if not candidates:
        if drm_formats:
            raise DRMProtected()
        if oversized_formats:
            raise FileTooLarge(
                f"yt-dlp's formats are above the configured {settings.max_file_size_mb} MB limit."
            )
        job.log("yt-dlp reported no usable video formats.", level="warning", tier=1)
        return []

    sources = sorted(
        candidates.values(),
        key=lambda source: ((source.height or 0), (source.filesize or 0)),
        reverse=True,
    )[:12]
    job.log(
        f"yt-dlp found {len(sources)} downloadable format(s). Best: {sources[0].label}.",
        level="success",
        tier=1,
    )
    return sources


def _handle_extract_error(job, raw_message: str) -> None:
    message = _ANSI_RE.sub("", str(raw_message)).strip()
    recent = message.splitlines()[-1] if message else "yt-dlp failed."
    job.log(f"yt-dlp: {recent[:400]}", level="warning", tier=1)
    low = message.lower()
    flags = job.flags
    if "drm" in low:
        raise DRMProtected()
    if "captcha" in low or "challenge" in low:
        flags.add("captcha")
    elif any(token in low for token in ("sign in", "log in", "login", "private video", "members-only")):
        flags.add("login")
    elif any(token in low for token in ("not available in your country", "geo", "region")):
        flags.add("geo")


def _clean_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    ignored = {"cookie", "host", "content-length", "connection", "accept-encoding"}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).lower() not in ignored and value
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
