"""Runtime configuration.

Everything is env-overridable so the app can be tuned without code changes.
Defaults are deliberately conservative: local host only, DRM never bypassed,
downloads capped at 2 GiB and 3 hours.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value is not None and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be a number.") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    download_dir: Path
    data_dir: Path
    static_dir: Path
    max_file_size_mb: int
    max_duration_seconds: int
    job_ttl_seconds: int
    download_concurrency: int
    analyze_concurrency: int
    proxy_url: str
    playwright_enabled: bool
    headless: bool
    sniff_timeout_seconds: float
    request_timeout_seconds: float
    ytdlp_timeout_seconds: float
    max_page_bytes: int
    download_chunk_size: int
    max_cookies_bytes: int
    allow_private_hosts: bool
    ffmpeg_path: str | None
    user_agent: str = DEFAULT_USER_AGENT

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def cookies_dir(self) -> Path:
        return self.data_dir / "cookies"

    @property
    def temp_dir(self) -> Path:
        return self.download_dir / "_tmp"


def load_settings() -> Settings:
    ffmpeg_env = (
        os.environ.get("UVD_FFMPEG_PATH", "").strip()
        or os.environ.get("FFMPEG_PATH", "").strip()
        or None
    )
    raw_host = os.environ.get("UVD_HOST") or os.environ.get("HOST") or "127.0.0.1"
    raw_port = os.environ.get("UVD_PORT") or os.environ.get("PORT") or "8000"
    try:
        parsed_port = int(raw_port)
    except ValueError:
        parsed_port = 8000

    return Settings(
        host=raw_host.strip(),
        port=parsed_port,
        download_dir=Path(_env_str("UVD_DOWNLOAD_DIR", str(BASE_DIR / "downloads"))).resolve(),
        data_dir=Path(_env_str("UVD_DATA_DIR", str(BASE_DIR / "data"))).resolve(),
        static_dir=BASE_DIR / "app" / "static",
        max_file_size_mb=_env_int("UVD_MAX_FILE_SIZE_MB", 2048),
        max_duration_seconds=_env_int("UVD_MAX_DURATION_SECONDS", 3 * 60 * 60),
        job_ttl_seconds=_env_int("UVD_JOB_TTL_SECONDS", 3600),
        download_concurrency=max(1, _env_int("UVD_DOWNLOAD_CONCURRENCY", 2)),
        analyze_concurrency=max(1, _env_int("UVD_ANALYZE_CONCURRENCY", 2)),
        proxy_url=_env_str("UVD_PROXY_URL", ""),
        playwright_enabled=_env_bool("UVD_PLAYWRIGHT_ENABLED", True),
        headless=_env_bool("UVD_HEADLESS", True),
        sniff_timeout_seconds=_env_float("UVD_SNIFF_TIMEOUT_SECONDS", 15.0),
        request_timeout_seconds=_env_float("UVD_REQUEST_TIMEOUT_SECONDS", 20.0),
        ytdlp_timeout_seconds=_env_float("UVD_YTDLP_TIMEOUT_SECONDS", 60.0),
        max_page_bytes=_env_int("UVD_MAX_PAGE_BYTES", 8 * 1024 * 1024),
        download_chunk_size=max(16384, _env_int("UVD_DOWNLOAD_CHUNK_SIZE", 262144)),
        max_cookies_bytes=_env_int("UVD_MAX_COOKIES_BYTES", 1024 * 1024),
        allow_private_hosts=_env_bool("UVD_ALLOW_PRIVATE_HOSTS", False),
        ffmpeg_path=ffmpeg_env,
        user_agent=_env_str("UVD_USER_AGENT", DEFAULT_USER_AGENT),
    )
