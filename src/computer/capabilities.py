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
#: ``open_browser`` and ``open_terminal`` are control tools: they ask for a
#: capability rather than doing its work themselves, so they belong to every
#: agent session. ``search_tools`` promotes an on-demand tool into the next
#: request (see ``CATALOG_TOOLS``).
BASE_TOOLS = (
    "read_files",
    "list_directory",
    "search_file_content",
    "open_browser",
    "open_terminal",
    "search_tools",
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
    "browser_navigate_forward",
    "browser_reload",
    "browser_scroll",
    "browser_check",
    "browser_uncheck",
    "browser_set_files",
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

#: Tools granted but **not declared** in every request: the model reaches them
#: through ``search_tools``, which promotes one into the next request. They stay
#: cheap in the prompt (names only) while the common step-by-step tools, which
#: the model needs without a detour, stay native. Phase 3 of
#: [the vision plan](../docs/future_vision.md) decides what belongs here.
CATALOG_TOOLS = frozenset({
    # The MCP facade is three meta-tools with long schemas, used only when a
    # configured server is actually needed.
    "mcp_list",
    "mcp_get",
    "mcp_call",
    # Browser inspection and edge cases: snapshots/clicks/fills are native,
    # these are reached after a snapshot or a failing step.
    "browser_eval",
    "browser_console",
    "browser_network",
    "browser_frames",
    "browser_handle_dialog",
    "browser_select_option",
    "browser_hover",
    "browser_find",
    "browser_navigate_back",
    "browser_navigate_forward",
    "browser_reload",
    "browser_scroll",
    "browser_check",
    "browser_uncheck",
    "browser_set_files",
})

#: Capabilities a mode grants today, and the shape the journal records.
MODE_GRANTS: dict[str, tuple[str, ...]] = {
    "chat": (CHAT,),
    "terminal": (BASE, TERMINAL),
    "browser": (BASE, BROWSER),
    "vision": (VISION,),
}

#: Envelope each agent handler answers with (the model's response format).
HANDLER_SCHEMAS: dict[str, str] = {
    TERMINAL: "terminal_tool_code",
    BROWSER: "browser_tool_code",
}

#: Capabilities each agent handler can *execute* today.
#:
#: Terminal and Browser are one loop (``TerminalHandler`` presents as either),
#: and that loop dispatches ``browser_*`` calls into the shared browser service
#: and every other tool onto its exec thread, so one handler runs every granted
#: agent tool. The two entries stay separate because the prompt and the
#: response envelope are still chosen per presentation.
HANDLER_CAPABILITIES: dict[str, tuple[str, ...]] = {
    TERMINAL: (BASE, TERMINAL, BROWSER),
    BROWSER: (BASE, TERMINAL, BROWSER),
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


def native_tools_for_grants(grants: Iterable[str],
                            promote: Iterable[str] = ()) -> tuple[str, ...]:
    """Granted tools declared in every request.

    ``promote`` names catalog tools the model asked for through
    ``search_tools``; they join the native set from the next request on.
    """
    wanted = {str(name).strip() for name in (promote or ()) if str(name).strip()}
    return tuple(
        name for name in tools_for_grants(grants)
        if name not in CATALOG_TOOLS or name in wanted)


def catalog_tools_for_grants(grants: Iterable[str],
                             promote: Iterable[str] = ()) -> tuple[str, ...]:
    """Granted tools reachable on demand, minus those already promoted."""
    wanted = {str(name).strip() for name in (promote or ()) if str(name).strip()}
    granted = set(tools_for_grants(grants))
    return tuple(
        name for capability in normalize_grants(grants)
        for name in TOOLS.get(capability, ())
        if name in CATALOG_TOOLS and name in granted and name not in wanted)


def tools_for_handler(handler: str, grants: Iterable[str],
                      promote: Iterable[str] = ()) -> tuple[str, ...]:
    """Tools one agent handler declares for a session's grants.

    Never wider than the grants: an ungranted capability's tools stay invisible
    even when the handler is able to execute them. Catalog tools are left out
    unless ``promote`` names them (``search_tools`` did).
    """
    handler = (handler or "").strip().lower()
    allowed = HANDLER_CAPABILITIES.get(handler)
    if allowed is None:
        return native_tools_for_grants(grants, promote)
    granted = set(native_tools_for_grants(grants, promote))
    return tuple(name for name in tools_for_grants(allowed) if name in granted)


def catalog_tools_for_handler(handler: str, grants: Iterable[str],
                              promote: Iterable[str] = ()) -> tuple[str, ...]:
    """On-demand tools this handler could run, for the prompt's catalog line."""
    handler = (handler or "").strip().lower()
    allowed = HANDLER_CAPABILITIES.get(handler)
    reachable = (set(tools_for_grants(allowed)) if allowed is not None
                 else {name for names in TOOLS.values() for name in names})
    return tuple(name for name in catalog_tools_for_grants(grants, promote)
                 if name in reachable)


def catalog_tools_for_mode(mode: str) -> tuple[str, ...]:
    """On-demand tools a mode's own cache block names (its handlers')."""
    mode = (mode or "").strip().lower()
    grants = grants_for_mode(mode)
    if mode in HANDLER_SCHEMAS:
        return catalog_tools_for_handler(mode, grants)
    return tuple(name for name in catalog_tools_for_grants(grants)
                 if name in set(tools_for_grants(grants)))


def merge_grants(*sets: Iterable[str]) -> tuple[str, ...]:
    """Ordered union of several grant sets (a capability attached in place)."""
    merged: list[str] = []
    for values in sets:
        for capability in normalize_grants(values):
            if capability not in merged:
                merged.append(capability)
    return normalize_grants(merged)


def remove_grants(grants: Iterable[str], *names: str) -> tuple[str, ...]:
    """Grants without the named capabilities; ``base`` always stays.

    Revoking is deliberate and immediate (P8 of the vision plan): the caller is
    expected to have asked the user, and the next prompt is built from what is
    left. Base is not a permission to remove, so it is never dropped.
    """
    dropped = {str(name).strip().lower() for name in names}
    return tuple(name for name in normalize_grants(grants)
                 if name == BASE or name not in dropped)


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
