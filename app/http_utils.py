"""HTTP helpers with manual redirect following and SSRF revalidation.

Two versions are provided because analysis is async and the download engine
runs in worker threads.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from .errors import NetworkError, SSRFBlocked
from .security import (
    assert_connected_peer_public,
    normalize_and_validate_url,
    parse_content_length,
    parse_content_range_total,
)

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass
class FetchResult:
    status: int
    url: str
    headers: httpx.Headers
    body: bytes
    truncated: bool = False

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        encoding = "utf-8"
        raw_type = self.headers.get("content-type") or ""
        if "charset=" in raw_type:
            encoding = raw_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(encoding, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def base_headers(user_agent: str, *, referer: str | None = None, accept: str = "*/*") -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def origin_for(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


async def fetch_limited(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    max_bytes: int,
    allow_private: bool,
    max_redirects: int = 5,
) -> FetchResult:
    """Fetch a URL, validating every redirect and capping the body size."""
    current = url
    for _ in range(max_redirects + 1):
        current = await _validate(current, allow_private)
        request = client.build_request(method, current, headers=headers)
        try:
            response = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise NetworkError("The site took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise NetworkError("Could not connect to the site.") from exc

        if not allow_private:
            assert_connected_peer_public(response)

        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                return FetchResult(response.status_code, current, response.headers, b"")
            current = urljoin(current, location)
            continue

        body = bytearray()
        truncated = False
        try:
            async for chunk in response.aiter_bytes(65536):
                if not chunk:
                    continue
                remaining = max_bytes - len(body)
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    body.extend(chunk[:remaining])
                    truncated = True
                    break
                body.extend(chunk)
        except httpx.TimeoutException as exc:
            raise NetworkError("The site stopped sending data.") from exc
        except httpx.HTTPError as exc:
            raise NetworkError("The connection failed while reading the response.") from exc
        finally:
            await response.aclose()
        return FetchResult(response.status_code, current, response.headers, bytes(body), truncated)

    raise NetworkError("Too many redirects.")


async def _validate(url: str, allow_private: bool) -> str:
    try:
        return await asyncio.to_thread(normalize_and_validate_url, url, allow_private=allow_private)
    except (SSRFBlocked, NetworkError):
        raise
    except Exception as exc:
        raise NetworkError("That URL could not be loaded.") from exc


def fetch_limited_sync(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    max_bytes: int,
    allow_private: bool,
    max_redirects: int = 5,
) -> FetchResult:
    """Thread-safe equivalent of fetch_limited for the download engine."""
    current = url
    for _ in range(max_redirects + 1):
        current = normalize_and_validate_url(current, allow_private=allow_private)
        request = client.build_request(method, current, headers=headers)
        try:
            response = client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise NetworkError("The site took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise NetworkError("Could not connect to the site.") from exc

        if not allow_private:
            assert_connected_peer_public(response)

        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("location")
            response.close()
            if not location:
                return FetchResult(response.status_code, current, response.headers, b"")
            current = urljoin(current, location)
            continue

        body = bytearray()
        truncated = False
        try:
            for chunk in response.iter_bytes(65536):
                if not chunk:
                    continue
                remaining = max_bytes - len(body)
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    body.extend(chunk[:remaining])
                    truncated = True
                    break
                body.extend(chunk)
        except httpx.TimeoutException as exc:
            raise NetworkError("The site stopped sending data.") from exc
        except httpx.HTTPError as exc:
            raise NetworkError("The connection failed while reading the response.") from exc
        finally:
            response.close()
        return FetchResult(response.status_code, current, response.headers, bytes(body), truncated)

    raise NetworkError("Too many redirects.")


def total_size_from_headers(headers: httpx.Headers) -> int | None:
    ranged = parse_content_range_total(headers)
    if ranged is not None:
        return ranged
    return parse_content_length(headers)
