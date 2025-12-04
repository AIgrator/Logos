import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "click",
        "strategic": "Clicks a UI element. `button` may be 'left', 'middle', or 'right'.",
        "description": "Performs a single click on a UI element at the center of the provided coordinates. You can specify which mouse button to use.",
        "arguments": {
        "description": {"type": "STRING", "description": "A clear and concise description of the action being performed (e.g., 'Click the Save button').", "required": True},
        "x1": {"type": "INTEGER", "description": "The top-left x-coordinate of the element.", "required": True},
        "y1": {"type": "INTEGER", "description": "The top-left y-coordinate of the element.", "required": True},
        "x2": {"type": "INTEGER", "description": "The bottom-right x-coordinate of the element.", "required": True},
        "y2": {"type": "INTEGER", "description": "The bottom-right y-coordinate of the element.", "required": True},
        "button": {"type": "STRING", "description": "The mouse button to use for the click. Can be 'left', 'middle', or 'right'. Defaults to 'left' if not specified.", "enum": ["left", "middle", "right"], "required": False}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def click(self, **kwargs):
    if not all(k in kwargs for k in ['x1', 'y1', 'x2', 'y2']):
        raise ValueError("Action 'click' failed: Missing one or more required coordinate arguments ('x1', 'y1', 'x2', 'y2'). Example: {'name': 'click', 'arguments': {'description': 'Click button', 'x1': 100, 'y1': 100, 'x2': 200, 'y2': 150}}")
    try:
        x = (kwargs['x1'] + kwargs['x2']) / 2
        y = (kwargs['y1'] + kwargs['y2']) / 2
        button = kwargs.get('button', 'left')
        if button not in ['left', 'middle', 'right']:
            raise ValueError(f"Invalid button '{button}'. Must be 'left', 'middle', or 'right'.")

        logging.info(f"Performing {button} click at ({x}, {y}).")
        self.pyautogui.click(x, y, button=button)
    except Exception as e:
        logging.error(f"Error performing click: {e}")
        raise
