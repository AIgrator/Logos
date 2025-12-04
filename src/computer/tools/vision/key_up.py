import logging
import pyautogui

from src.computer.computer import tool_meta
from src.computer.tools.constants import KEYBOARD_KEYS

@tool_meta(
    tool={
        "name": "key_up",
        "description": "Releases a previously pressed key. Used in conjunction with key_down.",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of the action (e.g., 'Releasing Shift after selection').", "required": True},
            "key": {"type": "STRING", "description": "The key to release (e.g., 'shift', 'ctrl').", "required": True, "enum": KEYBOARD_KEYS}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def key_up(self, **kwargs):
    key = kwargs.get('key')
    if key is None:
        raise ValueError("Action 'key_up' failed: Missing required 'key' argument. Example: {'name': 'key_up', 'arguments': {'description': 'Release shift', 'key': 'shift'}}")
    try:
        if key not in KEYBOARD_KEYS:
            raise ValueError(f"Invalid key '{key}'. See KEYBOARD_KEYS for supported values.")
        logging.info(f"Performing key up: '{key}'")
        pyautogui.keyUp(key)
    except Exception as e:
        logging.error(f"Error performing key up for '{key}': {e}")
        raise
