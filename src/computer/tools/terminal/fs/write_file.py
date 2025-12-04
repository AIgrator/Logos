import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "write_file",
        "description": "Writes content to a specified file, overwriting it if it exists.",
        "arguments": {
            "path": {"type": "STRING", "description": "The path to the file to write to.", "required": True},
            "content": {"type": "STRING", "description": "The content to write to the file.", "required": True}
        },
        "returns": {
            "type": "STRING",
            "description": "A success or error message."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def write_file(self, path: str, content: str) -> str:
    """
    Writes content to a file.
    """
    if not path or content is None:
        raise ValueError("Action 'write_file' failed: Missing 'path' or 'content' argument.")

    try:
        absolute_path = self._resolve_path(path)
        logging.info(f"Writing to file: {absolute_path}")
        with open(absolute_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Successfully wrote to {path}"
    except Exception as e:
        logging.error(f"Error writing to file '{path}': {e}")
        return f"Error: {e}"
