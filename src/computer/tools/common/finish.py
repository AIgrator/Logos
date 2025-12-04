import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "finish",
        "description": "Signals the successful completion of the entire task.",
        "arguments": {"message": {"type": "STRING", "description": "A summary message to display to the user upon completion.", "required": True}}
    },
    flags={"platform": "any", "mode": "any"}
)
def finish(self, **kwargs):
    if 'message' not in kwargs:
        raise ValueError("Action 'finish' failed: Missing required 'message' argument. Example: {'name': 'finish', 'arguments': {'message': 'Task completed successfully.'}}")
    logging.info("Finish action received. Task is complete.")
    pass
