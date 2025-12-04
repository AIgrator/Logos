import logging
import os

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "delete_files",
        "description": "Deletes one or more specified files. This action is irreversible.",
        "arguments": {
            "paths": {
                "type": "LIST_OF_STRINGS",
                "description": "A list of paths to the files to delete.",
                "required": True
            }
        },
        "returns": {
            "type": "STRING",
            "description": "A summary of successful and failed deletions."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def delete_files(self, paths: list[str]) -> str:
    """
    Deletes one or more files.
    """
    if not paths:
        raise ValueError("Action 'delete_files' failed: Missing required 'paths' argument.")

    success_count = 0
    failures = []

    for path in paths:
        try:
            absolute_path = self._resolve_path(path)
            logging.info(f"Attempting to delete file: {absolute_path}")
            if not os.path.exists(absolute_path):
                failures.append(f"'{path}' not found.")
                logging.warning(f"File not found for deletion: {absolute_path}")
                continue
            if os.path.isdir(absolute_path):
                failures.append(f"'{path}' is a directory, not a file.")
                logging.warning(f"Attempted to delete a directory with delete_files: {absolute_path}")
                continue

            os.remove(absolute_path)
            logging.info(f"Successfully deleted file: {absolute_path}")
            success_count += 1
        except Exception as e:
            error_message = f"Error deleting file '{path}': {e}"
            logging.error(error_message)
            failures.append(error_message)

    result_summary = f"Successfully deleted {success_count} file(s)."
    if failures:
        result_summary += "\nFailures:\n- " + "\n- ".join(failures)

    return result_summary
