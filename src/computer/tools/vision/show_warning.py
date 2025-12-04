import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "show_warning",
        "description": "Displays a confirmation dialog to the user before a potentially destructive action.",
        "arguments": {
            "message": {"type": "STRING", "description": "The warning message to display to the user.", "required": True}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def show_warning(self, **kwargs):
    if 'message' not in kwargs:
        raise ValueError("Action 'show_warning' failed: Missing required 'message' argument. Example: {'name': 'show_warning', 'arguments': {'message': 'This will delete the file. Are you sure?'}}")
    logging.info("The warning message to display to the user before a potentially destructive action.")
    pass
