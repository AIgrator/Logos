import logging
import pyautogui

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "scroll_page",
        "description": "Scrolls up or down by one full page using PageUp/PageDown keys. Use this for long scrolls.",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of the page scroll action (e.g., 'Scroll down one page.')", "required": True},
            "direction": {"type": "STRING", "description": "The direction to scroll: a string, either 'up' or 'down'", "required": True}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def scroll_page(self, **kwargs):
    direction = kwargs.get('direction')
    if direction not in ['up', 'down']:
        raise ValueError("Action 'scroll_page' failed: 'direction' must be 'up' or 'down'. Example: {'name': 'scroll_page', 'arguments': {'description': 'Next page', 'direction': 'down'}}")

    try:
        key_to_press = 'pageup' if direction == 'up' else 'pagedown'
        logging.info(f"Performing page scroll {direction} by pressing '{key_to_press}'.")
        pyautogui.press(key_to_press)
    except Exception as e:
        logging.error(f"Error performing page scroll: {e}")
        raise
