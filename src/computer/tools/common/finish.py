import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "finish",
        "description": "Signals the successful completion of the entire task.",
        "arguments": {
            "message": {"type": "STRING", "description": "A summary message to display to the user upon completion.", "required": True},
            "suggestions": {"type": "LIST_OF_STRINGS", "description": "Up to three short imperative next actions the user is likely to take after this result (open, run, review, commit, compare), in the user's language; rendered as clickable chips. Leave empty only when the result is self-contained and no sensible next step exists. Decide from the task context; never mention this in the message.", "required": False}
        }
    },
    flags={"platform": "any", "mode": "any"}
)
def finish(self, **kwargs):
    if 'message' not in kwargs:
        raise ValueError("Action 'finish' failed: Missing required 'message' argument. Example: {'name': 'finish', 'arguments': {'message': 'Task completed successfully.'}}")
    logging.info("Finish action received. Task is complete.")
    pass
