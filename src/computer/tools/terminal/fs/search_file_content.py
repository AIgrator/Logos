import logging
import os
import re

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "search_file_content",
        "description": "Searches for a pattern in the content of files in a directory.",
        "arguments": {
            "pattern": {"type": "STRING", "description": "The regex pattern to search for.", "required": True},
            "directory": {"type": "STRING", "description": "The directory to search in. Defaults to current directory.", "required": False}
        },
        "returns": {
            "type": "ARRAY",
            "description": "A list of strings, where each string is a line containing a match, prefixed with the file path.",
            "items": {"type": "STRING"}
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def search_file_content(self, pattern: str, directory: str = '.') -> list:
    """
    Searches for a pattern in files in a directory.
    """
    if not pattern:
        raise ValueError("Action 'search_file_content' failed: Missing 'pattern' argument.")

    matches = []
    try:
        absolute_directory = self._resolve_path(directory)
        logging.info(f"Searching for pattern '{pattern}' in directory '{absolute_directory}'")
        for root, _, files in os.walk(absolute_directory):
            for file in files:
                try:
                    file_path = os.path.join(root, file)
                    with open(file_path, encoding='utf-8', errors='ignore') as f:
                        for i, line in enumerate(f):
                            if re.search(pattern, line):
                                matches.append(f"{file_path}:{i+1}:{line.strip()}")
                except Exception:
                    # Ignore files that can't be opened or read
                    continue
        return matches
    except Exception as e:
        logging.error(f"Error searching for pattern '{pattern}' in '{directory}': {e}")
        return [f"Error: {e}"]
