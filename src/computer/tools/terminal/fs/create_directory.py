import logging
import os

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "create_directory",
        "description": "Creates a new directory at the specified path. It can create parent directories if they don't exist.",
        "arguments": {
            "path": {"type": "STRING", "description": "The path where the new directory should be created.", "required": True}
        },
        "returns": {
            "type": "STRING",
            "description": "A success or error message."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def create_directory(self, path: str) -> str:
    """
    Creates a new directory.
    """
    if not path:
        raise ValueError("Action 'create_directory' failed: Missing required 'path' argument.")

    try:
        absolute_path = self._resolve_path(path)
        logging.info(f"Creating directory: {absolute_path}")
        os.makedirs(absolute_path, exist_ok=True)
        return f"Directory '{path}' created successfully."
    except Exception as e:
        error_message = f"Error creating directory '{path}': {e}"
        logging.error(error_message)
        return error_message
