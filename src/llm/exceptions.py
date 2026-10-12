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


#: HTTP statuses that are auto-retried at every provider
#: (529 is Anthropic-specific overload).
#: NOTE: 429 is deliberately NOT here. Original design rule, restored:
#: 429 = quota/limits (money) -> FatalError + honest quota card, no auto-retry.
#: 503/overload = transient fault -> RetryableError + countdown + retry.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 500, 502, 503, 504, 529})


#: Wording that proves quota/limits are EXHAUSTED (money side).
#: Deliberately excludes the rate-limit family: gateways (OpenRouter) answer
#: 429 for a merely busy shared pool too — that is transient, see below.
QUOTA_EXHAUSTION_MARKERS = (
    "quota", "billing", "payment", "out of credits", "insufficient_quota",
    "quota_failure", "exceeded your current quota", "check your plan and billing",
    "resource_exhausted", "resource exhausted",
)


def is_quota_exhausted(exc_or_message) -> bool:
    """True on hard exhaustion signals (quota/billing wording)."""
    if isinstance(exc_or_message, str):
        text = exc_or_message
    else:
        text = f"{type(exc_or_message).__name__} {exc_or_message}"
    return any(marker in text.lower() for marker in QUOTA_EXHAUSTION_MARKERS)


def is_transient_throttle(exc_or_message) -> bool:
    """True on explicit 'retry shortly' upstream throttling WITHOUT exhaustion.

    Some gateways answer 429 for a busy shared pool as well
    ("temporarily rate-limited upstream, retry shortly"). The server asks to
    retry, so this is transient (countdown + bounded retries), not dead quota.
    Any exhaustion marker flips it back to quota.
    """
    if isinstance(exc_or_message, str):
        text = exc_or_message
    else:
        text = f"{type(exc_or_message).__name__} {exc_or_message}"
    lowered = text.lower()
    if "temporarily rate-limited" not in lowered and "retry shortly" not in lowered:
        return False
    return not is_quota_exhausted(lowered)


def is_quota_error(exc_or_message) -> bool:
    """True when an error looks like exhausted quota/limits, not a transient fault.

    Catches quota/billing wording ("quota exceeded", "billing", "out of
    credits", ...) as well as bare 429 codes. Used as a backstop so that a
    quota error which slipped into RetryableError still gets quota treatment
    (no futile auto-retry loop) instead of an "overloaded" card.
    """
    import re
    if isinstance(exc_or_message, str):
        text = exc_or_message
    else:
        text = f"{type(exc_or_message).__name__} {exc_or_message}"
    lowered = text.lower()
    markers = (
        QUOTA_EXHAUSTION_MARKERS
        + ("rate limit", "rate_limit", "ratelimit", "too many requests")
    )
    if any(marker in lowered for marker in markers):
        return True
    return re.search(r"\b429\b", text) is not None

#: Server-demanded waits longer than this are NOT auto-retried.
#: The worker caps backoff at this value, so anything above means
#: "come back much later" (e.g. daily free-tier quota with
#: retryDelay of hours), not "retry in a bit". Such cases get an
#: honest quota-wall card instead of futile retries.
MAX_AUTO_WAIT_SECONDS = 120.0


def format_wait(seconds: float | None) -> str:
    """Human-readable duration for server-demanded waits.

    42 -> "42s", 150 -> "2m 30s", 52334 -> "14h 32m".
    """
    if seconds is None:
        return "unknown time"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    if total < 86400:
        hours, rest = divmod(total, 3600)
        minutes = rest // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, rest = divmod(total, 86400)
    hours = rest // 3600
    return f"{days}d {hours}h" if hours else f"{days}d"


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
