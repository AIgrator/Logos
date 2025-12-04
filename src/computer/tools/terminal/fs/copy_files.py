import logging
import shutil
import os

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "copy_files",
        "description": "Copies one or more files or directories to a new location.",
        "arguments": {
            "source_paths": {
                "type": "LIST_OF_STRINGS",
                "description": "A list of original paths of the files/directories to copy.",
                "required": True
            },
            "destination_path": {
                "type": "STRING",
                "description": "The destination directory.",
                "required": True
            }
        },
        "returns": {
            "type": "STRING",
            "description": "A summary of successful and failed copies."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def copy_files(self, source_paths: list[str], destination_path: str) -> str:
    """
    Copies one or more files or directories to a destination directory.
    """
    if not source_paths or not destination_path:
        raise ValueError("Action 'copy_files' failed: Missing 'source_paths' or 'destination_path' argument.")

    abs_destination_path = self._resolve_path(destination_path)

    if not os.path.isdir(abs_destination_path):
        return f"Error: The destination '{destination_path}' must be an existing directory."

    success_count = 0
    failures = []

    for source in source_paths:
        try:
            abs_source_path = self._resolve_path(source)
            logging.info(f"Copying '{abs_source_path}' to '{abs_destination_path}'")
            if not os.path.exists(abs_source_path):
                failures.append(f"Source path '{source}' does not exist.")
                logging.warning(f"Source path not found for copy: {source}")
                continue

            # Correctly form the destination path for the copied item
            dest_item_path = os.path.join(abs_destination_path, os.path.basename(abs_source_path))

            if os.path.isdir(abs_source_path):
                shutil.copytree(abs_source_path, dest_item_path)
            else:
                shutil.copy2(abs_source_path, dest_item_path)

            logging.info(f"Successfully copied '{source}' to '{dest_item_path}'")
            success_count += 1
        except Exception as e:
            error_message = f"Error copying '{source}': {e}"
            logging.error(error_message)
            failures.append(error_message)

    result_summary = f"Successfully copied {success_count} item(s)."
    if failures:
        result_summary += "\nFailures:\n- " + "\n- ".join(failures)

    return result_summary
