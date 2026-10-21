import logging
import os

from src.computer.computer import tool_meta

_SKIP_ENTRIES = {
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    '.tox', '.eggs', '.DS_Store', 'Thumbs.db',
}


@tool_meta(
    tool={
        "name": "list_directory",
        "description": "Lists the contents of a specified directory.",
        "arguments": {
            "path": {"type": "STRING", "description": "The path to the directory to list.", "required": True}
        },
        "returns": {
            "type": "ARRAY",
            "description": "A list of files and directories.",
            "items": {"type": "STRING"}
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def list_directory(self, path: str) -> list:
    """
    Lists the contents of a directory.
    """
    if not path:
        raise ValueError("Action 'list_directory' failed: Missing required 'path' argument.")

    try:
        absolute_path = self._resolve_path(path)
        logging.info(f"Listing directory: {absolute_path}")
        return [entry for entry in os.listdir(absolute_path)
                if entry not in _SKIP_ENTRIES]
    except Exception as e:
        logging.error(f"Error listing directory '{path}': {e}")
        return [f"Error: {e}"]
