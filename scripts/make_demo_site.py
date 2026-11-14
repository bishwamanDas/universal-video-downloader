"""Generate a deterministic local demo site for the test plan.

Usage:
    python scripts/make_demo_site.py
    python -m http.server 9001 --directory demo

The generated pages exercise: direct MP4, HTML5 <video>, HLS, iframe parsing,
a JavaScript-only player, and a fake DRM manifest. FFmpeg must be installed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "demo"

DRM_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     type="static"
     mediaPresentationDuration="PT8S"
     minBufferTime="PT1.5S"
     profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
  <Period>
    <AdaptationSet mimeType="video/mp4" codecs="avc1.4d401f" width="1280" height="720">
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
      <Representation id="video" bandwidth="1200000" width="1280" height="720">
        <BaseURL>sample.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def find_ffmpeg() -> str | None:
    explicit = os.environ.get("FFMPEG_PATH", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which("ffmpeg")


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True)


def write_pages(out: Path) -> None:
    (out / "page_html5.html").write_text(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>HTML5 video fixture</title></head>
<body>
  <h1>HTML5 video fixture</h1>
  <p>A normal &lt;video&gt; element with a progressive MP4.</p>
  <video controls width="640" src="sample.mp4"></video>
</body></html>
""",
        encoding="utf-8",
    )

    (out / "player.html").write_text(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Iframe player fixture</title></head>
<body>
  <h1>Embedded player</h1>
  <video controls width="640" src="sample.mp4"></video>
</body></html>
""",
        encoding="utf-8",
    )

    (out / "page_iframe.html").write_text(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Iframe fixture</title></head>
<body>
  <h1>Iframe fixture</h1>
  <p>The playable video lives one iframe deep.</p>
  <iframe src="player.html" width="660" height="420" allow="autoplay; fullscreen"></iframe>
</body></html>
""",
        encoding="utf-8",
    )

    (out / "page_dynamic.html").write_text(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>JavaScript-only fixture</title></head>
<body>
  <h1>JavaScript-only fixture</h1>
  <p>The player only receives its media URL after a custom play control is clicked.</p>
  <button id="playButton" type="button">Watch sample</button>
  <video id="player" controls width="640"></video>
  <script>
    document.getElementById("playButton").addEventListener("click", function () {
      var parts = ["sam", "ple", ".mp4"];
      var video = document.getElementById("player");
      video.src = parts.join("");
      video.load();
      video.play().catch(function () {});
    });
  </script>
</body></html>
""",
        encoding="utf-8",
    )

    (out / "page_hls.html").write_text(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>HLS fixture</title></head>
<body>
  <h1>HLS fixture</h1>
  <video controls width="640" src="stream.m3u8"></video>
</body></html>
""",
        encoding="utf-8",
    )

    (out / "drm.html").write_text(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>DRM fixture</title></head>
<body>
  <h1>DRM fixture</h1>
  <p>This manifest contains ContentProtection and must be refused clearly.</p>
  <video controls width="640" src="drm.mpd"></video>
</body></html>
""",
        encoding="utf-8",
    )
    (out / "drm.mpd").write_text(DRM_MANIFEST, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("FFmpeg was not found. Install it and re-run this script.", file=sys.stderr)
        return 1

    sample = out / "sample.mp4"
    run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "8",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(sample),
        ]
    )

    run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(sample),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_playlist_type",
            "vod",
            "-hls_segment_filename",
            str(out / "seg%03d.ts"),
            str(out / "stream.m3u8"),
        ]
    )

    write_pages(out)
    print()
    print("Demo assets written to:", out)
    print("Serve them with:        python -m http.server 9001 --directory demo")
    print("Start the app with:     $env:UVD_ALLOW_PRIVATE_HOSTS=\"1\"; python run.py")
    print("(Localhost is blocked by default; the flag is only for the local demo.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
