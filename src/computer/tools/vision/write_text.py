import logging
import time
import sys
import pyautogui
import pyperclip

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "write_text",
        "description": "Types text into the currently focused input field. On Windows types key-by-key via pynput by default (works with any alphabet); 'clipboard' pastes via Ctrl+V for very long texts instead.",
        "arguments": {
            "description": {"type": "STRING", "description": "e.g., 'Typing the filename 'mydocument.txt'", "required": True},
            "text": {"type": "STRING", "description": "The text to type.", "required": True},
            "method": {"type": "STRING", "description": "The typing method to use. 'pynput' types via key events (default on Windows, any alphabet), 'clipboard' pastes text for speed. Defaults to the most reliable method for the OS.", "required": False, "enum": ["pynput", "clipboard", "direct"]},
            "press_enter": {"type": "BOOLEAN", "description": "If true, presses Enter right after typing (for terminal/console commands). Prefer this over a separate press_key step: a typed-but-unsubmitted command hangs.", "required": False}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def write_text(self, **kwargs):
    text = kwargs.get('text')
    method = kwargs.get('method')
    press_enter = kwargs.get('press_enter', False)

    if text is None:
        raise ValueError("Action 'write_text' failed: Missing required 'text' argument. Example: {'name': 'write_text', 'arguments': {'description': 'Enter username', 'text': 'admin'}}")

    if method is None:
        # Default: most reliable method per OS.
        method = 'pynput' if sys.platform == 'win32' else 'direct'

    if method == 'clipboard':
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
    elif method == 'pynput' and sys.platform == 'win32':
        from src.computer.tools.win_keys import send_text_pynput
        try:
            logging.info(f"Typing text via pynput: '{text}'")
            send_text_pynput(text)
        except Exception as e:
            logging.error(f"Error writing text via pynput: {e}")
            raise
    else:
        # 'direct' (or pynput requested off-Windows): legacy key simulation.
        try:
            logging.info(f"Typing text directly: '{text}'")
            pyautogui.write(text, interval=0.01)
        except Exception as e:
            logging.error(f"Error writing text directly: {e}")
            raise

    if press_enter:
        # Submit immediately: a typed-but-unsubmitted console command hangs.
        time.sleep(0.2)
        if sys.platform == 'win32':
            try:
                from src.computer.tools.win_keys import press_key_pynput
                logging.info("Pressing Enter after typing (press_enter=true).")
                press_key_pynput('enter')
                return
            except Exception as e:
                logging.warning(f"pynput Enter failed, falling back to pyautogui: {e}")
        pyautogui.press('enter')
