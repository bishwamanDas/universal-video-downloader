# Architecture and Interview Guide

## The system in one page

Universal Video Downloader is a single local web application. FastAPI serves the HTML/CSS/JS page and a small JSON API. The browser never talks to the video site; the server does. That is what lets the app replay cookies and request headers and run a headless browser.

A user action creates a **job**. Analysis runs asynchronously in the background while the page reconnects to it through Server-Sent Events. The job moves through `analyzing → ready → queued → downloading → merging → done` (or `failed`). Every transition and every tier attempt is appended to a job log, so the UI can show which extraction method found the video.

The extraction pipeline is deliberately ordered from cheapest to most expensive:

1. **Tier 0** makes a tiny Range request to the submitted URL. If the response is a video file, an HLS manifest, or a DASH manifest, it is already the answer. Manifests are downloaded as text to parse quality variants and to look for DRM.
2. **Tier 1** asks yt-dlp to extract metadata, with its generic extractor enabled. yt-dlp knows many site-specific extractors; the generic extractor handles ordinary pages. It returns formats, resolution, size, and duration. The job can stop here.
3. **Tier 2** fetches the page with `httpx` and parses static HTML: `<video>`, `<source>`, OpenGraph/Twitter video metadata, JSON-LD `VideoObject`, one level of iframes, and media-extension URLs found in inline scripts. Candidate URLs are probed, manifests are checked for DRM, and the survivors are ranked.
4. **Tier 3** launches headless Chromium through Playwright. It blocks images/fonts for speed, guards routes against private addresses, dismisses common consent banners, repeatedly triggers play controls (including custom overlays), scrolls to trigger lazy loading, and listens to both requests and responses in every frame. It captures video content types, extensionless media requests, and HLS/DASH manifests, plus the cookies and request headers used by the page. After a configurable ~15-second window it ranks what it saw and returns sources. This is what finds players that assemble their stream in JavaScript.

The first tier that produces usable, non-DRM sources wins. That keeps latency and resource use predictable: a direct MP4 is finished in milliseconds; a hostile JS page escalates to a real browser only when needed.

Once the user picks a source and clicks **Download MP4**, the job enters a bounded worker queue. The worker downloads on a background thread (yt-dlp's Python API and the direct HTTP streamer are synchronous). Direct files support Range resume and retries. HLS/DASH are handled by yt-dlp with FFmpeg as a fallback. Separate audio and video streams are merged with `ffmpeg -c copy`, and non-MP4 containers are remuxed, normally without re-encoding. The worker verifies size and duration, sanitises the filename, moves the file to `downloads/`, and marks the job done.

Security boundaries are enforced on every request: only HTTP(S), no embedded credentials, DNS results must be public, every redirect is revalidated, and our own connections check the peer IP. Playwright requests are guarded per host. External tools are only ever handed URLs that already passed validation. The app is for local use only.

Important modules: `app/pipeline/analyze.py` (tier loop), `app/pipeline/tier*.py` (extractors), `app/pipeline/drm.py` (protection detection), `app/download.py` (transfer and merge), `app/jobs.py` (queue, status, TTL), `app/security.py` (SSRF/filename), `app/main.py` (API/SSE), and `app/static/app.js` (polling fallback and UI state).

---

## Eight likely interview questions

### 1. Walk me through what happens from pasting a URL to a finished MP4.

The frontend POSTs `/api/analyze`. The API normalises and validates the URL, loads any attached Netscape cookie file, creates a job, and starts an async analysis task. The manager limits concurrent analyses with a semaphore so multiple Chromium launches cannot exhaust the machine.

The analysis task opens one shared HTTP client and runs the tiers in order. Each tier returns `Source` objects. The first non-empty result is deduplicated and ranked; the job becomes `ready` with the sources. The UI receives the whole job snapshot over SSE (or polling if SSE fails). The log contains each tier's start and outcome.

When the user picks a source, `/api/jobs/{id}/download` validates that the job is `ready`, stores the selected source, sets `queued`, and pushes the job ID into an `asyncio.Queue`. A worker picks it up and offloads the synchronous engine to a thread. The engine chooses the direct streamer, yt-dlp, or the manifest path, updates progress through thread-safe job methods, merges/remuxes, enforces limits, and moves the result into `downloads/`. SSE sends the download progress snapshots and the final `done` state, which includes a download URL. TTL cleanup deletes the file later.

### 2. Why four tiers, and why stop at the first success? What are the trade-offs?

Each tier has a different cost/coverage ratio. A direct response is essentially free. yt-dlp is fast but only knows what its extractors and generic parser can see. Static HTML parsing is cheap but misses JS players. A browser is powerful but slow (seconds, hundreds of MB of RAM) and can trigger anti-bot defenses. Escape only when the cheaper method failed.

Stopping at the first success makes behavior deterministic and keeps a simple request from launching Chromium. The trade-off is quality: a page might expose a low-bitrate MP4 that yt-dlp finds in Tier 1 while Tier 3 could observe a 1080p manifest. A "collect all tiers and rank" mode would be better for quality but spends the resources anyway and changes the demo story. The code keeps this decision in one place—`run_analysis`—so it can be changed without touching the engines. Ranking already prefers manifests and higher resolutions within a tier.

### 3. How do you make downloads work on sites with hotlink protection or login requirements?

The server replays context, not just the URL. For the browser tier, Playwright captures the actual cookies and the request headers used for media responses (User-Agent, Referer, Origin, Accept). Those headers are stored on the source; the cookies are stored on the job. When downloading, `_request_headers` merges them and computes a scoped Cookie header for the exact host, so cookies are not leaked to unrelated domains. For yt-dlp, cookies are written to a Netscape file and passed as `cookiefile`; the header set is passed as `http_headers`. FFmpeg gets the same context through `-user_agent` and a `-headers` block.

A user can attach their own `cookies.txt` for their own logged-in account. That file is parsed once, loaded into the Playwright context, replayed on HTTP requests, and given to yt-dlp. This is not login bypass: it only reuses a session the user already legitimately has. If the site still returns 401/403, the job fails with `login_required` or a clear network error and a hint to attach cookies.

### 4. How do jobs and live progress work, and what happens under load?

Jobs live in an in-memory store with a per-job lock. Analysis concurrency is capped by a semaphore; downloads by a fixed number of worker tasks consuming an `asyncio.Queue`. Each worker is one job at a time. Downloads run in threads so the event loop stays responsive; progress updates take a lock before mutating bytes/speed/status.

SSE snapshots are generated by a loop that reads a consistent copy of the job every 400 ms and sends it only if it changed, with keep-alive comments. The client also has a polling fallback if EventSource fails. Because the queue absorbs bursts, the API never blocks on a download. Statuses are explicit (`queued`, `analyzing`, `ready`, `downloading`, `merging`, `done`, `failed`), so the UI can render accurate states instead of guessing.

At real scale I would swap the in-memory store and queue for Redis plus durable workers, add authentication and per-user quotas, add cancellation and backpressure, move analysis/downloads into separate process pools, and stream progress through a message bus. The current design has no cross-process resume and loses jobs on restart, which is acceptable for a local tool.

### 5. How do you prevent SSRF, and where are the limits?

Every URL is normalised and checked before use: only HTTP(S), no `user:pass`, hostname required. For non-test mode the hostname is resolved and **all** A/AAAA results must be globally routable; loopback, private, link-local, CGNAT, and reserved ranges are rejected. Redirects are followed manually, validating each hop. For our HTTP client I also inspect the connected socket via httpx's `network_stream` extension after connect, which closes the DNS-rebinding gap. The Playwright route guard resolves each new host and aborts requests to private addresses; non-HTTP schemes are blocked. The Direct downloader repeats the validation.

The honest limitation is external engines: yt-dlp and FFmpeg may follow redirects or fetch subresources internally. We validate the URL we hand them and all sources before handing them over, but cannot force third-party binaries to pin an IP or enforce a full egress policy. The correct production answer is also network-level egress filtering plus process sandboxing and never exposing this service publicly. Local-only is part of the security model.

### 6. How are HLS/DASH handled and merged without re-encoding? How is DRM detected?

Manifests are treated as first-class sources. During analysis we fetch the manifest text, parse HLS variants (`RESOLUTION`, `BANDWIDTH`) or DASH representations, and estimate duration. For downloads we prefer yt-dlp because it already handles variant selection, segmented fetching, AES-128 keys, retries, and fragment concurrency. If yt-dlp fails, FFmpeg is the fallback: it reads the manifest, fetches the segments, maps the first video and audio streams, and copies them. `merge_output_format` plus `-c copy` combines separate video/audio tracks without transcoding. A final remux to MP4 uses `-c copy` and `+faststart`, so the operation is fast and lossless. Direct HTTP files use a custom stream writer with `.part` resume.

DRM is detected before download and during analysis. DASH is rejected if a `<ContentProtection>` element exists. HLS is rejected for `SAMPLE-AES` or key formats/UUIDs for Widevine, PlayReady, or FairPlay. Plain `AES-128` with an identity/no key format is allowed because it is openly keyed. The browser sniffer also watches for license-request URLs. If all candidates are protected, the pipeline stops and returns the exact message "This video is DRM-protected and cannot be downloaded." No decryption code exists in the project.

### 7. How do you handle restrictions—DRM, CAPTCHAs, logins, paywalls, and geo-blocks?

Restrictions are explicit outcomes, not obstacles to defeat. DRM is detected and refused. CAPTCHA/anti-bot signals produce `login_required` with a CAPTCHA-specific message. A 401 or a login-wall signal produces `login_required`. Geo hints and HTTP 451 produce `geo_blocked`. 403s are reported with context. The UI suggests attaching cookies for the user's own account; it never solves a challenge or bypasses a paywall.

This is both an engineering and an ethical boundary. A downloader for arbitrary pages is useful for content you own or are permitted to save; a DRM circumvention tool is a different product with legal exposure. Refusing early also makes the failure deterministic: no retry loop hammering a protected endpoint, no browser automation trying to click through a block. The error taxonomy and hints make failures actionable without pretending the app can do the impossible. For geo-blocking, a user who is allowed to use a proxy can configure one; the app does not attempt to evade access controls.

### 8. What can't you download, and why? What would you improve for production?

It cannot download DRM-protected video (Widevine/PlayReady/FairPlay playback requires licensed decryption), CAPTCHA-gated or login-only content without the user's own cookies, paywalled content, content the user is not authorized to access, or live streams (unbounded duration and different lifecycle). It also cannot guarantee success on sites with proprietary players, device attestation, app-only protocols, rotating signatures, or bot defenses; yt-dlp support can break when a site changes. Some pages expose only preview clips to anonymous clients. Duration is only enforced when metadata exists, with a final ffprobe check; if no metadata/ffprobe exists, the hard size cap protects the disk. In-memory jobs, one process, no auth, and a local disk make it unsuitable for public deployment as-is.

For production I would isolate the extraction and download engines in sandboxed workers with strict outbound network policy and resource limits; use a durable queue and database; add authenticated users, quotas, rate limiting, cancellation, and signed object-storage delivery; run a pool of Playwright browsers; add observability (structured logs, metrics, traces, per-tier success rates); and build a broader automated test corpus, including fixture sites for each tier and DRM failure. I would keep the same security posture: only URLs validated against public address space, no DRM circumvention, and explicit errors for anything that requires the user's own authorized session.
