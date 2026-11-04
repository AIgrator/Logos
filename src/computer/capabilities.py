"""Capability registry: which tools a session may use.

A **capability** is what a session is allowed to do (``base``, ``terminal``,
``browser``, ``vision``). A **mode** is how the current build delivers it: a
session is still mode-locked, so its grants are derived from the mode. This
module is the single place where that membership lives, so the tool cache, the
strict schemas, the prompts and (later) the UI stop each keeping their own list.

Two capabilities are whole sets rather than an addition to ``base``:

- ``vision`` runs the provider's Computer Use loop, which brings its own tools
  and does not use ``update_plan`` / ``use_skill`` / ``search_file_content``.
- ``chat`` has no tool loop at all; it keeps its two web tools until Phase 1 of
  [the vision plan](../docs/future_vision.md) turns it into a base-only session.

A mode's tool list is therefore ``base + <capability>`` for terminal/browser and
the capability's own list for vision/chat. Adding a tool means adding its name
here — ``ToolRegistry`` drops names it does not find and logs the divergence, so
a tool can never reach the model without a capability that grants it.
"""
from __future__ import annotations

from collections.abc import Iterable

BASE = "base"
TERMINAL = "terminal"
BROWSER = "browser"
VISION = "vision"
CHAT = "chat"

#: Every capability the application knows about, in grant order.
CAPABILITIES = (BASE, TERMINAL, BROWSER, VISION, CHAT)

#: Tools every agent session may use: read-only filesystem, control, skills.
BASE_TOOLS = (
    "read_files",
    "list_directory",
    "search_file_content",
    "finish",
    "communicate_to_user",
    "update_plan",
    "use_skill",
)

#: What a granted terminal adds: writing, running, capturing, network, MCP.
TERMINAL_TOOLS = (
    "find_files",
    "path_exists",
    "display_message",
    "create_directory",
    "write_file",
    "replace",
    "replace_many",
    "delete_files",
    "copy_files",
    "move_files",
    "execute_shell",
    "poll_shell_job",
    "stop_shell_job",
    "wait_seconds",
    "screenshot",
    "get_clipboard_content",
    "web_search",
    "web_fetchs",
    "download_file",
    "mcp_list",
    "mcp_get",
    "mcp_call",
)

#: The browser facade; it already carries the filesystem readers it needs.
BROWSER_TOOLS = (
    "browser_goto",
    "browser_snapshot",
    "browser_click",
    "browser_fill",
    "browser_select_option",
    "browser_hover",
    "browser_press",
    "browser_read",
    "browser_console",
    "browser_network",
    "browser_screenshot",
    "browser_find",
    "browser_wait_for",
    "browser_navigate_back",
    "browser_tabs",
    "browser_frames",
    "browser_eval",
    "browser_handle_dialog",
    "browser_close",
)

#: Vision is a whole set: the provider's Computer Use toolset.
VISION_TOOLS = (
    "click",
    "double_click",
    "drag_and_drop",
    "scroll",
    "scroll_page",
    "press_key",
    "key_down",
    "key_up",
    "hotkey",
    "write_text",
    "launch_app",
    "wait_element",
    "show_warning",
    "execute_shell",
    "write_file",
    "read_files",
    "list_directory",
    "display_message",
    "get_clipboard_content",
    "communicate_to_user",
    "finish",
)

#: Chat is a whole set too, until it becomes a base-only session.
CHAT_TOOLS = (
    "web_search",
    "web_fetchs",
)

#: Tools owned by each capability, in declaration order.
TOOLS: dict[str, tuple[str, ...]] = {
    BASE: BASE_TOOLS,
    TERMINAL: TERMINAL_TOOLS,
    BROWSER: BROWSER_TOOLS,
    VISION: VISION_TOOLS,
    CHAT: CHAT_TOOLS,
}

#: Capabilities a mode grants today, and the shape the journal records.
MODE_GRANTS: dict[str, tuple[str, ...]] = {
    "chat": (CHAT,),
    "terminal": (BASE, TERMINAL),
    "browser": (BASE, BROWSER),
    "vision": (VISION,),
}

#: A fresh session without a mode yet starts here.
DEFAULT_GRANTS = MODE_GRANTS["chat"]


def normalize_grants(value: Iterable[str] | None) -> tuple[str, ...]:
    """Validated, de-duplicated grants in canonical order (never raises)."""
    wanted = {str(item).strip().lower() for item in (value or ()) if str(item).strip()}
    return tuple(name for name in CAPABILITIES if name in wanted)


def grants_for_mode(mode: str) -> tuple[str, ...]:
    """Capabilities a mode grants; unknown modes fall back to chat."""
    return MODE_GRANTS.get((mode or "").strip().lower(), DEFAULT_GRANTS)


def tools_for_grants(grants: Iterable[str]) -> tuple[str, ...]:
    """Tool names granted by a capability set, de-duplicated, order preserved."""
    names: list[str] = []
    seen: set[str] = set()
    for capability in normalize_grants(grants):
        for name in TOOLS.get(capability, ()):
            if name not in seen:
                seen.add(name)
                names.append(name)
    return tuple(names)


def tools_for_mode(mode: str) -> tuple[str, ...]:
    """Tool names a session in ``mode`` is granted today."""
    return tools_for_grants(grants_for_mode(mode))


def capability_of_tool(name: str) -> str | None:
    """Which capability owns a tool, or None when nothing grants it."""
    for capability in CAPABILITIES:
        if name in TOOLS.get(capability, ()):
            return capability
    return None


def grants_from_header(header: dict | None) -> tuple[str, ...]:
    """Grants of a journal header, migrating a session written before grants.

    The header may carry ``capabilities`` (0.4+) or only the legacy ``mode``
    (0.3.x and earlier); both open, and the mode is the only migration input.
    """
    header = header or {}
    recorded = normalize_grants(header.get("capabilities") or ())
    if recorded:
        return recorded
    return grants_for_mode(str(header.get("mode") or ""))
