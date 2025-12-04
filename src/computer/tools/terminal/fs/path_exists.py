import logging
import os

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "path_exists",
        "description": "Checks if a file or directory exists at the specified path.",
        "arguments": {
            "path": {"type": "STRING", "description": "The path to check for existence.", "required": True}
        },
        "returns": {
            "type": "BOOLEAN",
            "description": "True if the path exists, False otherwise."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def path_exists(self, path: str) -> bool:
    """
    Checks if a file or directory exists.
    """
    if not path:
        raise ValueError("Action 'path_exists' failed: Missing required 'path' argument.")

    try:
        absolute_path = self._resolve_path(path)
        logging.info(f"Checking existence of: {absolute_path}")
        return os.path.exists(absolute_path)
    except Exception as e:
        logging.error(f"Error checking path '{path}': {e}")
        return False
