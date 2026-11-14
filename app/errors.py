"""Typed errors and mapping of external tool messages to the public taxonomy."""

from __future__ import annotations


class AppError(Exception):
    """An error that is safe to show to the user."""

    code = "error"
    status_code = 400

    def __init__(self, message: str, *, hint: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        if status_code is not None:
            self.status_code = status_code


class SSRFBlocked(AppError):
    code = "blocked_url"
    status_code = 400


class InvalidRequest(AppError):
    code = "invalid_request"
    status_code = 400


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class InvalidState(AppError):
    code = "invalid_state"
    status_code = 409


class InvalidCookies(AppError):
    code = "invalid_cookies"
    status_code = 400


class NoVideoFound(AppError):
    code = "no_video_found"
    status_code = 422


class DRMProtected(AppError):
    code = "drm"
    status_code = 422

    def __init__(self, message: str | None = None, *, hint: str | None = None):
        super().__init__(
            message or "This video is DRM-protected and cannot be downloaded.",
            hint=hint
            or "The page uses Widevine, PlayReady, or FairPlay. This tool does not bypass DRM.",
        )


class LoginRequired(AppError):
    code = "login_required"
    status_code = 422

    def __init__(self, message: str | None = None, *, hint: str | None = None):
        super().__init__(
            message or "This page needs a signed-in session before the video is available.",
            hint=hint
            or "Export cookies.txt from a browser where you are signed in to your own account, then attach it before analyzing.",
        )


class CaptchaBlocked(LoginRequired):
    def __init__(self, message: str | None = None):
        super().__init__(
            message or "A CAPTCHA or bot check is blocking this page.",
            hint="Open the page in a normal browser, solve the challenge for your own session, then export cookies.txt and attach it.",
        )


class GeoBlocked(AppError):
    code = "geo_blocked"
    status_code = 422

    def __init__(self, message: str | None = None, *, hint: str | None = None):
        super().__init__(
            message or "This video is not available from your region.",
            hint=hint or "The site blocks this network or country. A proxy can be configured if you are allowed to access the content.",
        )


class UpstreamTimeout(AppError):
    code = "timeout"
    status_code = 504


class NetworkError(AppError):
    code = "network_error"
    status_code = 502


class FileTooLarge(AppError):
    code = "too_large"
    status_code = 413


class DurationTooLong(AppError):
    code = "too_long"
    status_code = 422


class UnsupportedMedia(AppError):
    code = "unsupported"
    status_code = 422


class InternalError(AppError):
    code = "internal_error"
    status_code = 500


def classify_external_message(message: str) -> AppError:
    """Map messages from yt-dlp/ffmpeg/HTTP into the public error taxonomy."""
    text = (message or "").strip()
    low = text.lower()

    if "drm" in low or "protected by widevine" in low or "protected by playready" in low or "fairplay" in low:
        return DRMProtected()
    if "captcha" in low or "cf-challenge" in low or "verify you are human" in low:
        return CaptchaBlocked()
    if any(token in low for token in ("sign in", "log in", "login required", "authentication", "private video", "members-only")):
        return LoginRequired()
    if any(token in low for token in ("not available in your country", "not available in this country", "geo restriction", "geo-restricted", "blocked in your region")):
        return GeoBlocked()
    if "max-filesize" in low or "larger than the maximum" in low or "file is larger" in low:
        return FileTooLarge("The video is larger than the configured size limit.")
    if "maximum duration" in low or "longer than the maximum" in low:
        return DurationTooLong()
    if "timed out" in low or "timeout" in low:
        return UpstreamTimeout("The site took too long to respond.")
    if "unsupported url" in low:
        return NoVideoFound("No downloadable video was found at that URL.")
    return NetworkError(text[:500] if text else "The download failed because of a network or site error.")
