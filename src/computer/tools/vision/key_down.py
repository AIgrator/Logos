import logging
import pyautogui

from src.computer.computer import tool_meta
from src.computer.tools.constants import KEYBOARD_KEYS

@tool_meta(
    tool={
        "name": "key_down",
        "description": "Presses and holds a key down. Useful for modifier keys (e.g., Shift, Ctrl, Alt) or for actions that require a key to be held down.",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of the action (e.g., 'Holding down Shift to select text').", "required": True},
            "key": {"type": "STRING", "description": "The key to press and hold down (e.g., 'shift', 'ctrl').", "required": True, "enum": KEYBOARD_KEYS}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def key_down(self, **kwargs):
    key = kwargs.get('key')
    if key is None:
        raise ValueError("Action 'key_down' failed: Missing required 'key' argument. Example: {'name': 'key_down', 'arguments': {'description': 'Hold shift', 'key': 'shift'}}")
    try:
        if key not in KEYBOARD_KEYS:
            raise ValueError(f"Invalid key '{key}'. See KEYBOARD_KEYS for supported values.")
        logging.info(f"Performing key down: '{key}'")
        pyautogui.keyDown(key)
    except Exception as e:
        logging.error(f"Error performing key down for '{key}': {e}")
        raise
