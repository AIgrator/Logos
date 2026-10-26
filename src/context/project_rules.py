"""Stable project instructions from the root ``AGENTS.md``.

This is a baseline, not a summary: it is written by the user, read on demand,
and never generated or rewritten by the application. Only the file in the
active project root is used (no parent or per-directory lookup).
"""
from __future__ import annotations

import logging
import os

from context.secrets import redact_text

FILENAME = "AGENTS.md"
BLOCK_PREFIX = "**Project instructions (AGENTS.md):**"
MAX_BYTES = 32 * 1024
_FOOTER = (
    "These rules apply to all work in this project. Follow them unless the "
    "user explicitly overrides a rule; if the request conflicts with them, "
    "say so. System safety rules always take precedence."
)

_cache: dict[str, tuple[int, int, str]] = {}


def read(project_root: str) -> str:
    """Return the redacted text of ``<project_root>/AGENTS.md`` (cached by mtime)."""
    if not project_root:
        return ""
    path = os.path.join(project_root, FILENAME)
    key = os.path.normcase(os.path.abspath(path))
    try:
        stat = os.stat(path)
        if not os.path.isfile(path):
            _cache.pop(key, None)
            return ""
    except OSError:
        _cache.pop(key, None)
        return ""

    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(key)
    if cached and cached[0] == signature[0] and cached[1] == signature[1]:
        return cached[2]

    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        logging.warning(f"Could not read {path}: {exc}")
        _cache.pop(key, None)
        return ""

    truncated = len(data) > MAX_BYTES
    text = data[:MAX_BYTES].decode("utf-8", errors="replace").strip()
    if truncated:
        text += "\n\n[AGENTS.md truncated at 32 KB]"
    text = redact_text(text).strip()
    _cache[key] = (signature[0], signature[1], text)
    return text


def injection_block(project_root: str) -> str:
    """Prompt block for the active project, or an empty string when absent."""
    text = read(project_root)
    if not text:
        return ""
    return f"{BLOCK_PREFIX}\n\n{text}\n\n{_FOOTER}"
