import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "read_files",
        "description": "Reads and concatenates the content of one or more files.",
        "arguments": {
            "paths": {
                "type": "LIST_OF_STRINGS",
                "description": "A list of paths to the files to read.",
                "required": True
            }
        },
        "returns": {
            "type": "STRING",
            "description": "The combined content of all the files."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def read_files(self, paths: list[str]) -> str:
    """
    Reads the content of one or more files and concatenates them.
    """
    if not paths:
        raise ValueError("Action 'read_files' failed: Missing required 'paths' argument.")

    all_content = []
    for path in paths:
        try:
            absolute_path = self._resolve_path(path)
            logging.info(f"Reading file: {absolute_path}")
            with open(absolute_path, encoding='utf-8') as f:
                content = f.read()
                all_content.append(f"--- START OF FILE: {path} ---\n{content}\n--- END OF FILE: {path} ---\n")
        except Exception as e:
            error_message = f"Error reading file '{path}': {e}"
            logging.error(error_message)
            all_content.append(f"--- ERROR READING FILE: {path} ---\n{error_message}\n--- END OF ERROR ---\n")

    return "\n".join(all_content)
