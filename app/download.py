"""Download engine.

Handles direct HTTP files with resume, HLS/DASH via yt-dlp with an FFmpeg
fallback, stream merging without re-encoding, request-context replay, retries,
size/duration limits, and final file naming.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .config import Settings
from .errors import (
    AppError,
    DRMProtected,
    DurationTooLong,
    FileTooLarge,
    GeoBlocked,
    InternalError,
    LoginRequired,
    NetworkError,
    UnsupportedMedia,
    UpstreamTimeout,
    classify_external_message,
)
from .http_utils import fetch_limited_sync, origin_for
from .pipeline.drm import scan_manifest
from .pipeline.sources import Source, human_bytes
from .security import (
    assert_connected_peer_public,
    extension_for_content_type,
    normalize_and_validate_url,
    parse_content_length,
    parse_content_range_total,
    sanitize_filename,
)

logger = logging.getLogger("uvd.download")

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def find_ffmpeg(settings: Settings) -> str | None:
    candidates: list[str] = []
    if settings.ffmpeg_path:
        candidates.append(settings.ffmpeg_path)
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    if os.name == "nt":
        candidates.extend(
            [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"),
            ]
        )
    for candidate in candidates:
        try:
            path = Path(candidate)
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return None


def find_ffprobe(ffmpeg_path: str | None) -> str | None:
    if ffmpeg_path:
        sibling = Path(ffmpeg_path).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


def run_download(job, source: Source, settings: Settings) -> None:
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    job.workdir.mkdir(parents=True, exist_ok=True)
    job.start_download(f"Preparing to download {source.label}…")
    ffmpeg = find_ffmpeg(settings)
    if source.ytdlp_video_only and not ffmpeg:
        raise UnsupportedMedia(
            "This source stores video and audio as separate streams. Install FFmpeg so they can be merged into one MP4.",
            hint="Install FFmpeg, restart the app, and retry. Set UVD_FFMPEG_PATH if it is not on PATH.",
        )

    if source.tier == 1 and source.method == "ytdlp":
        produced = _download_with_ytdlp(job, source, settings, ffmpeg)
    elif source.kind in {"hls", "dash"}:
        produced = _download_manifest(job, source, settings, ffmpeg)
    else:
        produced = _download_direct(job, source, settings)

    if produced is None or not produced.exists() or produced.stat().st_size == 0:
        raise InternalError("The download finished but no media file was produced.")

    _enforce_duration_limit(job, produced, settings, ffmpeg)
    if produced.stat().st_size > settings.max_file_size_bytes:
        raise FileTooLarge(
            f"That file is {human_bytes(produced.stat().st_size)}, above the configured {settings.max_file_size_mb} MB limit."
        )

    if produced.suffix.lower() != ".mp4":
        produced = _remux_to_mp4(job, produced, settings, ffmpeg)

    filename = _unique_filename(job, produced.suffix, settings)
    destination = settings.download_dir / filename
    shutil.move(str(produced), str(destination))
    job.finish(destination, filename, destination.stat().st_size)
    shutil.rmtree(job.workdir, ignore_errors=True)


def _request_headers(job, source: Source, settings: Settings) -> dict[str, str]:
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for key, value in (source.headers or {}).items():
        if key.lower() in {"cookie", "host", "content-length", "connection", "accept-encoding"}:
            continue
        headers[key] = value
    if "Referer" not in headers and source.page_url:
        headers["Referer"] = source.page_url
    if "Origin" not in headers:
        origin = origin_for(headers.get("Referer") or source.page_url or "")
        if origin:
            headers["Origin"] = origin
    cookie = job.cookie_header_for(source.url)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(settings.request_timeout_seconds, read=60.0),
        proxy=settings.proxy_url or None,
        follow_redirects=False,
    )


def _open_stream(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    settings: Settings,
) -> tuple[httpx.Response, str]:
    current = url
    for _ in range(6):
        current = normalize_and_validate_url(current, allow_private=settings.allow_private_hosts)
        request = client.build_request("GET", current, headers=headers)
        try:
            response = client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("The media server took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise NetworkError("Could not connect to the media server.") from exc
        if not settings.allow_private_hosts:
            assert_connected_peer_public(response)
        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("location")
            response.close()
            if not location:
                raise NetworkError("The media server sent a redirect without a location.")
            current = urljoin(current, location)
            continue
        return response, current
    raise NetworkError("Too many redirects while fetching the media.")


def _download_direct(job, source: Source, settings: Settings) -> Path:
    job.start_download(f"Downloading {source.label}…")
    part_path = job.workdir / "source.part"
    base_headers = _request_headers(job, source, settings)
    base_headers["Accept-Encoding"] = "identity"
    attempts = 4

    for attempt in range(1, attempts + 1):
        existing = part_path.stat().st_size if part_path.exists() else 0
        if source.filesize and existing > source.filesize:
            part_path.unlink(missing_ok=True)
            existing = 0
        if source.filesize and existing and existing >= source.filesize:
            result = job.workdir / f"source{source.ext or '.mp4'}"
            part_path.replace(result)
            return result

        headers = dict(base_headers)
        if existing:
            headers["Range"] = f"bytes={existing}-"

        try:
            with _http_client(settings) as client:
                response, final_url = _open_stream(client, source.url, headers, settings)
                try:
                    _raise_for_http_error(response)
                    if response.status_code == 416:
                        if source.filesize and existing >= source.filesize:
                            extension = extension_for_content_type(
                                response.headers.get("content-type"), source.url
                            )
                            result = job.workdir / f"source{extension}"
                            part_path.replace(result)
                            return result
                        part_path.unlink(missing_ok=True)
                        raise NetworkError("The server rejected the resume range; restarting.")

                    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if content_type.startswith(("text/html", "application/json")):
                        raise NetworkError(
                            "The media URL returned a web page instead of a video.",
                            hint="The download link may have expired. Re-analyze the page.",
                        )

                    ranged_total = parse_content_range_total(response.headers)
                    if response.status_code == 206:
                        content_range = response.headers.get("content-range", "")
                        match = re.match(r"bytes\s+(\d+)-", content_range)
                        if not match or int(match.group(1)) != existing:
                            part_path.unlink(missing_ok=True)
                            raise NetworkError("The server returned an unexpected byte range.")
                        total = ranged_total or source.filesize
                        write_mode = "ab"
                        downloaded = existing
                    else:
                        total = parse_content_length(response.headers) or source.filesize
                        if existing:
                            # Server ignored Range: restart from zero.
                            part_path.unlink(missing_ok=True)
                        write_mode = "wb"
                        downloaded = 0

                    if total and total > settings.max_file_size_bytes:
                        raise FileTooLarge(
                            f"The file is {human_bytes(total)}, above the configured {settings.max_file_size_mb} MB limit."
                        )

                    started = time.monotonic()
                    last_update = 0.0
                    with part_path.open(write_mode) as handle:
                        for chunk in response.iter_bytes(settings.download_chunk_size):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if downloaded > settings.max_file_size_bytes:
                                raise FileTooLarge(
                                    f"File is larger than the maximum configured size of {settings.max_file_size_mb} MB."
                                )
                            now = time.monotonic()
                            if now - last_update >= 0.25:
                                elapsed = max(now - started, 0.001)
                                speed = (downloaded - existing) / elapsed if downloaded >= existing else 0
                                job.update_progress(
                                    downloaded,
                                    total,
                                    speed,
                                    stage=f"Downloading {source.label}…",
                                )
                                last_update = now

                    if total and downloaded < total:
                        raise NetworkError(
                            f"The connection closed early ({human_bytes(downloaded)} of {human_bytes(total)})."
                        )
                    if downloaded == 0:
                        raise NetworkError("The server sent an empty response.")

                    elapsed = max(time.monotonic() - started, 0.001)
                    job.update_progress(downloaded, total or downloaded, (downloaded - existing) / elapsed)
                    extension = extension_for_content_type(content_type, final_url)
                    if extension in {".m3u8", ".mpd"}:
                        extension = ".mp4"
                    output = job.workdir / f"source{extension}"
                    part_path.replace(output)
                    job.log(
                        f"Downloaded {human_bytes(downloaded)} from the direct URL.",
                        level="success",
                    )
                    return output
                finally:
                    response.close()
        except FileTooLarge:
            part_path.unlink(missing_ok=True)
            raise
        except (NetworkError, UpstreamTimeout) as exc:
            if attempt >= attempts:
                raise
            delay = 2 ** (attempt - 1)
            job.log(
                f"Transfer interrupted ({exc.message}). Retrying in {delay}s with resume…",
                level="warning",
            )
            time.sleep(delay)
        except LoginRequired:
            raise
    raise NetworkError("The download failed after multiple retries.")


def _raise_for_http_error(response: httpx.Response) -> None:
    status = response.status_code
    if status in {200, 206, 416}:
        return
    preview = ""
    try:
        for chunk in response.iter_bytes(8192):
            preview += chunk.decode("utf-8", errors="ignore")
            break
    except Exception:
        pass
    low = preview.lower()
    if status in {401, 402}:
        raise LoginRequired()
    if status == 451:
        raise GeoBlocked()
    if status in {403, 429}:
        if "captcha" in low or "verify you are human" in low:
            from .errors import CaptchaBlocked

            raise CaptchaBlocked()
        if any(token in low for token in ("sign in", "log in", "login", "members only")):
            raise LoginRequired()
        if any(token in low for token in ("country", "region", "geo")):
            raise GeoBlocked()
        raise NetworkError(
            f"The site refused the transfer (HTTP {status}).",
            hint="Attach cookies.txt for your own account and re-analyze the page; some CDNs require fresh request headers.",
        )
    if status >= 500:
        raise NetworkError(f"The media server returned HTTP {status}.")
    raise NetworkError(f"The media server returned HTTP {status}.")


def _download_manifest(job, source: Source, settings: Settings, ffmpeg: str | None) -> Path:
    _assert_manifest_safe(job, source, settings)
    try:
        return _download_with_ytdlp(job, source, settings, ffmpeg)
    except AppError as exc:
        if exc.code in {"drm", "too_large", "too_long", "login_required", "geo_blocked", "blocked_url"}:
            raise
        if not ffmpeg:
            raise
        job.log(
            f"yt-dlp could not fetch the stream ({exc.message}); falling back to FFmpeg.",
            level="warning",
        )
        return _download_with_ffmpeg(job, source, settings, ffmpeg)


def _download_with_ffmpeg(job, source: Source, settings: Settings, ffmpeg: str) -> Path:
    job.mark_merging("Downloading and muxing the stream with FFmpeg…")
    output = job.workdir / "ffmpeg-output.mp4"
    headers = _request_headers(job, source, settings)
    header_lines = []
    for key in ("Referer", "Origin", "Cookie"):
        if headers.get(key):
            header_lines.append(f"{key}: {headers[key]}")
    header_block = "\r\n".join(header_lines) + "\r\n" if header_lines else ""

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-user_agent",
        headers.get("User-Agent", settings.user_agent),
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
    ]
    if header_block:
        command += ["-headers", header_block]
    command += [
        "-i",
        source.url,
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(600, settings.max_duration_seconds + 300),
        )
    except subprocess.TimeoutExpired as exc:
        raise UpstreamTimeout("FFmpeg timed out while downloading the stream.") from exc
    if completed.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        tail = (completed.stderr or "FFmpeg failed.").strip().splitlines()
        message = tail[-1] if tail else "FFmpeg failed."
        raise classify_external_message(message)
    size = output.stat().st_size
    job.update_progress(size, size, 0, progress=99.0)
    return output


class _QuietYtdlpLogger:
    def __init__(self, job):
        self.job = job

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        text = _strip_ansi(str(message)).strip()
        if text:
            self.job.log(text[:400], level="warning")

    def error(self, message: str) -> None:
        pass


def _download_with_ytdlp(job, source: Source, settings: Settings, ffmpeg: str | None) -> Path:
    try:
        import yt_dlp
    except ImportError as exc:
        raise InternalError("yt-dlp is not installed. Run: pip install -r requirements.txt") from exc

    headers = _request_headers(job, source, settings)
    headers.pop("Cookie", None)
    selector = _format_selector(source)
    job.start_download(f"Downloading {source.label} with yt-dlp…")

    def progress_hook(data):
        try:
            status = data.get("status")
            if status == "downloading":
                downloaded = int(data.get("downloaded_bytes") or 0)
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                total_int = int(total) if total else None
                speed = data.get("speed")
                fragment_index = data.get("fragment_index")
                fragment_count = data.get("fragment_count")
                progress = None
                if fragment_index and fragment_count:
                    progress = float(fragment_index) / float(fragment_count) * 100.0
                if downloaded > settings.max_file_size_bytes:
                    raise FileTooLarge(
                        f"File is larger than the maximum configured size of {settings.max_file_size_mb} MB."
                    )
                job.update_progress(
                    downloaded,
                    total_int,
                    float(speed) if speed else None,
                    progress=progress,
                    stage=f"Downloading {source.label}…",
                )
            elif status == "finished":
                job.set_stage("Stream downloaded; preparing merge…")
        except FileTooLarge:
            raise
        except Exception:
            return

    def postprocessor_hook(data):
        try:
            status = data.get("status")
            postprocessor = str(data.get("postprocessor") or "")
            if status in {"started", "processing"} and postprocessor in {"Merger", "FFmpegVideoRemuxer"}:
                job.mark_merging("Merging separate video and audio with FFmpeg…")
        except Exception:
            return

    def match_filter(info, *, incomplete=False):
        duration = info.get("duration")
        if duration and float(duration) > settings.max_duration_seconds:
            return "Video is longer than the maximum duration configured for this app."
        if info.get("is_live"):
            return "Live streams are not supported."
        return None

    options = {
        "outtmpl": str(job.workdir / "media.%(ext)s"),
        "format": selector,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "restrictfilenames": True,
        "overwrites": True,
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": settings.request_timeout_seconds,
        "max_filesize": settings.max_file_size_bytes,
        "match_filter": match_filter,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "http_headers": headers,
        "logger": _QuietYtdlpLogger(job),
        "proxy": settings.proxy_url or None,
        "cachedir": str(job.workdir / ".cache"),
    }
    cookie_path = job.ytdlp_cookie_path()
    if cookie_path:
        options["cookiefile"] = cookie_path
    if ffmpeg:
        options["ffmpeg_location"] = str(Path(ffmpeg).parent)
        options["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source.url, download=True)
        if isinstance(info, dict):
            title = info.get("title")
            if title:
                job.set_title(str(title))
    except FileTooLarge:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise classify_external_message(_strip_ansi(str(exc))) from exc
    except AppError:
        raise
    except Exception as exc:
        raise classify_external_message(str(exc)) from exc

    produced = _find_downloaded_file(job.workdir)
    if produced is None:
        raise NetworkError("yt-dlp reported success but no output file was found.")
    return produced


def _format_selector(source: Source) -> str:
    if not source.ytdlp_format_id:
        return "bv*+ba/b"
    format_id = source.ytdlp_format_id
    if source.ytdlp_video_only:
        return f"{format_id}+ba/b"
    return f"{format_id}/b"


def _find_downloaded_file(workdir: Path) -> Path | None:
    candidates: list[Path] = []
    for path in workdir.iterdir():
        if not path.is_file():
            continue
        if path.name.startswith(".") or path.suffix in {".part", ".ytdl", ".temp", ".json"}:
            continue
        if path.suffix in {".txt", ".cache"}:
            continue
        if path.stat().st_size <= 0:
            continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_size)


def _assert_manifest_safe(job, source: Source, settings: Settings) -> None:
    if source.drm:
        raise DRMProtected()
    headers = _request_headers(job, source, settings)
    headers.pop("Cookie", None)
    headers["Cookie"] = job.cookie_header_for(source.url)
    headers = {key: value for key, value in headers.items() if value}
    try:
        with _http_client(settings) as client:
            manifest = fetch_limited_sync(
                client,
                "GET",
                source.url,
                headers=headers,
                max_bytes=2 * 1024 * 1024,
                allow_private=settings.allow_private_hosts,
            )
    except AppError as exc:
        if exc.code == "blocked_url":
            raise
        job.log(f"Could not re-check the manifest: {exc.message}", level="warning")
        return

    if manifest.status in {401, 402}:
        raise LoginRequired()
    if manifest.status == 451:
        raise GeoBlocked()
    if manifest.status >= 400:
        raise NetworkError(
            f"The manifest returned HTTP {manifest.status}.",
            hint="The site may require cookies or fresh headers. Re-analyze the page and try again.",
        )
    info = scan_manifest(manifest.url, manifest.text)
    if info.drm:
        job.log(info.reason or "DRM metadata detected.", level="warning")
        raise DRMProtected()
    if info.duration and info.duration > settings.max_duration_seconds:
        raise DurationTooLong()
    if not source.duration:
        source.duration = info.duration


def _enforce_duration_limit(job, path: Path, settings: Settings, ffmpeg: str | None) -> None:
    ffprobe = find_ffprobe(ffmpeg)
    if not ffprobe:
        if job.get_selected_source() and job.get_selected_source().duration:
            duration = job.get_selected_source().duration
            if duration > settings.max_duration_seconds:
                raise DurationTooLong()
        return
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        duration = float((completed.stdout or "").strip())
    except Exception:
        return
    if duration > settings.max_duration_seconds:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise DurationTooLong(
            f"That video is {duration / 60:.0f} minutes long, above the configured {settings.max_duration_seconds // 60}-minute limit."
        )


def _remux_to_mp4(job, source: Path, settings: Settings, ffmpeg: str | None) -> Path:
    if not ffmpeg:
        job.log("FFmpeg is unavailable, so the original container is kept.", level="warning")
        return source
    job.mark_merging("Remuxing the file to MP4 without re-encoding…")
    target = job.workdir / "final.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        job.log(
            f"Could not remux to MP4 ({ (completed.stderr or 'unknown error').strip()[:200] }); keeping the original container.",
            level="warning",
        )
        return source
    return target


def _unique_filename(job, extension: str, settings: Settings) -> str:
    ext = extension.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,6}", ext):
        ext = ".mp4"
    title = job.title or "video"
    base = sanitize_filename(title, default="video", max_length=90)
    filename = f"{base}{ext}"
    destination = settings.download_dir / filename
    if destination.exists():
        filename = f"{base} {job.id[:6]}{ext}"
    return filename


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
