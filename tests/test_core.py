from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from app.cookies import (
    cookie_header_for,
    cookies_to_netscape,
    parse_netscape_text,
)
from app.errors import SSRFBlocked, classify_external_message
from app.pipeline.drm import looks_like_license_request, scan_dash_text, scan_hls_text
from app.pipeline.sources import Source, rank_sources
from app.pipeline.tier2_html import _candidates_from_html
from app.security import (
    make_safe_filename,
    normalize_and_validate_url,
    sanitize_filename,
)


def test_private_and_loopback_addresses_are_blocked():
    with pytest.raises(SSRFBlocked):
        normalize_and_validate_url("http://127.0.0.1/video.mp4")
    with pytest.raises(SSRFBlocked):
        normalize_and_validate_url("http://[::1]/video.mp4")
    with pytest.raises(SSRFBlocked):
        normalize_and_validate_url("http://192.168.1.10/video.mp4")


def test_only_http_schemes_and_no_credentials():
    with pytest.raises(SSRFBlocked):
        normalize_and_validate_url("file:///etc/passwd")
    with pytest.raises(SSRFBlocked):
        normalize_and_validate_url("https://user:pass@example.com/video")
    with pytest.raises(SSRFBlocked):
        normalize_and_validate_url("ftp://example.com/video.mp4")


def test_missing_scheme_defaults_to_https():
    url = normalize_and_validate_url("example.com/watch", allow_private=True)
    assert url == "https://example.com/watch"


def test_fragment_is_removed():
    url = normalize_and_validate_url("https://example.com/watch#chapter", allow_private=True)
    assert "#" not in url


def test_filename_sanitisation_blocks_traversal_and_windows_names():
    cleaned = sanitize_filename("../../etc/passwd")
    assert "/" not in cleaned and "\\" not in cleaned and ".." not in cleaned
    assert make_safe_filename("CON", ".mp4").startswith("_CON")
    assert make_safe_filename("", ".mp4") == "video.mp4"


def test_hls_aes128_is_allowed_but_sample_aes_is_drm():
    aes = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720
720/index.m3u8
"""
    info = scan_hls_text(aes)
    assert info.drm is False
    assert info.height == 720

    drm = """#EXTM3U
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key",KEYFORMAT="com.apple.streamingkeydelivery"
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720
720/index.m3u8
"""
    assert scan_hls_text(drm).drm is True


def test_dash_content_protection_is_drm():
    manifest = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
      <Period><AdaptationSet><ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/></AdaptationSet></Period>
    </MPD>"""
    assert scan_dash_text(manifest).drm is True


def test_license_heuristic():
    assert looks_like_license_request("https://example.com/widevine/license", "application/octet-stream")
    assert looks_like_license_request("https://example.com/license", "application/json")
    assert not looks_like_license_request("https://example.com/api/comments", "application/json")


def test_cookie_roundtrip_and_domain_matching():
    text = (
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tFALSE\t0\tsession\tabc123\n"
        "other.test\tFALSE\t/\tFALSE\t0\tsession\tzzz\n"
    )
    cookies = parse_netscape_text(text)
    assert len(cookies) == 2
    assert cookie_header_for(cookies, "https://www.example.com/watch") == "session=abc123"
    reparsed = parse_netscape_text(cookies_to_netscape(cookies))
    assert cookie_header_for(reparsed, "https://www.example.com/watch") == "session=abc123"


def test_source_public_view_hides_internal_headers():
    source = Source(
        url="https://cdn.example.com/v.mp4",
        kind="direct",
        tier=2,
        label="MP4",
        headers={"Referer": "https://example.com", "Cookie": "secret=1"},
    )
    public = source.to_public()
    assert "headers" not in public
    assert public["id"]
    assert public["tier_name"] == "HTML parser"


def test_ranking_prefers_manifest_quality():
    direct_low = Source(
        url="https://example.com/480.mp4",
        kind="direct",
        tier=2,
        label="480p MP4",
        height=480,
    )
    hls_high = Source(
        url="https://example.com/master.m3u8",
        kind="hls",
        tier=3,
        label="HLS 1080p",
        height=1080,
    )
    ranked = rank_sources([direct_low, hls_high])
    assert ranked[0] is hls_high


def test_external_error_taxonomy():
    assert classify_external_message("ERROR: This video is DRM protected").code == "drm"
    assert classify_external_message("ERROR: Sign in to confirm your age").code == "login_required"
    assert classify_external_message("ERROR: Video not available in your country").code == "geo_blocked"
    assert classify_external_message("ERROR: Read timed out").code == "timeout"


def test_static_html_meta_and_iframe_extraction():
    html = """<!doctype html><html><head>
      <meta property="og:video" content="https://cdn.example.com/video.mp4">
      <meta property="og:video:url" content="https://player.example.com/embed/abc">
    </head><body>
      <iframe src="/embed/frame"></iframe>
      <script>var fallback = "https:\\/\\/cdn.example.com\\/extra.mp4";</script>
    </body></html>"""
    soup = BeautifulSoup(html, "html.parser")
    candidates, iframes = _candidates_from_html(html, soup, "https://example.com/watch/1")
    urls = {candidate.url for candidate in candidates}
    assert "https://cdn.example.com/video.mp4" in urls
    assert "https://cdn.example.com/extra.mp4" in urls
    assert "https://player.example.com/embed/abc" in iframes
    assert "https://example.com/embed/frame" in iframes
