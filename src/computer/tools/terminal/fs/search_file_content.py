import logging
import os
import re

from src.computer.computer import tool_meta

_SKIP_DIRS = {
    '__pycache__', '.git', '.hg', '.svn', '.venv', 'venv', 'env',
    'node_modules', '.idea', '.vscode', '.mypy_cache', '.pytest_cache',
    '.ruff_cache', 'build', 'dist', '.tox', '.eggs',
}
_SKIP_EXTENSIONS = {
    '.pyc', '.pyo', '.pyd', '.dll', '.exe', '.bin', '.so', '.dylib',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp', '.tiff',
    '.pdf', '.zip', '.7z', '.rar', '.gz', '.tar', '.bz2', '.xz',
    '.mp3', '.mp4', '.mov', '.avi', '.mkv', '.wav', '.flac',
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
    '.db', '.sqlite', '.sqlite3', '.lock',
}
_MAX_FILE_BYTES = 2 * 1024 * 1024


def _is_binary(file_path: str) -> bool:
    try:
        with open(file_path, 'rb') as f:
            return b'\x00' in f.read(4096)
    except OSError:
        return True


@tool_meta(
    tool={
        "name": "search_file_content",
        "description": "Searches for a pattern in the content of files in a directory.",
        "arguments": {
            "pattern": {"type": "STRING", "description": "The regex pattern to search for.", "required": True},
            "path": {"type": "STRING", "description": "The directory to search in. Defaults to current directory.", "required": False}
        },
        "returns": {
            "type": "ARRAY",
            "description": "A list of strings, where each string is a line containing a match, prefixed with the file path.",
            "items": {"type": "STRING"}
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def search_file_content(self, pattern: str, path: str = '.', directory: str | None = None) -> list:
    """
    Searches for a pattern in files in a directory.

    ``directory`` is accepted as a compatibility alias for older callers.
    """
    if not pattern:
        raise ValueError("Action 'search_file_content' failed: Missing 'pattern' argument.")

    target = directory if directory is not None and path == '.' else path
    matches = []
    try:
        absolute_directory = self._resolve_path(target)
        logging.info(f"Searching for pattern '{pattern}' in directory '{absolute_directory}'")
        for root, dirs, files in os.walk(absolute_directory):
            dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS]
            for file in files:
                if os.path.splitext(file)[1].lower() in _SKIP_EXTENSIONS:
                    continue
                try:
                    file_path = os.path.join(root, file)
                    if os.path.getsize(file_path) > _MAX_FILE_BYTES:
                        continue
                    if _is_binary(file_path):
                        continue
                    with open(file_path, encoding='utf-8', errors='ignore') as f:
                        for i, line in enumerate(f):
                            if re.search(pattern, line):
                                matches.append(f"{file_path}:{i+1}:{line.strip()}")
                except Exception:
                    # Ignore files that can't be opened or read
                    continue
        return matches
    except Exception as e:
        logging.error(f"Error searching for pattern '{pattern}' in '{target}': {e}")
        return [f"Error: {e}"]
