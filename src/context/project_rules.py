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

#: Written once, by ``/memory``, into a user file that does not exist yet. Every
#: line is a comment: creating the file must not change what the model does.
USER_TEMPLATE = """\
# User memory

Preferences that apply to every project. A project's own `AGENTS.md` overrides
anything written here.

<!-- One bullet per rule, for example:
- Answer in Russian.
- Use `uv run` for Python commands, never a bare `pip`.
- Never add `# type: ignore`; fix the type instead.
-->
"""

_cache: dict[str, tuple[int, int, str]] = {}


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
    text = data[:max_bytes].decode("utf-8", errors="replace").strip()
    if truncated:
        text += f"\n\n[{label} truncated at {max_bytes // 1024} KB]"
    text = redact_text(text).strip()
    _cache[key] = (signature[0], signature[1], text)
    return text
