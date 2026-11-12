"""Stable instruction files injected into every request.

Two layers, user first and project second, so a project can override a personal
default:

- ``~/.logos/AGENTS.md`` - the user's own standing preferences, shared by every
  project. Created from a template by ``/memory`` when the user asks for it.
- ``<project>/AGENTS.md`` - the project's baseline. Written by the user or by
  ``/init``; the application never rewrites it on its own.

Only the file in the active project root is used (no parent or per-directory
lookup). Both are read on demand and cached by mtime.
"""
from __future__ import annotations

import logging
import os
import re

from context.secrets import redact_text

FILENAME = "AGENTS.md"
BLOCK_PREFIX = "**Project instructions (AGENTS.md):**"

USER_FOLDER = ".logos"
USER_BLOCK_PREFIX = "**User instructions (~/.logos/AGENTS.md):**"
USER_MAX_BYTES = 8 * 1024
MAX_BYTES = 32 * 1024

_FOOTER = (
    "These rules apply to all work in this project. Follow them unless the "
    "user explicitly overrides a rule; if the request conflicts with them, "
    "say so. System safety rules always take precedence."
)
_USER_FOOTER = (
    "These are the user's standing preferences and apply in every project. "
    "The project instructions below override them when the two disagree, and "
    "an explicit request in the conversation overrides both."
)

#: Written once, by ``/memory``, into a user file that does not exist yet. The
#: whole template is one HTML comment, and comments are stripped before either
#: the prompt or ``/memory`` shows the file, so creating it cannot change what
#: the model sees and cannot look like a saved rule.
USER_TEMPLATE = """\
<!-- User memory: rules that apply in every project.

One bullet per rule, for example:
- Answer in Russian.
- Use `uv run` for Python commands, never a bare `pip`.
- Never add `# type: ignore`; fix the type instead.

A project's own AGENTS.md overrides anything written here.
`/memory <text>` appends a line; `/memory` shows this file.
-->
"""

_cache: dict[str, tuple[int, int, str]] = {}

_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")


def _strip_comments(text: str) -> str:
    """HTML comments are notes to the reader, never prompt content."""
    return _COMMENT_RE.sub("", str(text or ""))


def user_memory_path() -> str:
    """``~/.logos/AGENTS.md`` - the same folder the app uses for skills and MCP."""
    return os.path.join(os.path.expanduser("~"), USER_FOLDER, FILENAME)


def read(project_root: str) -> str:
    """Return the redacted text of ``<project_root>/AGENTS.md`` (cached by mtime)."""
    if not project_root:
        return ""
    return _read_file(
        os.path.join(project_root, FILENAME), MAX_BYTES, FILENAME)


def read_user() -> str:
    """Return the redacted text of the user's memory file (cached by mtime)."""
    return _read_file(user_memory_path(), USER_MAX_BYTES, "user memory")


def injection_block(project_root: str) -> str:
    """Prompt blocks for the active project, or an empty string when absent."""
    blocks = []
    user = read_user()
    if user:
        blocks.append(f"{USER_BLOCK_PREFIX}\n\n{user}\n\n{_USER_FOOTER}")
    project = read(project_root)
    if project:
        blocks.append(f"{BLOCK_PREFIX}\n\n{project}\n\n{_FOOTER}")
    return "\n\n".join(blocks)


def is_generated_block(text: str) -> bool:
    """True for a block ``injection_block`` produced, either layer.

    The agent history keeps these blocks at the top and rebuilds them on a
    project switch, so the filter has to recognize both prefixes.
    """
    return str(text or "").startswith((USER_BLOCK_PREFIX, BLOCK_PREFIX))


def ensure_user_memory() -> str:
    """Create the user memory file from a template when it is missing.

    Only ``/memory`` calls this: the application never creates a memory file on
    its own, and never touches the project's ``AGENTS.md`` at all.
    """
    path = user_memory_path()
    if os.path.isfile(path):
        return path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(USER_TEMPLATE)
    except OSError as exc:
        logging.warning(f"Could not create the user memory file {path}: {exc}")
    return path


class MemoryLimitError(Exception):
    """The file would grow past its cap; the message carries path and size."""


class MemorySecretError(Exception):
    """The text looks like a credential and must not be stored in plain text."""


def append_memory(path: str, text: str, cap_bytes: int) -> tuple[int, int]:
    """Append each non-empty line of ``text`` to ``path`` as one bullet.

    Returns ``(added, duplicates)``. The text is stored exactly as typed: no
    model is involved, because memory has to be what the user wrote, not a
    paraphrase of it. A line already present is skipped instead of repeated,
    and a file that would pass its cap raises ``MemoryLimit`` rather than
    growing into something the prompt silently truncates.
    """
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # A typed bullet marker would otherwise be doubled into ``- - ...``.
        line = re.sub(r"^[-*+]\s+", "", line).strip()
        if line:
            lines.append(line)
    if not lines:
        return (0, 0)
    if redact_text("\n".join(lines)) != "\n".join(lines):
        raise MemorySecretError()

    raw = _read_raw(path)
    known = _bullet_texts(raw)
    fresh: list[str] = []
    duplicates = 0
    for line in lines:
        key = line.lstrip("-*+ ").strip().casefold()
        if not key:
            continue
        if key in known:
            duplicates += 1
            continue
        known.add(key)
        fresh.append(line)
    if not fresh:
        return (0, duplicates)

    newline = "\r\n" if "\r\n" in raw else "\n"
    payload = "".join(f"- {line}{newline}" for line in fresh)
    size = len(raw.encode("utf-8"))
    if size + len(payload.encode("utf-8")) > cap_bytes:
        raise MemoryLimitError(
            f"{os.path.basename(path)} is {size / 1024:.1f} KB of "
            f"{cap_bytes // 1024} KB")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            if raw and not raw.endswith(("\n", "\r")):
                fh.write(newline)
            fh.write(payload)
    except OSError as exc:
        raise OSError(f"Could not write {path}: {exc}") from exc
    return (len(fresh), duplicates)


def _read_raw(path: str) -> str:
    """The file as it is on disk, unredacted (only used to compare lines)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_BYTES * 4).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _bullet_texts(text: str) -> set[str]:
    """The lines already present, as ``- line`` without the marker.

    Comments are stripped first: the template's examples sit inside one, and
    they must not make the user's own first rule look like a duplicate.
    """
    known = set()
    for line in _strip_comments(text).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        known.add(stripped.lstrip("-*+ ").strip().casefold())
    return known


def stored_text(path: str, max_bytes: int = MAX_BYTES) -> str:
    """The file exactly as written, for showing it back to the user.

    Not redacted and not trimmed: ``/memory`` shows what is on disk. Redaction
    belongs to the prompt block, which is the copy that reaches the model.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return ""
    text = data[:max_bytes].decode("utf-8", errors="replace")
    if len(data) > max_bytes:
        text += "\n[...]"
    return text


def display_text(path: str, max_bytes: int = MAX_BYTES) -> str:
    """The file as the user should see it: comments are not content."""
    return _strip_comments(stored_text(path, max_bytes)).strip()


def _read_file(path: str, max_bytes: int, label: str) -> str:
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
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        logging.warning(f"Could not read {path}: {exc}")
        _cache.pop(key, None)
        return ""

    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="replace")
    text = _strip_comments(text).strip()
    if truncated:
        text += f"\n\n[{label} truncated at {max_bytes // 1024} KB]"
    text = redact_text(text).strip()
    _cache[key] = (signature[0], signature[1], text)
    return text
