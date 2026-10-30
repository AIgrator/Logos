"""Next-step suggestions attached to assistant answers in the agent modes."""
from __future__ import annotations

import re

MAX_SUGGESTIONS = 3
MAX_SUGGESTION_CHARS = 80
SUGGESTIONS_MARKER = re.compile(r"^\s*SUGGESTIONS?\s*:\s*(.+?)\s*$", re.IGNORECASE)


def normalize_suggestions(value) -> list[str]:
    """Keep at most three unique, short, single-line suggestions."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        text = " ".join(raw.split()).strip()
        if not text:
            continue
        if len(text) > MAX_SUGGESTION_CHARS:
            text = text[:MAX_SUGGESTION_CHARS - 1].rstrip(" ,;:.!?") + "…"
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)
        if len(items) >= MAX_SUGGESTIONS:
            break
    return items


def extract_suggestions(text: str) -> tuple[str, list[str]]:
    """Pull the trailing ``SUGGESTIONS: a | b | c`` line out of a text answer."""
    body = str(text or "")
    lines = body.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        match = SUGGESTIONS_MARKER.match(lines[index])
        if not match:
            continue
        items = normalize_suggestions(match.group(1).split("|"))
        kept = lines[:index] + lines[index + 1:]
        return "\n".join(kept).rstrip(), items
    return body, []
