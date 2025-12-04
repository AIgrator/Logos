import logging
import os
import glob

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "find_files",
        "description": "Finds files and directories matching a specific pattern (glob) within a given path.",
        "arguments": {
            "path": {"type": "STRING", "description": "The base directory to start the search from.", "required": True},
            "pattern": {"type": "STRING", "description": "The glob pattern to match files against (e.g., '*.txt', '**/*.py').", "required": True}
        },
        "returns": {
            "type": "ARRAY",
            "description": "A list of paths for the files and directories that match the pattern.",
            "items": {"type": "STRING"}
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def find_files(self, directory: str, pattern: str) -> list[str]:
    """
    Finds files matching a glob pattern in a directory.
    """
    if not directory or not pattern:
        raise ValueError("Action 'find_files' failed: Missing 'directory' or 'pattern' argument.")

    try:
        absolute_directory = self._resolve_path(directory)
        logging.info(f"Finding files in '{absolute_directory}' matching pattern '{pattern}'")
        return glob.glob(os.path.join(absolute_directory, pattern), recursive=True)
    except Exception as e:
        logging.error(f"Error finding files: {e}")
        return [f"Error: {e}"]
