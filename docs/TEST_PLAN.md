# Test Plan

## Purpose

Verify that the pipeline finds media on five representative URL types, downloads it, and refuses DRM content with a clear message. The local demo is deterministic and avoids third-party changes.

## Preconditions

1. Complete the setup in `README.md`; confirm `ffmpeg -version` and `python -m playwright install chromium`.
2. Generate the demo assets (requires FFmpeg):

   ```powershell
   python scripts/make_demo_site.py
   ```

3. Serve the demo in one terminal:

   ```powershell
   python -m http.server 9001 --directory demo
   ```

4. Start the app in another terminal, allowing private hosts **for this local demo only**:

   ```powershell
   $env:UVD_ALLOW_PRIVATE_HOSTS="1"
   python run.py
   ```

5. Open `http://127.0.0.1:8000`.

Localhost is blocked by default as part of SSRF protection. The flag exists only so the demo can run on `127.0.0.1`.

## Required URL cases

| # | Type | URL | Expected pipeline result | Expected user result |
| --- | --- | --- | --- | --- |
| 1 | Direct `.mp4` | `http://127.0.0.1:9001/sample.mp4` | **Tier 0** finds `video/mp4` directly | One "Progressive MP4" source; download finishes; file plays with picture and sound |
| 2 | Page with HTML5 `<video>` | `http://127.0.0.1:9001/page_html5.html` | **Tier 1** (yt-dlp generic) or **Tier 2** (HTML parser) finds `sample.mp4` | At least one source; download finishes; MP4 plays |
| 3 | HLS stream | `http://127.0.0.1:9001/stream.m3u8` | **Tier 0** recognises the manifest, fetches it, parses variants, sees no DRM | An "HLS" source (the demo playlist is a single media playlist, so quality may show as adaptive); yt-dlp downloads and FFmpeg muxes to MP4; file plays |
| 4 | Page with an iframe player | `http://127.0.0.1:9001/page_iframe.html` | **Tier 1, 2, or 3**; Tier 2 specifically follows one iframe level. To force **Tier 3**, use `http://127.0.0.1:9001/page_dynamic.html` (the media URL is assembled by JS and only appears after clicking the custom "Watch sample" button) | At least one source; download finishes. For the dynamic page the log must show Tier 3 capturing `sample.mp4`, including a "custom play control" log line |
| 5 | DRM page | `http://127.0.0.1:9001/drm.html` (or direct `http://127.0.0.1:9001/drm.mpd`) | Manifest contains `<ContentProtection>`; detection raises the DRM error. No download is attempted | Job status **failed**, message exactly **"This video is DRM-protected and cannot be downloaded."**; no source can be selected; no file is produced |

### Step-by-step checks

For each non-DRM case:

1. Paste the URL and press **Analyze**.
2. Watch the tier strip and engine log; record which tier succeeded (the log prints `Tier N · … found X source(s)`).
3. Confirm the source row shows the type, quality (where applicable), size (where known), and tier.
4. Select the source and press **Download MP4**.
5. Confirm status transitions through queued/downloading (and merging if applicable) to **done**.
6. Press **Save file**; confirm `ffprobe -v error -show_entries stream=codec_type -of csv=p=0 <file>` reports both `video` and `audio` for the demo sample/HLS output.
7. Confirm the progress panel showed bytes and speed, and the log shows which tier found the video.

For the DRM case:

1. Analyze `drm.html` (or `drm.mpd`).
2. Expect a failed job with the exact DRM message and the code `DRM`.
3. Confirm no source cards and no download link.
4. Confirm the server log contains a line about the manifest's DRM protection or `<ContentProtection>`.

## Additional negative checks

| Check | How | Expected |
| --- | --- | --- |
| SSRF block | Stop the app, unset `UVD_ALLOW_PRIVATE_HOSTS`, start it, analyze `http://127.0.0.1:9001/sample.mp4` | HTTP 400 / code `blocked_url`; no request made |
| Unsupported scheme | Analyze `file:///C:/Windows/notepad.exe` | HTTP 400; only http/https allowed |
| Unknown page | Analyze a page with no video, e.g. `https://example.com` | Failed job code `no_video_found`, hint explains login/JS/DRM possibilities |
| Cookies upload | Upload a valid Netscape `cookies.txt` | UI shows cookie count attached; the next job receives it |
| Size limit | Set `UVD_MAX_FILE_SIZE_MB=1`, analyze the 8s demo | `too_large` failure during analysis or download; no final file |
| Timeouts | Temporarily set `UVD_REQUEST_TIMEOUT_SECONDS=1` on a slow site | Timeout is logged; job ends with `timeout` or a clear network error |
| TTL cleanup | Set `UVD_JOB_TTL_SECONDS=60`, finish a download, wait ~2 minutes | Job disappears, file and temp directory removed |

## Optional public URLs

Use these only where you have permission and where the site's terms allow it. They change over time.

| Type | Suggested URL | Notes |
| --- | --- | --- |
| Direct MP4 | `https://www.w3schools.com/html/mov_bbb.mp4` | Small public sample; Tier 0 |
| HTML5 page | `https://www.w3schools.com/html/html5_video.asp` | Tier 1 or Tier 2 |
| HLS | `https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8` | Public test stream; no DRM; may be slow |
| Iframe/JS player | Any page you control with a one-level iframe or JS player | Use your own fixture to avoid depending on third-party markup |
| DRM | Use your own manifest with `<ContentProtection>` (the demo) | Do not attempt to obtain keys or bypass protection |

## Expected log signatures

- Tier 0 success: `Found a direct media file…` or `Found a HLS manifest…` / `Found an HLS stream.`
- Tier 1 success: `yt-dlp found N downloadable format(s). Best: …`
- Tier 2 success: `Static HTML found N usable source(s). Best: …`
- Tier 3 success: `Browser sniffer produced N usable source(s). Best: …` and `Captured N media response(s) across all frames.`
- DRM: `This video is DRM-protected and cannot be downloaded.` with `code=drm`.

## Pass criteria

- All five required cases behave as described.
- The winning tier is visible in both the log and the source chip.
- Non-DRM downloads produce a playable file containing video.
- The DRM case produces no file and the exact refusal message.
- SSRF, filename, and cookie unit tests pass (`pytest -q`).
