"""In-memory job store, analysis launcher, download workers, and TTL cleanup."""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cookies as cookie_utils
from .config import Settings
from .download import run_download
from .errors import AppError, InternalError, InvalidState, NotFoundError
from .pipeline import analyze as analyze_pipeline
from .pipeline.sources import TIER_NAMES, Source

logger = logging.getLogger("uvd.jobs")


@dataclass
class Job:
    id: str
    url: str
    workdir: Path
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "analyzing"
    stage: str = "Waiting for an analyzer slot"
    title: str | None = None
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: float = 0.0
    active_tier: int | None = None
    winning_tier: int | None = None
    logs: list[dict] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    source_map: dict[str, Source] = field(default_factory=dict)
    selected_source_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_hint: str | None = None
    flags: set[str] = field(default_factory=set)
    drm_hints: set[str] = field(default_factory=set)
    uploaded_cookies: list[cookie_utils.Cookie] = field(default_factory=list)
    browser_cookies: list[cookie_utils.Cookie] = field(default_factory=list)
    cookies_path: str | None = None
    output_path: str | None = None
    output_filename: str | None = None
    output_size: int | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def _touch(self) -> None:
        self.updated_at = time.time()

    def log(self, message: str, level: str = "info", tier: int | None = None) -> None:
        with self.lock:
            entry: dict = {
                "ts": round(time.time(), 3),
                "level": level,
                "message": str(message)[:2000],
            }
            if tier is not None:
                entry["tier"] = tier
                entry["tier_name"] = TIER_NAMES.get(tier, "engine")
            self.logs.append(entry)
            if len(self.logs) > 500:
                del self.logs[: len(self.logs) - 500]
            self._touch()

    def set_stage(self, stage: str) -> None:
        with self.lock:
            self.stage = stage
            self._touch()

    def set_active_tier(self, tier: int | None) -> None:
        with self.lock:
            self.active_tier = tier
            self._touch()

    def begin_tier(self, tier: int) -> None:
        self.set_active_tier(tier)
        self.set_stage(f"Tier {tier} · {TIER_NAMES.get(tier, 'engine')}")
        self.log(f"Starting: {TIER_NAMES.get(tier, 'engine')}", tier=tier)

    def set_title(self, title: str | None) -> None:
        if not title:
            return
        with self.lock:
            self.title = title.strip()[:200] or self.title
            self._touch()

    def set_sources(self, sources: list[Source], winning_tier: int) -> None:
        with self.lock:
            self.sources = sources
            self.source_map = {source.id: source for source in sources}
            self.selected_source_id = sources[0].id if sources else None
            self.winning_tier = winning_tier
            self.status = "ready"
            self.stage = f"{len(sources)} source(s) ready"
            self.active_tier = None
            self._touch()

    def get_selected_source(self) -> Source | None:
        with self.lock:
            return self.source_map.get(self.selected_source_id or "")

    def mark_queued(self, source: Source) -> None:
        with self.lock:
            self.selected_source_id = source.id
            self.status = "queued"
            self.stage = "Queued for a download worker"
            self.progress = 0.0
            self.downloaded_bytes = 0
            self.total_bytes = source.filesize
            self.speed_bps = 0.0
            self.error_code = None
            self.error_message = None
            self.error_hint = None
            self._touch()
        self.log(f"Queued: {source.label} (Tier {source.tier}).", tier=source.tier)

    def start_download(self, message: str = "Starting download") -> None:
        with self.lock:
            self.status = "downloading"
            self.stage = message
            self.progress = 0.0
            self.downloaded_bytes = 0
            self.speed_bps = 0.0
            self._touch()
        self.log(message)

    def update_progress(
        self,
        downloaded: int,
        total: int | None = None,
        speed: float | None = None,
        progress: float | None = None,
        stage: str | None = None,
    ) -> None:
        with self.lock:
            self.downloaded_bytes = max(0, int(downloaded))
            if total is not None and total > 0:
                self.total_bytes = int(total)
            if speed is not None:
                self.speed_bps = max(0.0, float(speed))
            if progress is None and self.total_bytes:
                self.progress = max(0.0, min(99.5, self.downloaded_bytes / self.total_bytes * 100))
            elif progress is not None:
                self.progress = max(0.0, min(99.5, float(progress)))
            if stage:
                self.stage = stage
            self._touch()

    def mark_merging(self, stage: str = "Merging streams with FFmpeg…") -> None:
        with self.lock:
            self.status = "merging"
            self.stage = stage
            self._touch()
        self.log(stage)

    def finish(self, path: Path, filename: str, size: int) -> None:
        with self.lock:
            self.status = "done"
            self.stage = "Ready to download"
            self.progress = 100.0
            self.output_path = str(path)
            self.output_filename = filename
            self.output_size = int(size)
            self.speed_bps = 0.0
            self.active_tier = None
            self._touch()
        self.log(f"Download complete: {filename}.", level="success")

    def fail(self, error: AppError | str) -> None:
        if isinstance(error, AppError):
            code, message, hint = error.code, error.message, error.hint
        else:
            code, message, hint = "internal_error", str(error), None
        with self.lock:
            self.status = "failed"
            self.stage = "Failed"
            self.error_code = code
            self.error_message = message
            self.error_hint = hint
            self.active_tier = None
            self._touch()
        self.log(message, level="error")

    def cookie_header_for(self, url: str) -> str:
        with self.lock:
            combined = list(self.uploaded_cookies) + list(self.browser_cookies)
        return cookie_utils.cookie_header_for(combined, url) if combined else ""

    def ytdlp_cookie_path(self) -> str | None:
        with self.lock:
            if self.cookies_path:
                return self.cookies_path
            if not self.browser_cookies:
                return None
            path = self.workdir / "browser-cookies.txt"
            cookie_utils.write_netscape_file(self.browser_cookies, path)
            return str(path)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "url": self.url,
                "status": self.status,
                "stage": self.stage,
                "title": self.title,
                "progress": round(self.progress, 2),
                "downloaded_bytes": self.downloaded_bytes,
                "total_bytes": self.total_bytes,
                "speed_bps": round(self.speed_bps, 1),
                "active_tier": self.active_tier,
                "winning_tier": self.winning_tier,
                "logs": [dict(entry) for entry in self.logs],
                "sources": [source.to_public() for source in self.sources],
                "selected_source_id": self.selected_source_id,
                "error": (
                    {
                        "code": self.error_code,
                        "message": self.error_message,
                        "hint": self.error_hint,
                    }
                    if self.error_code
                    else None
                ),
                "filename": self.output_filename,
                "file_size": self.output_size,
                "download_url": f"/api/jobs/{self.id}/file" if self.status == "done" else None,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task] = []
        self._background: list[asyncio.Task] = []
        self._analyze_sem: asyncio.Semaphore | None = None
        self._analysis_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._analyze_sem = asyncio.Semaphore(self.settings.analyze_concurrency)
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.settings.cookies_dir.mkdir(parents=True, exist_ok=True)
        for index in range(self.settings.download_concurrency):
            self._workers.append(asyncio.create_task(self._worker(index), name=f"download-worker-{index}"))
        self._background.append(asyncio.create_task(self._cleanup_loop(), name="ttl-cleanup"))
        logger.info(
            "Job manager started with %s download worker(s) and %s analyzer slot(s).",
            self.settings.download_concurrency,
            self.settings.analyze_concurrency,
        )

    async def stop(self) -> None:
        tasks = self._workers + self._background + list(self._analysis_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def create_job(
        self,
        url: str,
        *,
        uploaded_cookies: list[cookie_utils.Cookie] | None = None,
        cookies_path: str | None = None,
    ) -> Job:
        job_id = secrets.token_hex(8)
        workdir = self.settings.temp_dir / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        job = Job(
            id=job_id,
            url=url,
            workdir=workdir,
            uploaded_cookies=uploaded_cookies or [],
            cookies_path=cookies_path,
        )
        job.log(f"Job created for {url}")
        with self._lock:
            self._jobs[job_id] = job
        return job

    def start_analysis(self, job: Job) -> None:
        task = asyncio.create_task(self._analyze(job), name=f"analysis-{job.id}")
        self._analysis_tasks.add(task)
        task.add_done_callback(self._analysis_tasks.discard)

    async def _analyze(self, job: Job) -> None:
        assert self._analyze_sem is not None
        async with self._analyze_sem:
            if job.status != "analyzing":
                return
            job.set_stage("Analyzing")
            try:
                await analyze_pipeline.run_analysis(job, self.settings)
            except AppError as exc:
                job.fail(exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Analysis crashed for job %s", job.id)
                job.fail(InternalError("Unexpected error during analysis. Check the server log."))

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> dict | None:
        job = self.get(job_id)
        return job.snapshot() if job else None

    def request_download(self, job_id: str, source_id: str) -> dict:
        job = self.get(job_id)
        if not job:
            raise NotFoundError("That job does not exist.")
        with job.lock:
            if job.status != "ready":
                raise InvalidState(f"This job is {job.status}; it cannot start a download now.")
            source = job.source_map.get(source_id)
            if source is None:
                raise InvalidState("That source is not part of this job.")
            if source.drm:
                from .errors import DRMProtected

                raise DRMProtected()
            job.mark_queued(source)
        assert self._queue is not None
        self._queue.put_nowait(job.id)
        return job.snapshot()

    async def _worker(self, index: int) -> None:
        assert self._queue is not None
        while True:
            job_id = await self._queue.get()
            job = self.get(job_id)
            try:
                if job is None or job.status != "queued":
                    continue
                source = job.get_selected_source()
                if source is None:
                    job.fail(InvalidState("No source was selected for this download."))
                    continue
                try:
                    await asyncio.to_thread(run_download, job, source, self.settings)
                except AppError as exc:
                    job.fail(exc)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Download crashed for job %s", job.id)
                    job.fail(InternalError("Unexpected error while downloading. Check the server log."))
                finally:
                    if job.status not in {"done", "failed"}:
                        job.fail(InternalError("The download ended without a result."))
            finally:
                self._queue.task_done()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(45)
            try:
                await asyncio.to_thread(self._cleanup)
            except Exception:
                logger.exception("Cleanup pass failed")

    def _cleanup(self) -> None:
        now = time.time()
        ttl = self.settings.job_ttl_seconds
        with self._lock:
            jobs = list(self._jobs.values())

        referenced_workdirs: set[Path] = set()
        referenced_cookies: set[str] = set()
        for job in jobs:
            referenced_workdirs.add(job.workdir.resolve())
            if job.cookies_path:
                referenced_cookies.add(str(Path(job.cookies_path).resolve()))

        expired: list[Job] = []
        for job in jobs:
            if job.status in {"done", "failed", "ready"} and now - job.updated_at > ttl:
                expired.append(job)

        for job in expired:
            if job.output_path:
                try:
                    path = Path(job.output_path).resolve()
                    if path.is_file() and self.settings.download_dir.resolve() in path.parents:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
            shutil.rmtree(job.workdir, ignore_errors=True)
            with self._lock:
                self._jobs.pop(job.id, None)
            logger.info("Expired job %s", job.id)

        # Orphaned temp directories (for example after a crash).
        if self.settings.temp_dir.exists():
            for child in self.settings.temp_dir.iterdir():
                if not child.is_dir():
                    continue
                try:
                    if child.resolve() in referenced_workdirs:
                        continue
                    if now - child.stat().st_mtime > ttl:
                        shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    continue

        # Uploaded cookie files are shared between jobs; only remove old unused ones.
        if self.settings.cookies_dir.exists():
            for cookie_file in self.settings.cookies_dir.glob("*.txt"):
                try:
                    if str(cookie_file.resolve()) in referenced_cookies:
                        continue
                    if now - cookie_file.stat().st_mtime > ttl:
                        cookie_file.unlink(missing_ok=True)
                except OSError:
                    continue

        # Remove output files whose job metadata vanished.
        try:
            referenced_outputs = {
                str(Path(job.output_path).resolve()) for job in jobs if job.output_path
            }
            for child in self.settings.download_dir.glob("*.mp4"):
                if not child.is_file():
                    continue
                try:
                    if str(child.resolve()) in referenced_outputs:
                        continue
                    if now - child.stat().st_mtime > ttl * 2:
                        child.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass
