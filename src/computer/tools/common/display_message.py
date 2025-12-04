import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "display_message",
        "description": "Displays a message to the user. This is for informational purposes only; the assistant will not wait for a response.",
        "arguments": {
            "message": {"type": "STRING", "description": "The information to display to the user.", "required": True}
        }
    },
    flags={"platform": "any", "mode": "any"}
)
def display_message(self, **kwargs):
    if 'message' not in kwargs:
        raise ValueError("Action 'display_message' failed: Missing required 'message' argument.")
    logging.info("Displays a message to the user for informational purposes.")
    pass
