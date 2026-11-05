"""The tool block a session's grants produce.

The per-mode cache files (``cache/terminal/tools.txt`` …) describe what a mode
can do. A session is not a mode anymore: it can hold the terminal capability and
the browser one at the same time, so its tool list is built from its grants and
written to ``cache/session/<handler>/`` (see
``ToolRegistry.generate_session_profile``).

Handlers call :func:`session_tools_text` while building their prompts; the
generation is signature-checked, so a grant change or a different session
regenerates it on the next turn without any explicit invalidation step.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from src.computer.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


def ensure_session_profile(settings, grants: Iterable[str],
                           handlers=None, force: bool = False,
                           promote=()) -> dict:
    """Regenerates the session views when the grants changed; returns their paths.

    ``settings`` is the app settings mapping (it only needs ``APP_CACHE_DIR``),
    so a handler can pass its own settings dict. ``promote`` names catalog tools
    the model loaded with ``search_tools``.
    """
    if not settings:
        return {}
    try:
        return ToolRegistry(settings).generate_session_profile(
            grants, handlers=handlers, force=force, promote=promote)
    except Exception as error:  # noqa: BLE001 - a prompt must still be built
        logger.warning("Could not build the session tool profile: %s", error,
                       exc_info=True)
        return {}


def session_tools_text(settings, grants: Iterable[str], handler: str,
                       promote=()) -> str:
    """The ``{available_tools}`` block for one handler under these grants.

    Returns an empty string when nothing can be built; the caller then falls
    back to its per-mode file.
    """
    view = ensure_session_profile(
        settings, grants, handlers=(handler,), promote=promote).get(handler)
    path = (view or {}).get("tools") or ""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        logger.warning("Could not read the session tool block '%s': %s", path, error)
        return ""
