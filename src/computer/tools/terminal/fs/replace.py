import logging

from src.computer.computer import tool_meta


@tool_meta(
    tool={
        "name": "replace",
        "description": "Replaces all occurrences of a string in a file.",
        "arguments": {
            "file_path": {"type": "STRING", "description": "Path to the file.", "required": True},
            "old_string": {"type": "STRING", "description": "Text to find.", "required": True},
            "new_string": {"type": "STRING", "description": "Replacement text.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "terminal"}
)
def replace(self, file_path: str, old_string: str, new_string: str) -> str:
    """
    Replaces all occurrences of a string in a file.
    """
    if not all([file_path, old_string, new_string]):
        raise ValueError("Action 'replace' failed: Missing one or more required arguments.")

    try:
        absolute_path = self._resolve_path(file_path)
        logging.info(f"Replacing text in file: {absolute_path}")
        with open(absolute_path, encoding='utf-8') as f:
            content = f.read()

        new_content = content.replace(old_string, new_string)

        with open(absolute_path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        return f"Successfully replaced text in '{file_path}'."
    except Exception as e:
        error_message = f"Error replacing text in '{file_path}': {e}"
        logging.error(error_message)
        return error_message
