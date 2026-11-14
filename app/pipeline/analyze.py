"""Extraction tier orchestration: stop at the first tier that produces sources."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from ..config import Settings
from ..errors import (
    AppError,
    CaptchaBlocked,
    DRMProtected,
    GeoBlocked,
    LoginRequired,
    NoVideoFound,
    UpstreamTimeout,
)
from . import tier0_direct, tier1_ytdlp, tier2_html, tier3_browser
from .sources import TIER_NAMES, rank_sources

logger = logging.getLogger("uvd.analyze")

# Errors that mean "do not bother trying other tiers".
_HARD_STOP_CODES = {
    "drm",
    "too_large",
    "too_long",
    "blocked_url",
    "captcha",
}


def _tier_timeouts(settings: Settings) -> tuple[float, float, float, float]:
    return (
        30.0,
        max(10.0, settings.ytdlp_timeout_seconds),
        45.0,
        max(90.0, settings.sniff_timeout_seconds + 75.0),
    )


async def run_analysis(job, settings: Settings) -> None:
    job.log(f"Extraction started for {job.url}")
    tier_timeouts = _tier_timeouts(settings)
    timeout = httpx.Timeout(settings.request_timeout_seconds, read=45.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        proxy=settings.proxy_url or None,
        follow_redirects=False,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        for tier in range(4):
            job.set_stage(f"Tier {tier} · {TIER_NAMES[tier]}")
            job.set_active_tier(tier)
            started = time.monotonic()
            try:
                sources = await asyncio.wait_for(
                    _run_tier(tier, client, job, settings), timeout=tier_timeouts[tier]
                )
            except TimeoutError:
                job.log(
                    f"Tier {tier} timed out after {tier_timeouts[tier]:.0f}s; escalating.",
                    level="warning",
                    tier=tier,
                )
                job.flags.add("timeout")
                sources = []
            except AppError as exc:
                if isinstance(exc, CaptchaBlocked):
                    job.log(exc.message, level="error", tier=tier)
                    raise
                if exc.code in _HARD_STOP_CODES:
                    job.log(exc.message, level="error", tier=tier)
                    raise
                if tier == 0 and exc.code in {"login_required", "geo_blocked"}:
                    job.log(exc.message, level="error", tier=tier)
                    raise
                if isinstance(exc, LoginRequired):
                    job.flags.add("login")
                elif isinstance(exc, GeoBlocked):
                    job.flags.add("geo")
                elif exc.code == "timeout":
                    job.flags.add("timeout")
                job.log(f"Tier {tier} could not finish: {exc.message}", level="warning", tier=tier)
                sources = []
            except Exception:
                logger.exception("Tier %s crashed for job %s", tier, job.id)
                job.log(f"Tier {tier} hit an internal error; escalating.", level="warning", tier=tier)
                sources = []

            elapsed = time.monotonic() - started
            if sources:
                clean = [source for source in sources if not source.drm]
                if not clean:
                    raise DRMProtected()
                ranked = rank_sources(clean)
                if not job.title:
                    job.set_title(_title_from_url(job.url))
                job.set_active_tier(None)
                job.set_sources(ranked, winning_tier=tier)
                job.log(
                    f"Tier {tier} · {TIER_NAMES[tier]} found {len(ranked)} source(s) in {elapsed:.1f}s.",
                    level="success",
                    tier=tier,
                )
                return
            job.log(
                f"Tier {tier} · {TIER_NAMES[tier]} found nothing ({elapsed:.1f}s). Escalating.",
                tier=tier,
            )

    job.set_active_tier(None)
    if job.drm_hints:
        raise DRMProtected(
            hint="The player contacted a DRM license server. This tool does not bypass DRM protection."
        )
    if "captcha" in job.flags:
        raise CaptchaBlocked()
    if "login" in job.flags or "login_required" in job.flags:
        raise LoginRequired()
    if "geo" in job.flags or "geo_blocked" in job.flags:
        raise GeoBlocked()
    if "timeout" in job.flags:
        raise UpstreamTimeout("The site did not finish loading in time. Try again or check the URL.")
    hint = (
        "The page may build its player with JavaScript, require a login, or use formats this pipeline cannot access. "
        "Confirm the video plays in a normal browser, then try again or attach cookies.txt for your own account."
    )
    if "blocked" in job.flags:
        hint = "The site refused automated requests. Open it in your browser, confirm it plays, then attach cookies.txt if it is your account."
    raise NoVideoFound(
        "No downloadable video was found after all four extraction tiers.",
        hint=hint,
    )


async def _run_tier(tier: int, client: httpx.AsyncClient, job, settings: Settings):
    if tier == 0:
        return await tier0_direct.extract(client, job, settings)
    if tier == 1:
        return await asyncio.to_thread(tier1_ytdlp.extract_sync, job, settings)
    if tier == 2:
        return await tier2_html.extract(client, job, settings)
    if tier == 3:
        return await tier3_browser.extract(job, settings)
    return []


def _title_from_url(url: str) -> str:
    try:
        stem = Path(unquote(urlsplit(url).path)).stem
    except Exception:
        stem = ""
    return stem[:160] or "video"
