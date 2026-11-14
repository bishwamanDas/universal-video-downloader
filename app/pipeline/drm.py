"""DRM and manifest inspection.

This module only *detects* protection so the pipeline can stop with a clear
message. It deliberately contains no key handling or circumvention code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sources import Source

_HLS_KEY_TAG_RE = re.compile(r"#EXT-X-(?:SESSION-)?KEY\s*:\s*(.+)", re.IGNORECASE)
_KEYFORMAT_DRM = (
    "widevine",
    "playready",
    "fairplay",
    "com.apple.streamingkeydelivery",
    "com.microsoft.playready",
    "edef8ba9",  # Widevine UUID
    "9a04f079",  # PlayReady UUID
    "94ce86fb",  # FairPlay UUID
)
_LICENSE_MARKERS = ("widevine", "playready", "fairplay", "clearkey")
_VARIANT_RE = re.compile(r"#EXT-X-STREAM-INF:([^\n\r]+)", re.IGNORECASE)
_ATTR_RE = re.compile(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)')


@dataclass
class ManifestInfo:
    drm: bool
    reason: str | None = None
    height: int | None = None
    width: int | None = None
    duration: float | None = None
    variant_count: int = 0


def parse_attributes(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1).upper()] = value
    return attrs


def scan_hls_text(text: str) -> ManifestInfo:
    drm = False
    reason: str | None = None
    for line in text.splitlines():
        match = _HLS_KEY_TAG_RE.search(line.strip())
        if not match:
            continue
        attrs = parse_attributes(match.group(1))
        method = attrs.get("METHOD", "").upper()
        if method in {"", "NONE"}:
            continue
        keyformat = attrs.get("KEYFORMAT", "").lower()
        uri = attrs.get("URI", "").lower()
        combined = f"{keyformat} {uri}"
        if any(marker in combined for marker in _KEYFORMAT_DRM):
            drm = True
            reason = f"HLS uses {method} with a DRM key system ({keyformat or 'unknown key format'})."
            break
        if method.startswith("SAMPLE-AES"):
            drm = True
            reason = "HLS uses SAMPLE-AES encryption, which is DRM-protected."
            break

    height: int | None = None
    width: int | None = None
    variant_count = 0
    for match in _VARIANT_RE.finditer(text):
        variant_count += 1
        attrs = parse_attributes(match.group(1))
        resolution = attrs.get("RESOLUTION", "")
        if "x" in resolution.lower():
            try:
                w, h = resolution.lower().split("x", 1)
                width = max(width or 0, int(w))
                height = max(height or 0, int(h))
            except ValueError:
                continue

    duration: float | None = None
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                total += float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                continue
    if total > 0:
        duration = total

    return ManifestInfo(
        drm=drm,
        reason=reason,
        height=height,
        width=width,
        duration=duration,
        variant_count=variant_count,
    )


def scan_dash_text(text: str) -> ManifestInfo:
    if re.search(r"<ContentProtection\b", text, re.IGNORECASE):
        scheme = ""
        match = re.search(r'schemeIdUri="([^"]+)"', text, re.IGNORECASE)
        if match:
            scheme = match.group(1)
        reason = "DASH manifest declares <ContentProtection>"
        if scheme:
            reason += f" ({scheme})"
        reason += "."
        return ManifestInfo(drm=True, reason=reason)

    height: int | None = None
    width: int | None = None
    for match in re.finditer(r'<Representation\b[^>]*>', text, re.IGNORECASE):
        tag = match.group(0)
        h = re.search(r'height="(\d+)"', tag)
        w = re.search(r'width="(\d+)"', tag)
        if h:
            height = max(height or 0, int(h.group(1)))
        if w:
            width = max(width or 0, int(w.group(1)))

    duration: float | None = None
    match = re.search(r'mediaPresentationDuration="PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?"', text)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = float(match.group(3) or 0)
        duration = hours * 3600 + minutes * 60 + seconds

    return ManifestInfo(drm=False, height=height, width=width, duration=duration)


def scan_manifest(url: str, text: str) -> ManifestInfo:
    low = url.lower()
    if ".m3u8" in low or "#EXTM3U" in text[:200].upper():
        return scan_hls_text(text)
    if ".mpd" in low or "<MPD" in text[:1000]:
        return scan_dash_text(text)
    return ManifestInfo(drm=False)


def looks_like_license_request(url: str, content_type: str | None) -> bool:
    """Heuristic for license/key-server traffic seen by the browser sniffer."""
    low = url.lower()
    ct = (content_type or "").lower()
    if any(token in low for token in _LICENSE_MARKERS):
        return True
    if "license" in low and (
        "drm" in low
        or ct.startswith("application/octet-stream")
        or ct.startswith("application/json")
        or ct.startswith("application/xml")
        or ct.startswith("text/xml")
    ):
        return True
    return False


def apply_manifest_info(source: Source, info: ManifestInfo) -> None:
    source.drm = info.drm
    source.drm_reason = info.reason
    if info.height:
        source.height = info.height
        source.quality = f"{info.height}p"
        source.label = f"{source.kind.upper()} · {info.height}p"
    if info.duration and not source.duration:
        source.duration = info.duration
