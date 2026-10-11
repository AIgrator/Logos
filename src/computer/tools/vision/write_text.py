import logging
import time
import sys
import pyautogui
import pyperclip

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "write_text",
        "description": "Types text into the currently focused input field. On Windows uses layout-independent UNICODE injection by default (works with any alphabet); 'clipboard' pastes via Ctrl+V for very long texts instead.",
        "arguments": {
            "description": {"type": "STRING", "description": "e.g., 'Typing the filename 'mydocument.txt'", "required": True},
            "text": {"type": "STRING", "description": "The text to type.", "required": True},
            "method": {"type": "STRING", "description": "The typing method to use. 'clipboard' pastes text for speed, while 'direct' simulates key presses for compatibility. Defaults to the most reliable method for the OS.", "required": False, "enum": ["clipboard", "direct"]}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def write_text(self, **kwargs):
    text = kwargs.get('text')
    method = kwargs.get('method')

    if text is None:
        raise ValueError("Action 'write_text' failed: Missing required 'text' argument. Example: {'name': 'write_text', 'arguments': {'description': 'Enter username', 'text': 'admin'}}")

    use_direct_method = False
    if method == 'direct':
        use_direct_method = True
    elif method == 'clipboard':
        use_direct_method = False
    elif sys.platform == 'win32':
        # Default on Windows: UNICODE injection (layout-independent, verified
        # visually incl. Cyrillic). pyautogui.write() is layout-dependent and
        # clipboard+Ctrl+V paste proved unreliable for system dialogs here.
        use_direct_method = True
    elif sys.platform == 'linux':
        use_direct_method = True

    if use_direct_method:
        if sys.platform == 'win32':
            from src.computer.tools.win_keys import send_text_pynput
            try:
                logging.info(f"Typing text via pynput: '{text}'")
                send_text_pynput(text)
            except Exception as e:
                logging.error(f"Error writing text via pynput: {e}")
                raise
        else:
            try:
                logging.info(f"Typing text directly: '{text}'")
                pyautogui.write(text, interval=0.01)
            except Exception as e:
                logging.error(f"Error writing text directly: {e}")
                raise
    else:
        original_clipboard = pyperclip.paste()
        try:
            logging.info(f"Typing text via clipboard: '{text}'")
            pyperclip.copy(text)
            time.sleep(0.2)

            if sys.platform == 'darwin':
                pyautogui.hotkey('command', 'v')
            else:
                pyautogui.hotkey('ctrl', 'v')

            time.sleep(0.2)
        except Exception as e:
            logging.error(f"Error writing text via clipboard: {e}")
            raise
        finally:
            pyperclip.copy(original_clipboard)
