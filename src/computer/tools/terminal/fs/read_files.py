import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "read_files",
        "description": "Reads and concatenates the content of one or more files. Large files can be read in slices with offset/limit instead of loading them whole.",
        "arguments": {
            "paths": {
                "type": "LIST_OF_STRINGS",
                "description": "A list of paths to the files to read.",
                "required": True
            },
            "offset": {
                "type": "INTEGER",
                "description": "Optional 1-based line number to start from in every file. Omit to start at the beginning.",
                "required": False
            },
            "limit": {
                "type": "INTEGER",
                "description": "Optional maximum number of lines to return per file, counted from offset. Omit to read through the end of the file.",
                "required": False
            }
        },
        "returns": {
            "type": "STRING",
            "description": "The combined content of all the files. A ranged read is numbered per line and starts with a '--- START OF FILE: <path> (lines a-b of N) ---' header."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def read_files(self, paths: list[str], offset: int | None = None, limit: int | None = None) -> str:
    """
    Reads the content of one or more files and concatenates them.

    Without offset/limit the whole file is returned. With them only the
    1-based line range is returned, numbered per line, so a large file can be
    inspected in slices without paying for its entire content.
    """
    if not paths:
        raise ValueError("Action 'read_files' failed: Missing required 'paths' argument.")

    try:
        offset = int(offset) if offset is not None else None
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        raise ValueError("Action 'read_files' failed: 'offset' and 'limit' must be whole numbers.") from None
    if offset is not None and offset < 1:
        raise ValueError("Action 'read_files' failed: 'offset' is a 1-based line number.")
    if limit is not None and limit < 1:
        raise ValueError("Action 'read_files' failed: 'limit' must be at least 1 line.")

    all_content = []
    for path in paths:
        try:
            absolute_path = self._resolve_path(path)
            logging.info(f"Reading file: {absolute_path}")
            with open(absolute_path, encoding='utf-8') as f:
                if offset is None and limit is None:
                    content = f.read()
                    all_content.append(f"--- START OF FILE: {path} ---\n{content}\n--- END OF FILE: {path} ---\n")
                    continue
                lines = f.readlines()
                total = len(lines)
                first = offset or 1
                if first > total:
                    all_content.append(
                        f"--- START OF FILE: {path} ---\n"
                        f"Offset {first} is past the end: the file has {total} line(s).\n"
                        f"--- END OF FILE: {path} ---\n")
                    continue
                last = total if limit is None else min(total, first - 1 + limit)
                body = "".join(
                    f"{i}\t{lines[i - 1].rstrip(chr(10) + chr(13))}\n"
                    for i in range(first, last + 1))
                all_content.append(
                    f"--- START OF FILE: {path} (lines {first}-{last} of {total}) ---\n"
                    f"{body}"
                    f"--- END OF FILE: {path} ---\n")
        except Exception as e:
            error_message = f"Error reading file '{path}': {e}"
            logging.error(error_message)
            all_content.append(f"--- ERROR READING FILE: {path} ---\n{error_message}\n--- END OF ERROR ---\n")

    return "\n".join(all_content)
