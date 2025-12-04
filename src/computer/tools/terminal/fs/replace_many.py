import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "replace_many",
        "description": "Performs multiple, non-overlapping text replacements in a single file. Each replacement requires the exact text to be replaced, including context.",
        "arguments": {
            "path": {"type": "STRING", "description": "The path to the file to edit.", "required": True},
            "replacements": {
                "type": "ARRAY",
                "description": "A list of replacement operations.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "old_string": {"type": "STRING", "description": "The exact block of text to be replaced.", "required": True},
                        "new_string": {"type": "STRING", "description": "The new block of text to replace the old one.", "required": True}
                    }
                },
                "required": True
            }
        },
        "returns": {
            "type": "STRING",
            "description": "A success or error message."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def replace_many(self, path: str, replacements: list) -> str:
    """
    Performs multiple replacements in a single file.
    """
    if not all([path, replacements]):
        raise ValueError("Action 'replace_many' failed: Missing 'path' or 'replacements' argument.")

    try:
        absolute_path = self._resolve_path(path)
        logging.info(f"Performing multiple replacements in file: {absolute_path}")
        with open(absolute_path, encoding='utf-8') as f:
            content = f.read()

        for item in replacements:
            old_string = item.get('old_string')
            new_string = item.get('new_string')

            if old_string is None or new_string is None:
                return "Error: Each replacement item must have 'old_string' and 'new_string' keys."

            if old_string not in content:
                # Revert all changes if one fails to avoid partial writes.
                logging.error(f"Aborting replace_many: old_string not found: {old_string[:100]}...")
                return f"Error: A string to be replaced was not found. No changes were made. String: {old_string}"

            if content.count(old_string) > 1:
                logging.error(f"Aborting replace_many: old_string is not unique: {old_string[:100]}...")
                return f"Error: A string to be replaced appears multiple times. Please provide more context. String: {old_string}"

            content = content.replace(old_string, new_string)

        with open(absolute_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return f"Successfully performed {len(replacements)} replacements in {path}"
    except Exception as e:
        return f"Error: {e}"
