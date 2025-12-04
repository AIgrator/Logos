import logging
import pyautogui

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "double_click",
        "strategic":"Double mouse click on UI element.",
        "description": "Performs a double left-click on a UI element at the center of the provided coordinates.",
        "arguments": {
            "description": {"type": "STRING", "description": "A clear and concise description of the action being performed (e.g., 'Double-click the icon to open the file').", "required": True},
            "x1": {"type": "INTEGER", "description": "The top-left x-coordinate of the element.", "required": True},
            "y1": {"type": "INTEGER", "description": "The top-left y-coordinate of the element.", "required": True},
            "x2": {"type": "INTEGER", "description": "The bottom-right x-coordinate of the element.", "required": True},
            "y2": {"type": "INTEGER", "description": "The bottom-right y-coordinate of the element.", "required": True}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def double_click(self, **kwargs):
    if not all(k in kwargs for k in ['x1', 'y1', 'x2', 'y2']):
        raise ValueError("Action 'double_click' failed: Missing one or more required coordinate arguments ('x1', 'y1', 'x2', 'y2'). Example: {'name': 'double_click', 'arguments': {'description': 'Open file', 'x1': 100, 'y1': 100, 'x2': 200, 'y2': 150}}")
    try:
        x = (kwargs['x1'] + kwargs['x2']) / 2
        y = (kwargs['y1'] + kwargs['y2']) / 2
        logging.info(f"Performing double click at ({x}, {y}).")
        pyautogui.doubleClick(x, y)
    except Exception as e:
        logging.error(f"Error performing double click: {e}")
        raise
