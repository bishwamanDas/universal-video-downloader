# Universal Video Downloader

A local, four-tier video extractor. Paste a webpage or a direct media URL and the app tries progressively stronger extraction methods, then downloads the chosen MP4 with the browser's cookies and headers.

> **Permission first.** Only download content you own or have permission to download. The app refuses DRM-protected media and does not bypass logins, CAPTCHAs, paywalls, or access controls.

---

## What it does

| Tier | Method | Finds |
| --- | --- | --- |
| 0 | Direct response probe | `video/*`, `.mp4`, `.webm`, `.m3u8`, `.mpd` URLs |
| 1 | yt-dlp (generic extractor enabled) | Site formats and resolutions via yt-dlp |
| 2 | Static HTML parser | `<video>`, `<source>`, OG/Twitter video meta, JSON-LD `VideoObject`, one iframe level, media URLs in inline scripts |
| 3 | Playwright Chromium sniffer | Media requests and responses observed while a real headless browser plays the page, including extensionless media URLs, HLS/DASH manifests, custom play controls, lazy-loaded players, and JS-only pages |

The first tier that returns usable sources wins. The UI log shows exactly which tier found the video. Every source is listed with its kind, quality, extension, and size where known.

### Download engine

- Replays the browser's `User-Agent`, `Referer`, `Origin`, and cookies so hotlink/referer checks pass.
- Optional user-supplied **Netscape `cookies.txt`** for content the user can access with their own account.
- Direct HTTP downloads are streamed with `.part` files, Range/resume, exponential-backoff retries, and per-request timeouts.
- HLS/DASH downloads use yt-dlp, with an FFmpeg fallback. Separate video/audio streams are merged with `ffmpeg -c copy` (no re-encoding); non-MP4 containers are remuxed to MP4 when possible.
- Optional proxy, off by default.
- DRM detection from DASH `<ContentProtection>`, HLS key tags (`SAMPLE-AES`, Widevine/PlayReady/FairPlay key formats), and DRM license requests seen by the browser sniffer. The job stops with **"This video is DRM-protected and cannot be downloaded."**

### Production-ish behavior

- Async job queue with configurable download workers; jobs have IDs and states: `queued`, `analyzing`, `ready`, `downloading`, `merging`, `done`, `failed`.
- Live progress over Server-Sent Events (with polling fallback in the browser): bytes, speed, ETA, merge stage.
- SSRF protection: only HTTP(S), no embedded credentials, DNS resolution must yield public addresses only, every redirect is revalidated, and our own connections verify the peer address. Localhost/private ranges are blocked by default.
- Filename sanitization, per-job temp directories, TTL cleanup, structured JSON logging, maximum file size and duration limits.
- Error taxonomy: `no_video_found`, `drm`, `login_required`, `geo_blocked`, `timeout`, `network_error`, `too_large`, `too_long`, `blocked_url`, plus input/state errors.

---

## Requirements

- Python 3.11+ (developed and verified on Python 3.13)
- FFmpeg + ffprobe on `PATH` (needed for merging and MP4 output)
- Chromium downloaded for Playwright (`python -m playwright install chromium`)

### Windows setup

```powershell
cd "C:\Users\Asus\Downloads\video downloader"

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium

winget install Gyan.FFmpeg
# Open a new terminal after WinGet finishes so PATH is refreshed, then verify:
ffmpeg -version
```

If `ffmpeg` is installed somewhere unusual, set `UVD_FFMPEG_PATH` to the full executable path instead.

### macOS setup

```bash
cd "video downloader"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
brew install ffmpeg
ffmpeg -version
```

### Linux setup

```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv
cd "video downloader"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install --with-deps chromium
ffmpeg -version
```

`python3` must be 3.11 or newer.

---

## Run

One command after setup:

```bash
python run.py
```

Open <http://127.0.0.1:8000>. The page is served by FastAPI; there is no frontend build step. API docs are at <http://127.0.0.1:8000/api/docs>.

Without activating the venv, use:

- Windows: `.\.venv\Scripts\python.exe run.py`
- macOS/Linux: `./.venv/bin/python run.py`

---

## Configuration

All settings are environment variables. Defaults are safe for a local tool.

| Variable | Default | Purpose |
| --- | --- | --- |
| `UVD_HOST` | `127.0.0.1` | Bind address. Do not expose this app publicly |
| `UVD_PORT` | `8000` | HTTP port |
| `UVD_DOWNLOAD_DIR` | `./downloads` | Completed files |
| `UVD_DATA_DIR` | `./data` | Uploaded cookie files |
| `UVD_MAX_FILE_SIZE_MB` | `2048` | Hard download size cap |
| `UVD_MAX_DURATION_SECONDS` | `10800` | Duration cap where metadata is available, plus post-download ffprobe check |
| `UVD_JOB_TTL_SECONDS` | `3600` | Delete finished jobs/files after this many seconds |
| `UVD_DOWNLOAD_CONCURRENCY` | `2` | Parallel download workers |
| `UVD_ANALYZE_CONCURRENCY` | `2` | Parallel analyses (each browser sniff launches Chromium) |
| `UVD_PROXY_URL` | *(empty)* | Optional HTTP/SOCKS proxy for all engines |
| `UVD_PLAYWRIGHT_ENABLED` | `true` | Disable Tier 3 if desired |
| `UVD_HEADLESS` | `true` | Run Chromium headless |
| `UVD_SNIFF_TIMEOUT_SECONDS` | `15` | Browser capture window |
| `UVD_REQUEST_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `UVD_YTDLP_TIMEOUT_SECONDS` | `60` | Tier 1 timeout |
| `UVD_ALLOW_PRIVATE_HOSTS` | `false` | **Testing only.** Permits localhost/private demo URLs |
| `UVD_FFMPEG_PATH` | *(empty)* | Explicit FFmpeg executable |
| `UVD_USER_AGENT` | Chrome-like UA | User-Agent used by HTTP tiers |

Example:

```powershell
$env:UVD_MAX_FILE_SIZE_MB="1024"
$env:UVD_PROXY_URL="http://127.0.0.1:3128"
python run.py
```

---

## Cookies for your own account

Some videos are only available inside your own logged-in account. The app does not attempt to log in for you.

1. Sign in to the site in your normal browser.
2. Export cookies in **Netscape format** with a cookie-export extension.
3. Click **Attach cookies.txt** in the UI before clicking **Analyze**.
4. The cookie file is stored locally under `data/cookies/`, passed to yt-dlp, loaded into the headless browser, and replayed on media requests. It expires with the TTL cleanup.

Use this only for accounts and content you are authorized to access. Never share cookie files; they are credentials.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/analyze` | `{ "url": "...", "cookies_id": "..."? }` → job snapshot; analysis starts asynchronously |
| `POST` | `/api/jobs/{id}/download` | `{ "source_id": "..." }` → queues the selected source |
| `GET` | `/api/jobs/{id}` | Current job snapshot |
| `GET` | `/api/jobs/{id}/events` | SSE stream of snapshots until done/failed |
| `GET` | `/api/jobs/{id}/file` | Download the completed file (supports byte ranges) |
| `POST` | `/api/cookies` | Multipart `file` upload of a Netscape cookies.txt |
| `GET` | `/api/health` | Engine availability, limits, and version |
| `GET` | `/api/docs` | OpenAPI UI |

---

## Demo site and tests

Generate a deterministic local demo with FFmpeg:

```powershell
python scripts/make_demo_site.py
python -m http.server 9001 --directory demo
```

In a second terminal:

```powershell
$env:UVD_ALLOW_PRIVATE_HOSTS="1"
python run.py
```

Use the URLs in [docs/TEST_PLAN.md](docs/TEST_PLAN.md). The demo includes a direct MP4, HTML5 page, HLS stream, iframe page, a JavaScript-only player (for Tier 3), and a fake DRM manifest.

Unit tests (no network needed):

```bash
pip install -r requirements-dev.txt
pytest -q
```

The full test plan covers the five required URL types, expected tier/result, and evidence to collect.

---

## Security notes and limits

- **Do not expose this server to the internet.** There is no user authentication and any visitor could use it to make requests.
- SSRF protection validates every URL and redirect for our HTTP tiers and the browser route guard. yt-dlp and FFmpeg are external engines; all URLs handed to them are validated first, but they cannot be forced to pin a resolved IP. This is a documented best-effort boundary. Keep the app local and do not add an open proxy.
- DRM is detected and refused. No decryption keys are handled. Open AES-128 HLS (no key-system marker) is supported.
- CAPTCHA solving, paywall bypass, login bypass, and anti-bot evasion are not implemented. Attach your own cookies only for content you may access.
- Live streams are not supported (the download would be unbounded); size and duration caps protect the rest.
- Some sites use proprietary players, device attestation, or rotated signatures. yt-dlp can break when sites change. The log shows the failing tier and reason.
- Jobs, logs, and files are in memory/on local disk; restarting the server clears jobs. Files are deleted after the TTL.
- FFmpeg is required for merging separate streams and MP4 remuxing. Without it, downloads can still work but may remain in a different container and merging may fail.
- Strict CSP, no public CORS, security headers, request timeouts, file-size checks, and filename sanitization are enabled. Local-only deployment is still the primary boundary.

---

## Project tree

```text
video downloader/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI routes, SSE, static delivery, security headers
│   ├── config.py               # env-driven settings
│   ├── jobs.py                 # job store, queue, workers, TTL cleanup
│   ├── download.py             # direct/HLS/DASH/yt-dlp engine, retries, remux
│   ├── http_utils.py           # SSRF-safe redirecting fetch helpers
│   ├── security.py             # URL validation, filename sanitisation
│   ├── cookies.py              # Netscape parser and cookie replay
│   ├── errors.py               # public error taxonomy
│   ├── logging_conf.py         # structured JSON logs
│   ├── pipeline/
│   │   ├── analyze.py          # tier orchestration
│   │   ├── tier0_direct.py
│   │   ├── tier1_ytdlp.py
│   │   ├── tier2_html.py
│   │   ├── tier3_browser.py
│   │   ├── media_probe.py
│   │   ├── drm.py
│   │   └── sources.py
│   └── static/
│       ├── index.html
│       ├── styles.css
│       ├── app.js
│       └── favicon.svg
├── docs/
│   ├── TEST_PLAN.md
│   └── ARCHITECTURE_AND_INTERVIEW.md
├── scripts/
│   └── make_demo_site.py
├── tests/
│   └── test_core.py
├── run.py
├── requirements.txt
├── requirements-dev.txt
├── README.md
└── .gitignore
```

The architecture walkthrough and eight interview questions with answers are in [docs/ARCHITECTURE_AND_INTERVIEW.md](docs/ARCHITECTURE_AND_INTERVIEW.md).

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Executable doesn't exist` on browser tier | `python -m playwright install chromium` |
| No MP4 / merging fails | Install FFmpeg, open a new terminal, verify `ffmpeg -version`; or set `UVD_FFMPEG_PATH` |
| Tier 3 never runs | Check `UVD_PLAYWRIGHT_ENABLED`, install Chromium, read the engine log |
| A site returns HTTP 403 | The site may require your own cookies or reject automation; attach `cookies.txt` for your account and re-analyze |
| "No downloadable video found" | Check tier logs; the player may be DRM-protected, login-only, or use an unsupported/proprietary protocol |
| Port already in use | Set `UVD_PORT=8010` |
| Localhost demo blocked | Set `UVD_ALLOW_PRIVATE_HOSTS=1` only while running the local demo |
| File expired | Files are deleted after `UVD_JOB_TTL_SECONDS`; re-analyze |
