"""Central credential redaction for the project journal and handoff summary."""
from __future__ import annotations

import re

_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>(?:--)?[\"']?\b(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|token|password|passwd|secret|authorization)\b"
    r"[\"']?\s*(?::|=)\s*)"
    r"(?P<value>(?:bearer\s+)?[^\s,;]+)"
)
_FLAG_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>--(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"token|password|passwd|secret)\b\s+)"
    r"(?P<value>[^\s,;]+)"
)
_SECRET_KEYS = {
    "api_key", "apikey", "access_token", "refresh_token", "token",
    "password", "passwd", "secret", "authorization",
}


def redact_text(text: str) -> str:
    if not text:
        return ""
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group('prefix')}[REDACTED]", str(text))
    return _FLAG_VALUE_RE.sub(
        lambda m: f"{m.group('prefix')}[REDACTED]", text)


def _is_secret_key(key) -> bool:
    return str(key or "").strip().lower().replace("-", "_") in _SECRET_KEYS


def redact_value(value):
    """Recursively redact strings in journal event fields."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value
