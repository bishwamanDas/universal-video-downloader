"""Source candidates and ranking."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

TIER_NAMES = {
    0: "Direct media",
    1: "yt-dlp",
    2: "HTML parser",
    3: "Browser sniffer",
}

KIND_NAMES = {
    "direct": "Progressive",
    "hls": "HLS",
    "dash": "DASH",
    "ytdlp": "yt-dlp",
}


@dataclass
class Source:
    url: str
    kind: str
    tier: int
    label: str
    method: str = "direct"  # direct | ytdlp | ffmpeg
    quality: str = ""
    height: int | None = None
    width: int | None = None
    ext: str = "mp4"
    filesize: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    ytdlp_format_id: str | None = None
    ytdlp_has_audio: bool = True
    ytdlp_video_only: bool = False
    page_url: str | None = None
    duration: float | None = None
    drm: bool = False
    drm_reason: str | None = None
    id: str = field(init=False)

    def __post_init__(self) -> None:
        key = f"{self.tier}|{self.url}|{self.ytdlp_format_id or ''}|{self.label}"
        self.id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "kind": self.kind,
            "kind_name": KIND_NAMES.get(self.kind, self.kind),
            "tier": self.tier,
            "tier_name": TIER_NAMES.get(self.tier, "engine"),
            "label": self.label,
            "quality": self.quality,
            "ext": self.ext,
            "filesize": self.filesize,
            "height": self.height,
            "duration": self.duration,
            "drm": self.drm,
            "drm_reason": self.drm_reason,
        }


def rank_sources(sources: list[Source], limit: int = 12) -> list[Source]:
    """Deduplicate and rank. Manifests win ties because they offer adaptive quality."""
    unique: dict[tuple[str, str, str | None], Source] = {}
    for source in sources:
        key = (source.url, source.kind, source.ytdlp_format_id)
        existing = unique.get(key)
        if existing is None or (source.filesize or 0) > (existing.filesize or 0):
            unique[key] = source

    def score(source: Source) -> float:
        manifest_bonus = 60 if source.kind in {"hls", "dash"} else 0
        height_score = (source.height or 0) * 2
        size_score = math.log10((source.filesize or 0) + 10)
        tier_note = 2 if source.tier == 3 else 0
        return height_score + manifest_bonus + size_score + tier_note

    return sorted(unique.values(), key=score, reverse=True)[:limit]


def human_bytes(value: int | None) -> str:
    if not value or value <= 0:
        return "unknown size"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"
