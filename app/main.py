"""FastAPI application: API, SSE progress, file delivery, and SPA hosting."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import load_settings
from .cookies import load_netscape_file, parse_netscape_text
from .download import find_ffmpeg, find_ffprobe
from .errors import AppError, InvalidCookies, NotFoundError
from .jobs import JobManager
from .logging_conf import setup_logging
from .security import normalize_and_validate_url

logger = logging.getLogger("uvd.api")
_COOKIE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = load_settings()
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.cookies_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    manager = JobManager(settings)
    await manager.start()
    app.state.settings = settings
    app.state.manager = manager
    if not find_ffmpeg(settings):
        logger.warning(
            "FFmpeg was not found. Direct MP4 downloads will work, but HLS/DASH merging and MP4 remuxing may fail."
        )
    logger.info("Universal Video Downloader ready at http://%s:%s", settings.host, settings.port)
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(
    title="Universal Video Downloader",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    cookies_id: str | None = Field(default=None, max_length=64)


class DownloadRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=64)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.url.path != "/api/docs":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code, "hint": exc.hint},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", []) if item != "body")
        parts.append(f"{location}: {error.get('msg', 'invalid value')}" if location else error.get("msg", "invalid value"))
    return JSONResponse(status_code=422, content={"detail": "; ".join(parts) or "Invalid request."})


def _manager(request: Request) -> JobManager:
    return request.app.state.manager


def _settings(request: Request):
    return request.app.state.settings


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).resolve().parent / "static" / "index.html")


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).resolve().parent / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/health")
async def health(request: Request):
    settings = _settings(request)
    ffmpeg = find_ffmpeg(settings)
    try:
        ytdlp_version = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        ytdlp_version = None
    return {
        "status": "ok",
        "version": app.version,
        "engine": {
            "yt_dlp": ytdlp_version,
            "ffmpeg": bool(ffmpeg),
            "ffmpeg_path": ffmpeg,
            "ffprobe": bool(find_ffprobe(ffmpeg)),
            "playwright_package": _module_available("playwright"),
            "playwright_enabled": settings.playwright_enabled,
        },
        "limits": {
            "max_file_size_mb": settings.max_file_size_mb,
            "max_duration_seconds": settings.max_duration_seconds,
            "job_ttl_seconds": settings.job_ttl_seconds,
            "download_concurrency": settings.download_concurrency,
        },
        "notice": "Only download content you own or have permission to download.",
    }


@app.post("/api/cookies")
async def upload_cookies(request: Request, file: UploadFile = File(...)):
    settings = _settings(request)
    contents = await file.read(settings.max_cookies_bytes + 1)
    if len(contents) > settings.max_cookies_bytes:
        raise InvalidCookies(f"Cookies files are limited to {settings.max_cookies_bytes // 1024} KB.")
    try:
        text = contents.decode("utf-8", errors="replace")
    except Exception as exc:
        raise InvalidCookies("The cookies file could not be read.") from exc
    parsed = parse_netscape_text(text)
    if not parsed:
        raise InvalidCookies(
            "That does not look like a Netscape cookies.txt file.",
            hint="Use a browser extension that exports Netscape-format cookies for a site and account you own.",
        )
    cookie_id = uuid.uuid4().hex
    path = settings.cookies_dir / f"{cookie_id}.txt"
    path.write_text(text, encoding="utf-8")
    return {"cookies_id": cookie_id, "cookie_count": len(parsed), "filename": file.filename}


@app.post("/api/analyze")
async def analyze(request: Request, payload: AnalyzeRequest):
    settings = _settings(request)
    url = await asyncio.to_thread(
        normalize_and_validate_url,
        payload.url,
        allow_private=settings.allow_private_hosts,
    )
    uploaded = None
    cookies_path = None
    if payload.cookies_id:
        if not _COOKIE_ID_RE.match(payload.cookies_id):
            raise InvalidCookies("That cookies reference is not valid.")
        cookies_path = str(settings.cookies_dir / f"{payload.cookies_id}.txt")
        if not Path(cookies_path).is_file():
            raise InvalidCookies("That cookies file is no longer available. Upload it again.")
        uploaded = await asyncio.to_thread(load_netscape_file, cookies_path)
    job = _manager(request).create_job(url, uploaded_cookies=uploaded, cookies_path=cookies_path)
    _manager(request).start_analysis(job)
    return job.snapshot()


@app.post("/api/jobs/{job_id}/download")
async def start_download(request: Request, job_id: str, payload: DownloadRequest):
    snapshot = _manager(request).request_download(job_id, payload.source_id)
    return snapshot


@app.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    snapshot = _manager(request).snapshot(job_id)
    if snapshot is None:
        raise NotFoundError("That job does not exist or has expired.")
    return snapshot


@app.get("/api/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str):
    manager = _manager(request)
    if manager.snapshot(job_id) is None:
        raise NotFoundError("That job does not exist or has expired.")

    async def event_stream():
        yield "retry: 1500\n\n"
        last_payload: str | None = None
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            snapshot = manager.snapshot(job_id)
            if snapshot is None:
                break
            payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 25:
                    yield ": keep-alive\n\n"
                    idle_ticks = 0
            if snapshot["status"] in {"done", "failed"}:
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/jobs/{job_id}/file")
async def download_file(request: Request, job_id: str):
    manager = _manager(request)
    settings = _settings(request)
    job = manager.get(job_id)
    if job is None:
        raise NotFoundError("That job does not exist or has expired.")
    with job.lock:
        output_path = job.output_path
        filename = job.output_filename
        status = job.status
    if status != "done" or not output_path:
        raise AppError("That download is not ready yet.", status_code=409)
    path = Path(output_path).resolve()
    try:
        path.relative_to(settings.download_dir.resolve())
    except ValueError as exc:
        raise AppError("Invalid download path.", status_code=403) from exc
    if not path.is_file():
        raise NotFoundError("The file has expired.")
    media_type = "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename or path.name)


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False
