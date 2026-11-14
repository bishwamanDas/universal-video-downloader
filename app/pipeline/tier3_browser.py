"""Tier 3: headless Chromium network sniffer.

Loads the page, dismisses common consent overlays, triggers playback, and
watches every response in every frame for media content types and HLS/DASH
manifests. Cookies and request headers are captured so the download engine can
replay the browser's request context.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .. import cookies as cookie_utils
from ..config import Settings
from ..errors import AppError, DRMProtected, DurationTooLong
from ..http_utils import base_headers, fetch_limited
from ..security import extension_for_content_type, normalize_and_validate_url
from .drm import apply_manifest_info, looks_like_license_request, scan_manifest
from .media_probe import probe_media
from .sources import Source, rank_sources

_HLS_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
}
_DASH_TYPES = {"application/dash+xml"}
_RESOLUTION_RE = re.compile(r"(2160|1440|1080|720|480|360|240)p", re.IGNORECASE)

_PLAY_SELECTORS = (
    "button[aria-label*='play' i]",
    "button[title*='play' i]",
    ".vjs-big-play-button",
    ".jw-display-icon-container",
    ".play-button",
    "[class*='playButton']",
    "[class*='play-icon']",
)
_CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept all')",
    "button:has-text('Accept cookies')",
    "button:has-text('I agree')",
    "button:has-text('Got it')",
    "[aria-label*='accept' i]",
    "[aria-label*='consent' i]",
)

_DOM_VIDEO_JS = """
() => {
  const found = [];
  for (const video of document.querySelectorAll('video')) {
    const urls = [];
    if (video.currentSrc) urls.push(video.currentSrc);
    if (video.src) urls.push(video.src);
    for (const source of video.querySelectorAll('source')) {
      if (source.src) urls.push(source.src);
    }
    for (const url of urls) {
      found.push({url, width: video.videoWidth || 0, height: video.videoHeight || 0});
    }
  }
  return found;
}
"""

_PLAYING_JS = """
() => Array.from(document.querySelectorAll('video'))
  .some(v => !v.paused && !v.ended && v.readyState >= 2)
"""

_GENERIC_PLAY_JS = """
() => {
  const bad = /(subscribe|sign in|log in|login|trial|rent|buy|purchase|download|share|trailer)/i;
  const nodes = Array.from(document.querySelectorAll(
    'button, [role="button"], a, [class*="play" i], [data-testid*="play" i], [aria-label]'
  ));
  for (const node of nodes.slice(0, 1600)) {
    const aria = node.getAttribute('aria-label') || '';
    const title = node.getAttribute('title') || '';
    const testid = node.getAttribute('data-testid') || '';
    const klass = typeof node.className === 'string' ? node.className : '';
    const text = (node.textContent || '').trim().slice(0, 80);
    const label = [aria, title, testid, klass, text].join(' ');
    if (!/(^|[^a-z])(play|watch|start)([^a-z]|$)/i.test(label) || bad.test(label)) continue;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8 || style.visibility === 'hidden' ||
        style.display === 'none' || style.pointerEvents === 'none') continue;
    node.click();
    return true;
  }
  return false;
}
"""

_GENERIC_FRAME_PLAY_JS = """
() => {
  const nodes = Array.from(document.querySelectorAll(
    'button, [role="button"], [class*="play" i], [data-testid*="play" i], [aria-label]'
  ));
  for (const node of nodes.slice(0, 800)) {
    const label = [
      node.getAttribute('aria-label') || '',
      node.getAttribute('title') || '',
      node.getAttribute('data-testid') || '',
      typeof node.className === 'string' ? node.className : '',
      (node.textContent || '').trim().slice(0, 80)
    ].join(' ');
    if (!/(^|[^a-z])(play|watch|start)([^a-z]|$)/i.test(label) ||
        /(subscribe|sign in|log in|login|trial|rent|buy)/i.test(label)) continue;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    if (rect.width < 8 || rect.height < 8 || style.display === 'none') continue;
    node.click();
    return true;
  }
  return false;
}
"""


@dataclass
class Sniffed:
    url: str
    kind: str
    content_type: str
    headers: dict[str, str]
    filesize: int | None = None
    height: int | None = None
    page_url: str = ""
    raw_headers: dict[str, str] = field(default_factory=dict)


async def extract(job, settings: Settings) -> list[Source]:
    if not settings.playwright_enabled:
        job.log("Browser sniffing is disabled by configuration.", level="warning", tier=3)
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        job.log(
            "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium",
            level="warning",
            tier=3,
        )
        return []

    job.begin_tier(3)
    job.log("Launching headless Chromium and watching media requests and responses…", tier=3)
    sniffed: dict[str, Sniffed] = {}
    license_hits: list[str] = []
    page_url = job.url
    page_title: str | None = None
    dom_videos: list[dict] = []

    async with async_playwright() as playwright:
        launch_kwargs: dict = {
            "headless": settings.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
            ],
        }
        if settings.proxy_url:
            launch_kwargs["proxy"] = {"server": settings.proxy_url}
        try:
            browser = await playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            text = str(exc)
            hint = ""
            if "Executable doesn't exist" in text or "looks like Playwright" in text:
                hint = " Install Chromium with: python -m playwright install chromium"
            job.log(f"Could not launch Chromium: {text[:300]}{hint}", level="warning", tier=3)
            return []

        try:
            context = await browser.new_context(
                user_agent=settings.user_agent,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                ignore_https_errors=True,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            if job.uploaded_cookies:
                try:
                    await context.add_cookies(cookie_utils.cookies_for_playwright(job.uploaded_cookies))
                except Exception as exc:
                    job.log(f"Could not load your cookies into Chromium: {exc}", level="warning", tier=3)

            host_cache: dict[str, bool] = {}

            async def route_guard(route):
                request = route.request
                if request.resource_type in {"image", "font"}:
                    await route.abort()
                    return
                url = request.url
                try:
                    parsed = urlsplit(url)
                except Exception:
                    await route.abort()
                    return
                if parsed.scheme in {"data", "blob", "about"}:
                    await route.continue_()
                    return
                if parsed.scheme not in {"http", "https"}:
                    await route.abort()
                    return
                host = parsed.hostname or ""
                allowed = host_cache.get(host)
                if allowed is None:
                    if settings.allow_private_hosts:
                        allowed = True
                    else:
                        try:
                            await asyncio.to_thread(normalize_and_validate_url, url)
                            allowed = True
                        except Exception:
                            allowed = False
                    host_cache[host] = allowed
                if not allowed:
                    await route.abort()
                    return
                await route.continue_()

            await context.route("**/*", route_guard)
            page = await context.new_page()
            page_url = page.url

            async def on_response(response):
                try:
                    raw_headers = await _headers_dict(response)
                    content_type = raw_headers.get("content-type", "").split(";")[0].strip().lower()
                    url = response.url
                    if not url.lower().startswith(("http://", "https://")):
                        return
                    if looks_like_license_request(url, content_type):
                        if url not in license_hits:
                            license_hits.append(url)
                        return
                    if _is_segment_url(url) or content_type in {"video/mp2t", "video/iso.segment"} or content_type.startswith("audio/"):
                        sniffed.pop(url, None)
                        return
                    kind = _classify_response(url, content_type)
                    if kind is None:
                        try:
                            request_type = response.request.resource_type
                        except Exception:
                            request_type = ""
                        if request_type == "media" and content_type in {"", "application/octet-stream", "binary/octet-stream"}:
                            kind = "direct"
                    if kind is None:
                        return
                    request_headers = await _headers_dict(response.request)
                    cleaned = _clean_request_headers(request_headers)
                    size = _size_from_response(response.status, raw_headers)
                    height = _height_from_url(url)
                    existing = sniffed.get(url)
                    if existing is None:
                        sniffed[url] = Sniffed(
                            url=url,
                            kind=kind,
                            content_type=content_type,
                            headers=cleaned,
                            filesize=size,
                            height=height,
                            page_url=page_url,
                            raw_headers=raw_headers,
                        )
                    else:
                        existing.kind = kind
                        existing.content_type = content_type or existing.content_type
                        if cleaned:
                            existing.headers = {**existing.headers, **cleaned}
                        existing.filesize = size or existing.filesize
                        existing.height = height or existing.height
                        existing.raw_headers = raw_headers
                        if page_url and not page_url.startswith("about:"):
                            existing.page_url = page_url
                except Exception:
                    # Sniffing must never break the page load.
                    return

            async def on_request(request):
                try:
                    url = request.url
                    if not url.lower().startswith(("http://", "https://")):
                        return
                    request_headers = await _headers_dict(request)
                    if looks_like_license_request(url, request_headers.get("content-type", "")):
                        if url not in license_hits:
                            license_hits.append(url)
                        return
                    if _is_segment_url(url):
                        return
                    kind = _classify_url(url)
                    if kind is None and request.resource_type != "media":
                        return
                    if kind is None:
                        kind = "direct"
                    cleaned = _clean_request_headers(request_headers)
                    existing = sniffed.get(url)
                    if existing is None:
                        sniffed[url] = Sniffed(
                            url=url,
                            kind=kind,
                            content_type="",
                            headers=cleaned,
                            filesize=None,
                            height=_height_from_url(url),
                            page_url=page_url,
                        )
                    else:
                        if cleaned:
                            existing.headers = {**existing.headers, **cleaned}
                        if existing.kind == "direct" and kind in {"hls", "dash"}:
                            existing.kind = kind
                except Exception:
                    return

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                await page.goto(job.url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                job.log(f"Page load warning: {str(exc).splitlines()[0][:240]}", level="warning", tier=3)

            try:
                await page.bring_to_front()
            except Exception:
                pass

            try:
                page_title = (await page.title()).strip() or None
                if page_title:
                    job.set_title(page_title)
            except Exception:
                pass

            try:
                current_url = page.url or ""
                if current_url and not current_url.startswith("about:"):
                    page_url = current_url
                else:
                    page_url = job.url
            except Exception:
                page_url = job.url
            for item in sniffed.values():
                if not item.page_url or item.page_url.startswith("about:"):
                    item.page_url = page_url

            await _dismiss_overlays(page, job)
            await _click_play(page, job)

            start = time.monotonic()
            timeout = max(6.0, float(settings.sniff_timeout_seconds))
            activation_schedule = [3.0, 7.0, 11.0]
            scroll_schedule = [2.5, 6.5]
            activation_index = 0
            scroll_index = 0
            while True:
                await asyncio.sleep(0.5)
                elapsed = time.monotonic() - start
                while activation_index < len(activation_schedule) and elapsed >= activation_schedule[activation_index]:
                    await _click_play(page, job)
                    await _click_play_in_frames(page, job)
                    activation_index += 1
                while scroll_index < len(scroll_schedule) and elapsed >= scroll_schedule[scroll_index]:
                    try:
                        await page.mouse.wheel(0, 600)
                        await page.wait_for_timeout(250)
                    except Exception:
                        pass
                    scroll_index += 1
                has_manifest = any(item.kind in {"hls", "dash"} for item in sniffed.values())
                if has_manifest and elapsed >= min(7.0, timeout):
                    break
                if sniffed and elapsed >= min(9.0, timeout):
                    break
                if elapsed >= timeout:
                    break

            try:
                page_url = page.url or page_url
            except Exception:
                pass
            for frame in page.frames:
                try:
                    found = await frame.evaluate(_DOM_VIDEO_JS)
                except Exception:
                    continue
                if isinstance(found, list):
                    for item in found:
                        if isinstance(item, dict) and item.get("url"):
                            dom_videos.append({**item, "page_url": frame.url})

            try:
                raw_cookies = await context.cookies()
                job.browser_cookies = cookie_utils.cookies_from_playwright(raw_cookies)
            except Exception:
                pass
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    if page_title:
        job.set_title(page_title)
    if license_hits:
        for hint in license_hits[:3]:
            job.drm_hints.add(hint)
        job.log(
            f"Saw {len(license_hits)} request(s) that look like DRM license traffic.",
            level="warning",
            tier=3,
        )

    job.log(
        f"Captured {len(sniffed)} media candidate(s) from requests and responses across all frames.",
        tier=3,
    )

    all_candidates = list(sniffed.values())
    await _probe_unknown_directs(job, settings, all_candidates, page_url)
    sources: list[Source] = []
    drm_reasons: list[str] = []
    manifest_candidates = [item for item in all_candidates if item.kind in {"hls", "dash"}]
    direct_candidates = [item for item in all_candidates if item.kind == "direct"]
    manifest_budget = 8

    for item in manifest_candidates[:12]:
        if manifest_budget <= 0:
            break
        manifest_budget -= 1
        source, reason = await _build_manifest_source(job, settings, item)
        if source is not None:
            sources.append(source)
        if reason:
            drm_reasons.append(reason)

    for item in direct_candidates[:14]:
        ext = extension_for_content_type(item.content_type, item.url)
        if ext in {".ts", ".m4s"}:
            continue
        quality = f"{item.height}p" if item.height else ""
        match = _RESOLUTION_RE.search(item.url)
        if not item.height and match:
            quality = f"{int(match.group(1))}p"
        label = f"{ext.lstrip('.').upper()} · browser capture"
        if quality:
            label += f" · {quality}"
        headers = dict(item.headers)
        headers.setdefault("Referer", item.page_url or page_url)
        if "User-Agent" not in headers:
            headers["User-Agent"] = settings.user_agent
        headers.setdefault("Accept", "*/*")
        source = Source(
            url=item.url,
            kind="direct",
            tier=3,
            label=label,
            method="direct",
            quality=quality,
            height=item.height or (int(match.group(1)) if match else None),
            ext=ext,
            filesize=item.filesize,
            headers=headers,
            page_url=item.page_url or page_url,
        )
        sources.append(source)

    for item in dom_videos:
        url = str(item.get("url") or "")
        if not url.lower().startswith(("http://", "https://")):
            continue
        if any(source.url == url for source in sources):
            continue
        kind = _classify_url(url)
        if kind is None:
            continue
        height = int(item.get("height") or 0) or _height_from_url(url)
        headers = base_headers(settings.user_agent, referer=item.get("page_url") or page_url)
        source = Source(
            url=url,
            kind=kind,
            tier=3,
            label=("HLS · " if kind == "hls" else "Progressive · ") + (f"{height}p" if height else "browser"),
            method="ytdlp" if kind in {"hls", "dash"} else "direct",
            quality=f"{height}p" if height else "",
            height=height or None,
            ext="mp4",
            filesize=None,
            headers=headers,
            page_url=item.get("page_url") or page_url,
        )
        sources.append(source)

    if sources:
        sources = rank_sources(sources)
        job.log(
            f"Browser sniffer produced {len(sources)} usable source(s). Best: {sources[0].label}.",
            level="success",
            tier=3,
        )
        return sources

    if drm_reasons:
        job.log(drm_reasons[0], level="warning", tier=3)
        raise DRMProtected()
    if license_hits:
        raise DRMProtected(
            hint="The player contacted a DRM license server. This tool does not bypass DRM protection."
        )
    job.log("No downloadable media requests or responses were observed.", level="warning", tier=3)
    return []


async def _probe_unknown_directs(job, settings: Settings, candidates: list[Sniffed], page_url: str) -> None:
    """Extensionless media requests have no content type yet; probe a few to qualify them."""
    unknown = [item for item in candidates if item.kind == "direct" and not item.content_type][:3]
    if not unknown:
        return
    job.log(f"Probing {len(unknown)} extensionless media request(s) captured by the browser.", tier=3)
    timeout = httpx.Timeout(min(settings.request_timeout_seconds, 10.0), read=15.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        proxy=settings.proxy_url or None,
        follow_redirects=False,
    ) as client:
        for item in unknown:
            headers = dict(item.headers)
            headers.setdefault("Referer", item.page_url or page_url or job.url)
            headers.setdefault("User-Agent", settings.user_agent)
            headers.setdefault("Accept", "*/*")
            cookie_header = job.cookie_header_for(item.url)
            if cookie_header:
                headers["Cookie"] = cookie_header
            try:
                probe = await probe_media(client, item.url, headers, settings)
            except Exception:
                continue
            if probe.kind in {"hls", "dash"}:
                item.kind = probe.kind
            elif probe.kind == "direct":
                if probe.content_type.startswith("audio/") or _is_segment_url(probe.url):
                    item.kind = "skip"
                    continue
                item.url = probe.url
                item.content_type = probe.content_type or item.content_type
                item.filesize = probe.filesize or item.filesize
            elif probe.content_type.startswith(("text/html", "application/json")):
                item.kind = "skip"
            if not item.height:
                item.height = _height_from_url(probe.url)


async def _build_manifest_source(
    job, settings: Settings, item: Sniffed
) -> tuple[Source | None, str | None]:
    headers = dict(item.headers)
    headers.setdefault("Referer", item.page_url or job.url)
    if "User-Agent" not in headers:
        headers["User-Agent"] = settings.user_agent
    cookie_header = job.cookie_header_for(item.url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            proxy=settings.proxy_url or None,
            follow_redirects=False,
        ) as client:
            manifest = await fetch_limited(
                client,
                "GET",
                item.url,
                headers=headers,
                max_bytes=2 * 1024 * 1024,
                allow_private=settings.allow_private_hosts,
            )
    except AppError as exc:
        job.log(f"Could not inspect a manifest: {exc.message}", level="warning", tier=3)
        return None, None

    if manifest.status >= 400:
        return None, None
    info = scan_manifest(manifest.url, manifest.text)
    if info.drm:
        job.log(info.reason or "A manifest declares DRM.", level="warning", tier=3)
        return None, info.reason or "DRM manifest detected."
    if info.duration and info.duration > settings.max_duration_seconds:
        raise DurationTooLong(
            f"That video is {info.duration / 60:.0f} minutes long, above the configured {settings.max_duration_seconds // 60}-minute limit."
        )

    safe_headers = {key: value for key, value in headers.items() if key.lower() != "cookie"}
    source = Source(
        url=manifest.url,
        kind=item.kind,
        tier=3,
        label="HLS · adaptive" if item.kind == "hls" else "DASH · adaptive",
        method="ytdlp",
        quality=f"{item.height}p" if item.height else "",
        height=item.height,
        ext="mp4",
        filesize=None,
        headers=safe_headers,
        page_url=item.page_url or job.url,
        duration=info.duration,
    )
    apply_manifest_info(source, info)
    return source, None


async def _dismiss_overlays(page, job) -> None:
    for selector in _CONSENT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                await locator.click(timeout=800)
                job.log("Dismissed a consent overlay.", tier=3)
                break
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _click_play(page, job) -> None:
    try:
        playing = bool(await page.evaluate(_PLAYING_JS))
    except Exception:
        playing = False

    clicked = False
    if not playing:
        for selector in _PLAY_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=1000, force=True)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            try:
                clicked = bool(await page.evaluate(_GENERIC_PLAY_JS))
            except Exception:
                clicked = False
        if not clicked:
            try:
                video = page.locator("video").first
                if await video.count() and await video.is_visible():
                    await video.scroll_into_view_if_needed(timeout=1000)
                    box = await video.bounding_box()
                    if box:
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        clicked = True
            except Exception:
                pass

    try:
        await page.evaluate(
            "() => { document.querySelectorAll('video').forEach(v => { try { v.muted = true; v.play().catch(() => {}); } catch (e) {} }); }"
        )
    except Exception:
        pass
    if clicked:
        job.log("Triggered a custom play control or video surface.", tier=3)


async def _click_play_in_frames(page, job) -> None:
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            playing = bool(await frame.evaluate(_PLAYING_JS))
            if playing:
                continue
            clicked = False
            for selector in _PLAY_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() and await locator.is_visible():
                        await locator.click(timeout=700, force=True)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                try:
                    clicked = bool(await frame.evaluate(_GENERIC_FRAME_PLAY_JS))
                except Exception:
                    clicked = False
            await frame.evaluate(
                "() => { let played = false; document.querySelectorAll('video').forEach(v => { try { v.muted = true; const p = v.play(); if (p) p.catch(() => {}); played = true; } catch (e) {} }); return played; }"
            )
            if clicked:
                job.log("Triggered playback inside an iframe.", tier=3)
                return
        except Exception:
            continue


async def _headers_dict(target) -> dict[str, str]:
    headers: dict[str, str] = {}
    try:
        raw = target.headers
        if asyncio.iscoroutine(raw):
            raw = await raw
        if isinstance(raw, dict):
            headers = {str(key).lower(): str(value) for key, value in raw.items()}
    except Exception:
        headers = {}
    if not headers:
        try:
            raw = await target.all_headers()
            headers = {str(key).lower(): str(value) for key, value in raw.items()}
        except Exception:
            headers = {}
    return headers


def _clean_request_headers(headers: dict[str, str]) -> dict[str, str]:
    keep = {"user-agent", "referer", "origin", "accept", "accept-language"}
    cleaned = {key: value for key, value in headers.items() if key.lower() in keep}
    if "user-agent" not in cleaned:
        return {}
    return {
        "User-Agent": cleaned["user-agent"],
        **({"Referer": cleaned["referer"]} if "referer" in cleaned else {}),
        **({"Origin": cleaned["origin"]} if "origin" in cleaned else {}),
        **({"Accept": cleaned["accept"]} if "accept" in cleaned else {}),
        **({"Accept-Language": cleaned["accept-language"]} if "accept-language" in cleaned else {}),
    }


def _is_segment_url(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower()
    except Exception:
        return False
    if path.endswith((".ts", ".m4s", ".m4a", ".aac", ".mp3", ".vtt", ".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return True
    return path.endswith("init.mp4") or "/init-" in path


def _classify_response(url: str, content_type: str) -> str | None:
    path = urlsplit(url).path.lower()
    if content_type in {"video/mp2t", "video/iso.segment"} or path.endswith((".ts", ".m4s")):
        return None
    if path.endswith("init.mp4") or "/init-" in path:
        return None
    if content_type in _HLS_TYPES or ".m3u8" in path:
        return "hls"
    if content_type in _DASH_TYPES or ".mpd" in path:
        return "dash"
    if content_type.startswith("audio/"):
        return None
    if content_type.startswith("video/"):
        return "direct"
    if path.endswith((".mp4", ".webm", ".mov", ".mkv")):
        return "direct"
    return None


def _classify_url(url: str) -> str | None:
    path = urlsplit(url).path.lower()
    if ".m3u8" in path:
        return "hls"
    if ".mpd" in path:
        return "dash"
    if path.endswith((".mp4", ".webm", ".mov", ".mkv")):
        return "direct"
    return None


def _size_from_response(status: int, headers: dict[str, str]) -> int | None:
    content_range = headers.get("content-range", "")
    match = re.search(r"/(\d+)\s*$", content_range)
    if match:
        return int(match.group(1))
    if status not in {206, 304}:
        try:
            value = int(headers.get("content-length", ""))
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def _height_from_url(url: str) -> int | None:
    match = _RESOLUTION_RE.search(url)
    return int(match.group(1)) if match else None
