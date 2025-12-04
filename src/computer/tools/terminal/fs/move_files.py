import logging
import shutil
import os

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "move_files",
        "description": "Moves one or more files or directories to a new location.",
        "arguments": {
            "source_paths": {
                "type": "LIST_OF_STRINGS",
                "description": "A list of original paths of the files/directories.",
                "required": True
            },
            "destination_path": {
                "type": "STRING",
                "description": "The destination directory or new path for a single source.",
                "required": True
            }
        },
        "returns": {
            "type": "STRING",
            "description": "A summary of successful and failed moves."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def move_files(self, source_paths: list[str], destination_path: str) -> str:
    """
    Moves one or more files or directories.
    """
    if not source_paths or not destination_path:
        raise ValueError("Action 'move_files' failed: Missing 'source_paths' or 'destination_path' argument.")

    abs_destination_path = self._resolve_path(destination_path)

    if len(source_paths) > 1 and not os.path.isdir(abs_destination_path):
        return f"Error: When moving multiple items, the destination '{destination_path}' must be an existing directory."

    success_count = 0
    failures = []

    for source in source_paths:
        try:
            abs_source_path = self._resolve_path(source)
            logging.info(f"Moving '{abs_source_path}' to '{abs_destination_path}'")
            shutil.move(abs_source_path, abs_destination_path)
            logging.info(f"Successfully moved '{source}' to '{destination_path}'")
            success_count += 1
        except Exception as e:
            error_message = f"Error moving '{source}': {e}"
            logging.error(error_message)
            failures.append(error_message)

    result_summary = f"Successfully moved {success_count} item(s)."
    if failures:
        result_summary += "\nFailures:\n- " + "\n- ".join(failures)

    return result_summary
