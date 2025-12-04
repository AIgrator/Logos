import logging
import pyautogui

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "scroll",
        "description": "Scrolls the mouse wheel up or down. This is for small, precise scrolls.",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of the scroll action (e.g., 'Scrolling down to see more content.')", "required": True},
            "direction": {"type": "STRING", "description": "The direction to scroll: a string, either 'up' or 'down'", "required": True},
            "amount": {"type": "INTEGER", "description": "The number of units to scroll (must be an INTEGER, e.g., 100, not 100.0).", "required": True}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def scroll(self, **kwargs):
    direction = kwargs.get('direction')
    amount = kwargs.get('amount')
    if direction is None or amount is None:
        raise ValueError("Action 'scroll' failed: Missing 'direction' or 'amount' argument. Example: {'name': 'scroll', 'arguments': {'description': 'Scroll down', 'direction': 'down', 'amount': 100}}")

    try:
        scroll_amount = int(amount)
        pyautogui_amount = scroll_amount if direction == 'up' else -scroll_amount

        logging.info(f"Scrolling {direction} by {scroll_amount} units.")
        pyautogui.scroll(pyautogui_amount)
    except Exception as e:
        logging.error(f"Error performing scroll: {e}")
        raise
