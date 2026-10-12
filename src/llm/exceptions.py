"""
Custom exception classes for LLM provider interactions.

This module defines a structured hierarchy of exceptions to allow for more
granular error handling, particularly for implementing intelligent retry mechanisms.
"""

class LLMProviderError(Exception):
    """Base class for all custom LLM provider errors."""
    pass

class FatalError(LLMProviderError):
    """
    Represents a non-recoverable error.

    These errors should not be retried, as the request is guaranteed to fail
    again (e.g., invalid API key, malformed request, insufficient quota).
    The operation should be aborted immediately, and the error should be
    reported to the user.
    """
    pass

class RetryableError(LLMProviderError):
    """
    Represents a temporary, potentially recoverable error.

    These errors indicate a transient issue, such as a server-side problem
    (5xx errors), rate limiting (429) or a temporary network failure.
    Operations that raise this error can be safely retried, ideally with
    an exponential backoff strategy.

    `retry_after` (seconds) optionally carries the server-suggested wait
    (Retry-After header / RetryInfo); the worker honors it instead of the
    blind backoff when present.
    """
    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def extract_retry_after(response=None, message: str = "") -> float | None:
    """Best-effort server-suggested wait in seconds.

    Checks, in order: `retry-after` / `retry-after-ms` response headers,
    then message patterns like 'retry in 54s', "retryDelay: '54s'".
    Returns None when nothing usable is found.
    """
    import re
    try:
        headers = getattr(response, "headers", None) or {}
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
        for key in ("retry-after", "retry-after-ms"):
            if key in lowered:
                value = float(str(lowered[key]).strip().rstrip("s"))
                if "ms" in key:
                    value /= 1000.0
                if value > 0:
                    return value
    except Exception:
        pass
    try:
        match = re.search(r"retry\s*(?:in|after)?\s*:?\s*(\d+(?:\.\d+)?)\s*s", str(message), re.IGNORECASE)
        if match:
            value = float(match.group(1))
            if value > 0:
                return value
    except Exception:
        pass
    return None


#: HTTP statuses that mean "try again later" at every provider
#: (529 is Anthropic-specific overload).
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})


def is_transient_error(exc: BaseException) -> bool:
    """True when an arbitrary SDK/HTTP exception looks transient.

    Needed because SDKs raise their own classes (e.g. google.genai
    ClientError/ServerError instead of google.api_core exceptions), so
    `except SpecificError` blocks silently miss them. Matches on status
    codes (429/5xx incl. Anthropic's 529) and on overload wording.
    Auth/billing/validation errors (401/403/404/400) are NOT transient.
    """
    import re
    text = f"{type(exc).__name__} {exc}"
    code_match = re.search(r"\b(40[089]|425|50[0-4]|52[0-9])\b", text)
    if code_match and int(code_match.group(1)) in RETRYABLE_STATUS_CODES:
        return True
    lowered = text.lower()
    markers = (
        "unavailable", "resource_exhausted", "overloaded", "overload",
        "rate limit", "rate_limit", "ratelimit", "too many requests",
        "temporarily", "temporary", "try again", "high demand",
        "timeout", "timed out", "connection", "network", "econn",
        "socket", "dns", "resolve", "could not resolve",
    )
    return any(marker in lowered for marker in markers)
