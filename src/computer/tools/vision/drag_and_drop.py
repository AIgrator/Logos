import logging
import pyautogui

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "drag_and_drop",
        "strategic": "Drags an element from a start location and drops it at an end location.",
        "description": "Performs a drag-and-drop action. It moves the mouse to the center of the start coordinates, presses the left mouse button, drags the mouse to the center of the end coordinates, and then releases the button.",
        "arguments": {
            "description": {"type": "STRING", "description": "A clear description of the drag-and-drop action (e.g., 'Move the file icon to the trash icon').", "required": True},
            "x1_start": {"type": "INTEGER", "description": "The top-left x-coordinate of the element to start dragging.", "required": True},
            "y1_start": {"type": "INTEGER", "description": "The top-left y-coordinate of the element to start dragging.", "required": True},
            "x2_start": {"type": "INTEGER", "description": "The bottom-right x-coordinate of the element to start dragging.", "required": True},
            "y2_start": {"type": "INTEGER", "description": "The bottom-right y-coordinate of the element to start dragging.", "required": True},
            "x1_end": {"type": "INTEGER", "description": "The top-left x-coordinate of the target area to drop onto.", "required": True},
            "y1_end": {"type": "INTEGER", "description": "The top-left y-coordinate of the target area to drop onto.", "required": True},
            "x2_end": {"type": "INTEGER", "description": "The bottom-right x-coordinate of the target area to drop onto.", "required": True},
            "y2_end": {"type": "INTEGER", "description": "The bottom-right y-coordinate of the target area to drop onto.", "required": True}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def drag_and_drop(self, **kwargs):
    required_keys = ['x1_start', 'y1_start', 'x2_start', 'y2_start', 'x1_end', 'y1_end', 'x2_end', 'y2_end']
    if not all(k in kwargs for k in required_keys):
        raise ValueError(f"Action 'drag_and_drop' failed: Missing one or more required coordinate arguments. Required: {required_keys}")
    try:
        start_x = (kwargs['x1_start'] + kwargs['x2_start']) / 2
        start_y = (kwargs['y1_start'] + kwargs['y2_start']) / 2
        end_x = (kwargs['x1_end'] + kwargs['x2_end']) / 2
        end_y = (kwargs['y1_end'] + kwargs['y2_end']) / 2

        logging.info(f"Performing drag and drop from ({start_x}, {start_y}) to ({end_x}, {end_y}).")

        pyautogui.moveTo(start_x, start_y, duration=0.2)
        pyautogui.mouseDown(button='left')
        pyautogui.moveTo(end_x, end_y, duration=0.5)
        pyautogui.mouseUp(button='left')

    except Exception as e:
        logging.error(f"Error performing drag and drop: {e}")
        raise
