import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "communicate_to_user",
        "description": "Asks the user a question or provides information if the task cannot proceed.",
        "arguments": {
            "message": {"type": "STRING", "description": "The question or information for the user.", "required": True},
            "type": {"type": "STRING", "description": "The type of message: 'question' or 'info'", "enum": ["question", "info"], "required": False},
            "suggestions": {"type": "LIST_OF_STRINGS", "description": "Up to three short imperative answers or next actions the user is likely to choose, in the user's language; rendered as clickable chips. Leave empty only when no clear choice exists. Decide from the task context; never mention this in the message.", "required": False}
        }
    },
    flags={"platform": "any", "mode": "any"}
)
def communicate_to_user(self, **kwargs):
    if 'message' not in kwargs:
        raise ValueError("Action 'communicate_to_user' failed: Missing required 'message' argument. Example: {'name': 'communicate_to_user', 'arguments': {'message': 'Could not find the file. Should I create it?'}}")
    logging.info("Asks the user a question or provides information if the task cannot proceed")
    pass
