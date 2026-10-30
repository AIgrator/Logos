"""MCP meta-tools exposed to the Terminal agent.

The agent never sees one tool per MCP tool (server inventories are dynamic and
would flood the prompt): it discovers servers with ``mcp_list``, reads a tool's
input schema with ``mcp_get`` and executes it with ``mcp_call``. The client
manager itself lives in ``src.mcp_client`` and is attached by the controller.
"""
import json

from src.computer.computer import tool_meta

MAX_RESULT_CHARS = 20000
LIST_DESCRIPTION_LIMIT = 160


def _manager(self):
    manager = getattr(self, "mcp_manager", None)
    if manager is None or not getattr(manager, "is_running", False):
        raise RuntimeError(
            "MCP is not available: no MCP manager is running for this Terminal "
            "session. Servers connect when Terminal mode starts; ask the user to "
            "check Settings -> MCP Servers.")
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


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text) - limit} more characters)"


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
    return _clip("\n".join(lines))


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
    return _clip(json.dumps(payload, ensure_ascii=False, indent=2))


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
            "description": "The text returned by the MCP tool.",
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
    return _clip(text or f"MCP tool '{server}/{tool}' returned no output.")
