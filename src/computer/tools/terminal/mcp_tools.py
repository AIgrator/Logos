"""MCP meta-tools exposed to the Terminal agent.

The agent never sees one tool per MCP tool (server inventories are dynamic and
would flood the prompt): it discovers servers with ``mcp_list``, reads a tool's
input schema with ``mcp_get`` and executes it with ``mcp_call``. The client
manager itself lives in ``src.mcp_client`` and is attached by the controller.
"""
import json
import logging
import os
import re
import time

from src.computer.computer import tool_meta

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 20000
MAX_SPILL_CHARS = 5_000_000
MAX_SPILL_FILES = 50
LIST_DESCRIPTION_LIMIT = 160


def _manager(self):
    manager = getattr(self, "mcp_manager", None)
    if manager is None or not getattr(manager, "is_running", False):
        raise RuntimeError(
            "MCP is not available: no MCP manager is running for this Terminal "
            "session. Servers connect when the terminal capability is attached; "
            "ask the user to check Settings -> MCP Servers.")
    return manager


def _target(server, tool):
    server = str(server or "").strip()
    tool = str(tool or "").strip()
    if not server or not tool:
        raise RuntimeError(
            "Both 'server' and 'tool' are required. Call mcp_list to see the "
            "configured servers and their tools.")
    return server, tool


def _shorten(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _spill_directory(self) -> str:
    root = getattr(self, "working_directory", "") or ""
    if not root:
        return ""
    return os.path.join(root, ".logos", "mcp_outputs")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-")
    return cleaned or "output"


def _prune_spills(directory: str) -> None:
    try:
        files = []
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        files.append((entry.stat().st_mtime, entry.path))
                except OSError:
                    continue
        files.sort(reverse=True)
        for _, path in files[MAX_SPILL_FILES:]:
            try:
                os.remove(path)
            except OSError:
                pass
    except OSError:
        return


def _spill(self, text: str, kind: str, server: str = "", tool: str = "") -> str:
    """Writes the full output under the project so the model can read it later."""
    directory = _spill_directory(self)
    if not directory:
        return ""
    try:
        os.makedirs(directory, exist_ok=True)
        parts = [kind] + [part for part in (server, tool) if part]
        name = "_".join(_safe_name(part) for part in parts)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
        path = os.path.join(directory, f"{name}_{stamp}.txt")
        body = text
        if len(body) > MAX_SPILL_CHARS:
            body = body[:MAX_SPILL_CHARS] + (
                f"\n... (spill truncated, {len(text) - MAX_SPILL_CHARS} more characters)")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    except OSError as error:
        logger.warning(f"Could not spill MCP output: {error}")
        return ""
    _prune_spills(directory)
    return path


def _clip(self, text: str, kind: str, server: str = "", tool: str = "") -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    overflow = len(text) - MAX_RESULT_CHARS
    path = _spill(self, text, kind, server, tool)
    if path:
        return text[:MAX_RESULT_CHARS] + (
            f"\n... (truncated, {overflow} more characters; the full output is "
            f"saved at {path} and can be read with read_files)")
    return text[:MAX_RESULT_CHARS] + f"\n... (truncated, {overflow} more characters)"


def _parse_arguments(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "'arguments' must be a JSON object string such as "
            f"{{\"path\": \".\"}} ({error}).") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("'arguments' must decode to a JSON object, not a list or scalar.")
    return parsed


# -- prompt summary ---------------------------------------------------------

PROMPT_SERVER_LIMIT = 12
PROMPT_DESCRIPTION_LIMIT = 140
PROMPT_TOOL_LIMIT = 6


def _first_sentence(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    cut = re.search(r"(?<=[.!?])\s", text)
    if cut:
        text = text[:cut.start()]
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return text


def _server_prompt_line(status, server_tools: dict, entry=None) -> str:
    head = f"- {status.name} ({status.transport}, {status.state}"
    if status.state == "connected":
        head += f", {status.tools or len(server_tools)} tools"
    head += ")"
    if entry is not None and entry.transport == status.transport:
        description = _first_sentence(entry.description, PROMPT_DESCRIPTION_LIMIT)
        if description:
            return f"{head}: {description}"
    if status.state == "connected":
        names = list(server_tools)[:PROMPT_TOOL_LIMIT]
        if names:
            extra = len(server_tools) - len(names)
            suffix = f" (+{extra})" if extra > 0 else ""
            return f"{head}: {', '.join(names)}{suffix}"
        return head
    if status.state == "error" and status.error:
        return f"{head}: {_shorten(status.error, PROMPT_DESCRIPTION_LIMIT)}"
    if status.state == "needs_trust":
        return f"{head}: project server, not trusted yet"
    return head


def servers_prompt_block(manager) -> str:
    """Configured servers, one line each, for the Terminal system prompt."""
    if manager is None or not getattr(manager, "is_running", False):
        return "No MCP servers are configured."
    try:
        statuses = manager.statuses()
        tools = manager.tools()
    except Exception as error:
        logger.warning(f"Could not describe MCP servers: {error}")
        return "MCP server state is unavailable; call mcp_list to check."
    if not statuses:
        return "No MCP servers are configured."

    from src.mcp_client.catalog import CATALOG

    catalog = {entry.name: entry for entry in CATALOG}
    ordered = sorted(
        statuses.values(),
        key=lambda status: (status.state != "connected", status.name.lower()))
    lines = ["Configured MCP servers (live state):"]
    for status in ordered[:PROMPT_SERVER_LIMIT]:
        lines.append(_server_prompt_line(
            status, tools.get(status.name, {}), catalog.get(status.name)))
    if len(ordered) > PROMPT_SERVER_LIMIT:
        lines.append(
            f"- …and {len(ordered) - PROMPT_SERVER_LIMIT} more; mcp_list shows them all.")
    return "\n".join(lines)


@tool_meta(
    tool={
        "name": "mcp_list",
        "description": (
            "Lists the configured MCP servers with their connection state and the "
            "tools each connected server offers. Call it before using any MCP tool."),
        "arguments": {
            "server": {
                "type": "STRING",
                "description": "Optional server name; when set, lists only that server's tools.",
                "required": False,
            }
        },
        "returns": {
            "type": "STRING",
            "description": "Server states plus tool names and short descriptions.",
        },
    },
    flags={"platform": "any", "mode": "terminal"},
)
def mcp_list(self, server: str = "", **kwargs) -> str:
    """Lists MCP servers and the tools they expose."""
    manager = _manager(self)
    statuses = manager.statuses()
    requested = str(server or "").strip()
    if requested:
        if requested not in statuses:
            available = ", ".join(sorted(statuses)) or "none"
            raise RuntimeError(
                f"Unknown MCP server '{requested}'. Configured servers: {available}.")
        statuses = {requested: statuses[requested]}
    if not statuses:
        return ("No MCP servers are configured. The user can add them in "
                "Settings -> MCP Servers or in the project's .mcp.json.")

    lines = []
    for name in sorted(statuses):
        status = statuses[name]
        head = f"{name} ({status.scope}, {status.transport}): {status.state}"
        if status.tools:
            head += f", {status.tools} tool(s)"
        if status.error:
            head += f" - {status.error}"
        lines.append(head)
        if status.state != "connected":
            continue
        for tool_name, info in sorted(manager.tools(name).items()):
            suffix = " [read-only]" if info.read_only else ""
            description = _shorten(info.description, LIST_DESCRIPTION_LIMIT)
            line = f"  - {tool_name}{suffix}"
            lines.append(f"{line}: {description}" if description else line)
    return _clip(self, "\n".join(lines), "list")


@tool_meta(
    tool={
        "name": "mcp_get",
        "description": (
            "Returns the full definition of one MCP tool: description, annotations "
            "and the JSON input schema. Call it before mcp_call to learn the exact "
            "argument names."),
        "arguments": {
            "server": {
                "type": "STRING",
                "description": "MCP server name as listed by mcp_list.",
                "required": True,
            },
            "tool": {
                "type": "STRING",
                "description": "Tool name as listed by mcp_list.",
                "required": True,
            },
        },
        "returns": {
            "type": "STRING",
            "description": "A JSON document with the tool's metadata and input schema.",
        },
    },
    flags={"platform": "any", "mode": "terminal"},
)
def mcp_get(self, server: str = "", tool: str = "", **kwargs) -> str:
    """Returns one MCP tool's metadata and input schema."""
    manager = _manager(self)
    server, tool = _target(server, tool)
    status = manager.status(server)
    if status.state != "connected":
        raise RuntimeError(
            f"MCP server '{server}' is {status.state}, so its tools cannot be "
            f"inspected right now. {status.error}".strip())
    info = manager.get_tool(server, tool)
    payload = {"server": server, **info.as_dict()}
    return _clip(self, json.dumps(payload, ensure_ascii=False, indent=2), "get", server, tool)


@tool_meta(
    tool={
        "name": "mcp_call",
        "description": (
            "Calls a tool on a connected MCP server. Read the tool's schema with "
            "mcp_get first and pass arguments as a JSON object string that matches "
            "that schema exactly."),
        "arguments": {
            "server": {
                "type": "STRING",
                "description": "MCP server name as listed by mcp_list.",
                "required": True,
            },
            "tool": {
                "type": "STRING",
                "description": "Tool name as listed by mcp_list.",
                "required": True,
            },
            "arguments": {
                "type": "STRING",
                "description": (
                    "A JSON object string with the tool's arguments, for example "
                    "{\"path\": \".\"}. Omit for tools without arguments."),
                "required": False,
            },
        },
        "returns": {
            "type": "STRING",
            "description": (
                "The text returned by the MCP tool. Output over 20,000 "
                "characters is truncated; the full text is saved in the "
                "project's .logos/mcp_outputs folder and its path is included."),
        },
    },
    flags={"platform": "any", "mode": "terminal"},
)
def mcp_call(self, server: str = "", tool: str = "", arguments: str = "", **kwargs) -> str:
    """Executes one MCP tool call and returns its text output."""
    manager = _manager(self)
    server, tool = _target(server, tool)
    args = _parse_arguments(arguments)
    result = manager.call_tool(server, tool, args)
    text = result.as_text().strip()
    if result.is_error:
        raise RuntimeError(text or f"MCP tool '{server}/{tool}' reported an error.")
    return _clip(
        self, text or f"MCP tool '{server}/{tool}' returned no output.",
        "call", server, tool)
