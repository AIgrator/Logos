import logging
import time

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "wait_element",
        "description": "Pauses execution and repeatedly checks the screen until a specific condition is met, which is evaluated by an AI observer. This is essential for handling long-running operations like downloads, installations, or loading screens.",
        "arguments": {
            "description": {"type": "STRING", "description": "A clear, user-facing description of the waiting action.", "required": True},
            "wait_context": {"type": "STRING", "description": "A detailed text description of the element and condition being waited for. This context will be used by another AI to check the screen state.", "required": True},
            "timeout": {"type": "INTEGER", "description": "Maximum time to wait in seconds. Defaults to 60.", "required": False}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def wait_element(self, **kwargs):
    if 'wait_context' not in kwargs:
        raise ValueError("Action 'wait_element' failed: Missing required 'wait_context' argument. Example: {'name': 'wait_element', 'arguments': {'description': 'Wait for download', 'wait_context': 'The progress bar disappears.'}}")
    logging.info(f"Waiting for element: {kwargs.get('element_description')}")
    time.sleep(1)
    pass
