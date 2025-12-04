import logging
import pyautogui

from src.computer.computer import tool_meta
from src.computer.tools.constants import KEYBOARD_KEYS

@tool_meta(
    tool={
        "name": "press_key",
        "description": "Presses a single keyboard key (e.g., 'enter', 'esc', 'f1', 'down').",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of the key being pressed (e.g., 'Pressing the Enter key to confirm.)'", "required": True},
            "key": {"type": "STRING", "description": "The name of the single key to press (e.g., 'enter', 'esc', 'f1', 'down').", "required": True, "enum": KEYBOARD_KEYS}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def press_key(self, **kwargs):
    key = kwargs.get('key')
    keys = kwargs.get('keys')

    if key is None and keys is None:
        raise ValueError("Action 'press_key' failed: Missing required 'key' or 'keys' argument. Example: {'name': 'press_key', 'arguments': {'description': 'Confirm action', 'key': 'enter'}}")

    try:
        if keys is not None:
            if isinstance(keys, str):
                keys = [keys]
            if isinstance(keys, list):
                for k in keys:
                    if k not in KEYBOARD_KEYS:
                        raise ValueError(f"Invalid key '{k}'. See KEYBOARD_KEYS for supported values.")
                    logging.info(f"Performing key press from list: '{k}'")
                    pyautogui.press(k)
                return
            else:
                raise ValueError("'keys' argument must be a list or string.")

        if key not in KEYBOARD_KEYS:
            raise ValueError(f"Invalid key '{key}'. See KEYBOARD_KEYS for supported values.")
        logging.info(f"Performing single key press: '{key}'")
        pyautogui.press(key)
    except Exception as e:
        logging.error(f"Error pressing key(s) '{key or keys}': {e}")
        raise
