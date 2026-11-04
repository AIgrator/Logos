from src.computer.computer import tool_meta


@tool_meta(
    tool={
        "name": "open_browser",
        "description": (
            "Opens a URL in the application's browser and attaches the browser "
            "capability to this session, so the next steps can read, snapshot or "
            "click the page. Use it when the task needs a web page that the "
            "terminal tools cannot provide; call browser_snapshot afterwards to "
            "see the page."),
        "arguments": {
            "url": {
                "type": "STRING",
                "description": "Absolute http(s) or file:// URL to open.",
                "required": True,
            },
            "description": {
                "type": "STRING",
                "description": "Short note, in the user's language, shown as the step title.",
                "required": False,
            },
        },
        "returns": {
            "type": "STRING",
            "description": "The opened page's address and title.",
        },
    },
    flags={"platform": "any", "mode": "any"},
)
def open_browser(self, url: str = "", **kwargs):
    """Handled by the agent loop, which owns the shared browser service."""
    return None
