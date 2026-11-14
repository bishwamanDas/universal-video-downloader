"""URL validation (SSRF protection) and filename sanitisation.

SSRF model:
- Only http/https is accepted, embedded credentials are rejected.
- Hostnames are resolved and every resolved address must be globally routable.
- Redirects are followed manually by the HTTP helpers, with each hop revalidated.
- For our own HTTP requests the connected peer address is checked after connect,
  which closes the DNS-rebinding gap.
- External engines (yt-dlp/ffmpeg) cannot be forced to pin an IP. Every URL we
  hand them is validated first; this is documented as a limitation.
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import NetworkError, SSRFBlocked

_MEDIA_EXTENSIONS = {".mp4", ".webm", ".m3u8", ".mpd", ".mov", ".m4v", ".mkv", ".ogv", ".ts", ".m4s"}
_CONTENT_TYPE_EXT = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/ogg": ".ogv",
    "application/vnd.apple.mpegurl": ".m3u8",
    "application/x-mpegurl": ".m3u8",
    "audio/mpegurl": ".m3u8",
    "application/dash+xml": ".mpd",
}
_RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def normalize_and_validate_url(raw: str, *, allow_private: bool = False) -> str:
    """Normalise input and reject unsafe schemes/hosts. Raises AppError."""
    value = (raw or "").strip()
    if not value:
        raise SSRFBlocked("Enter a page or media URL.")
    if len(value) > 2048:
        raise SSRFBlocked("That URL is too long.")
    if any(ch.isspace() for ch in value):
        raise SSRFBlocked("URLs cannot contain spaces. Encode them with %20.")
    if "\\" in value:
        raise SSRFBlocked("Backslashes are not allowed in URLs.")
    if value.startswith("//"):
        value = "https:" + value
    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SSRFBlocked("That URL is not valid.") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise SSRFBlocked("Only http and https URLs are allowed.")
    if parsed.username or parsed.password:
        raise SSRFBlocked("URLs with embedded usernames or passwords are not allowed.")
    host = parsed.hostname
    if not host:
        raise SSRFBlocked("That URL has no hostname.")
    if len(host) > 253:
        raise SSRFBlocked("That hostname is too long.")

    if not allow_private:
        _assert_host_is_public(host, port)

    # Remove the fragment: it is never sent to servers and confuses probing.
    cleaned = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))
    return cleaned


def _assert_host_is_public(host: str, port: int | None) -> None:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        _assert_ip_public(literal)
        return

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SSRFBlocked("That hostname is not valid.") from exc

    try:
        results = socket.getaddrinfo(ascii_host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkError("Could not resolve that hostname.") from exc

    addresses = {result[4][0] for result in results}
    if not addresses:
        raise NetworkError("Could not resolve that hostname.")
    for address in addresses:
        try:
            _assert_ip_public(ipaddress.ip_address(address.split("%")[0]))
        except ValueError:
            raise SSRFBlocked("That hostname resolved to an invalid address.")


def _assert_ip_public(ip: ipaddress._BaseAddress) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        raise SSRFBlocked("That URL points to a local or private network address, which is blocked.")


def assert_connected_peer_public(response: httpx.Response) -> None:
    """Best-effort peer check to defeat DNS rebinding for our own requests."""
    try:
        stream = response.extensions.get("network_stream")
        peer = stream.get_extra_info("server_addr") if stream is not None else None
        if peer:
            _assert_ip_public(ipaddress.ip_address(str(peer[0]).split("%")[0]))
    except SSRFBlocked:
        raise
    except Exception:
        return


def hostname_is_public(url: str) -> bool:
    """Used by the browser route guard. Returns False on any failure."""
    try:
        normalize_and_validate_url(url, allow_private=False)
        return True
    except Exception:
        return False


def looks_like_media_url(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower()
    except Exception:
        return False
    return any(path.endswith(ext) for ext in _MEDIA_EXTENSIONS)


def media_kind_from_url(url: str) -> str | None:
    low = url.lower()
    try:
        path = urlsplit(url).path.lower()
    except Exception:
        path = low
    if ".m3u8" in path or "m3u8" in low:
        return "hls"
    if ".mpd" in path or "mpd" in low:
        return "dash"
    if any(path.endswith(ext) for ext in (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".ogv")):
        return "direct"
    return None


def extension_for_content_type(content_type: str | None, fallback_url: str = "") -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[ct]
    if ct.startswith("video/"):
        subtype = ct.split("/", 1)[1].split("+")[0]
        aliases = {"mpeg": ".mpeg", "x-msvideo": ".avi"}
        return aliases.get(subtype, "." + re.sub(r"[^a-z0-9]", "", subtype))
    suffix = Path(urlsplit(fallback_url).path).suffix.lower()
    if suffix in _MEDIA_EXTENSIONS:
        return suffix
    return ".mp4"


def parse_content_length(headers: httpx.Headers | dict) -> int | None:
    raw = headers.get("content-length") if headers else None
    if not raw:
        return None
    try:
        value = int(str(raw).split(",")[0].strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def parse_content_range_total(headers: httpx.Headers | dict) -> int | None:
    raw = headers.get("content-range") if headers else None
    if not raw:
        return None
    match = re.search(r"/(\d+)\s*$", str(raw))
    return int(match.group(1)) if match else None


def sanitize_filename(name: str, default: str = "video", max_length: int = 100) -> str:
    value = html.unescape(name or "").replace("\u200b", "")
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r'[<>:"/\\|?*]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = default
    stem = value.split(".")[0].strip().upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        value = "_" + value
    value = value[:max_length].rstrip(" .")
    return value or default


def make_safe_filename(title: str, extension: str, default: str = "video") -> str:
    ext = extension if extension.startswith(".") else "." + extension
    ext = re.sub(r"[^a-z0-9.]", "", ext.lower()) or ".mp4"
    base = sanitize_filename(title, default=default, max_length=120 - len(ext))
    return f"{base}{ext}"
