"""Shared helpers for probing a candidate URL with a Range request."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import Settings
from ..http_utils import FetchResult, fetch_limited, total_size_from_headers
from ..security import looks_like_media_url, media_kind_from_url


@dataclass
class ProbeResult:
    status: int
    url: str
    content_type: str
    filesize: int | None
    kind: str | None
    truncated: bool = False


async def probe_media(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    settings: Settings,
) -> ProbeResult:
    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-0"
    probe_headers.setdefault("Accept", "*/*")
    result = await fetch_limited(
        client,
        "GET",
        url,
        headers=probe_headers,
        max_bytes=1,
        allow_private=settings.allow_private_hosts,
    )
    kind = classify_media(result, url)
    return ProbeResult(
        status=result.status,
        url=result.url,
        content_type=result.content_type,
        filesize=total_size_from_headers(result.headers),
        kind=kind,
        truncated=result.truncated,
    )


def classify_media(result: FetchResult, url: str) -> str | None:
    """Return hls/dash/direct when the response looks like downloadable media."""
    ct = result.content_type
    url_kind = media_kind_from_url(url)
    if ct in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"}:
        return "hls"
    if ct in {"application/dash+xml"}:
        return "dash"
    if url_kind in {"hls", "dash"}:
        return url_kind
    if ct.startswith("video/"):
        return "direct"
    if ct in {"application/octet-stream", "binary/octet-stream", ""} and url_kind == "direct":
        return "direct"
    if url_kind == "direct" and not ct.startswith("text/") and not ct.startswith("application/json"):
        return "direct"
    return None


def is_manifest_kind(kind: str | None) -> bool:
    return kind in {"hls", "dash"}


def is_probably_media_url(url: str) -> bool:
    return looks_like_media_url(url) or media_kind_from_url(url) is not None
