from src.computer.computer import tool_meta


@tool_meta(
    tool={
        "name": "open_terminal",
        "description": (
            "Attaches the terminal capability to this session, so the next "
            "steps can run shell commands and edit files (execute_shell, "
            "write_file, replace, screenshot, MCP tools). Use it when the task "
            "needs the local machine and the terminal tools are not listed "
            "yet. It executes nothing itself."),
        "arguments": {},
        "returns": {
            "type": "STRING",
            "description": "Confirmation that the terminal tools are available.",
        },
    },
    flags={"platform": "any", "mode": "any"},
)
def open_terminal(self, **kwargs):
    """Handled by the agent loop, which owns the grants and the session."""
    return None
