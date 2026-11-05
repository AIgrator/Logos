from src.computer.computer import tool_meta


@tool_meta(
    tool={
        "name": "search_tools",
        "description": (
            "Loads the schema of a granted tool that is listed under Tools on "
            "demand instead of being declared. Pass the tool name or what you "
            "need to do (\"read the page console\", \"run an MCP tool\"); the "
            "result carries the tool's description and arguments, and the tool "
            "becomes callable on the next step. Use it before guessing an "
            "argument or when the tool you need is not in the tool list."),
        "arguments": {
            "query": {
                "type": "STRING",
                "description": (
                    "Tool name or a short description of the needed action."),
                "required": True,
            }
        },
        "returns": {
            "type": "STRING",
            "description": (
                "The tool's schema, or the names of the available on-demand "
                "tools when nothing matched."),
        },
    },
    flags={"platform": "any", "mode": "any"},
)
def search_tools(self, query: str = "", **kwargs):
    """Handled by the agent loop, which owns the grants and the request."""
    return None
