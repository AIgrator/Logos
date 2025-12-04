import logging
import pyautogui

from src.computer.computer import tool_meta
from src.computer.tools.constants import KEYBOARD_KEYS

@tool_meta(
    tool={
        "name": "hotkey",
        "description": "Presses a combination of keys (hotkey).(e.g., ['ctrl', 's'], ['ctrl', 'c'], ['alt', 'f4'])",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of the hotkey action (e.g., 'Pressing Ctrl+S to save.')", "required": True},
            "keys": {"type": "ARRAY", "items": {"type": "STRING", "enum": KEYBOARD_KEYS}, "description": "A list of key names to press in combination, e.g., ['ctrl', 's'], ['ctrl', 'c'], ['alt', 'f4'].", "required": True}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def hotkey(self, **kwargs):
    keys = kwargs.get('keys')
    if not isinstance(keys, list) or not keys:
        raise ValueError("Action 'hotkey' failed: The 'keys' argument must be a non-empty list of strings. Example: {'name': 'hotkey', 'arguments': {'description': 'Save file', 'keys': ['ctrl', 's']}}")

    try:
        for k in keys:
            if k not in KEYBOARD_KEYS:
                raise ValueError(f"Invalid key '{k}' in hotkey. See KEYBOARD_KEYS for supported values.")
        logging.info(f"Performing hotkey: {keys}")
        pyautogui.hotkey(*keys)
    except Exception as e:
        logging.error(f"Error pressing hotkey '{keys}': {e}")
        raise
