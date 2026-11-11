import logging
import os

from src.computer.computer import tool_meta
from src.computer.tools.terminal.fs._walk import MAX_RESULTS, matches, walk_entries


@tool_meta(
    tool={
        "name": "find_files",
        "description": "Finds files (and folders) matching a glob pattern below a path. The search is recursive: '*.py' matches that name at any depth, '**' spans folders, and vendor or cache folders (.git, venv, node_modules, build, dist, __pycache__, ...) are skipped.",
        "arguments": {
            "path": {"type": "STRING", "description": "The base directory to start the search from.", "required": True},
            "pattern": {"type": "STRING", "description": "The glob pattern to match entries against (e.g., '*.txt', '**/*.py', 'src/**/test_*.py').", "required": True}
        },
        "returns": {
            "type": "ARRAY",
            "description": "A list of paths for the files and directories that match the pattern.",
            "items": {"type": "STRING"}
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def find_files(self, path: str = ".", pattern: str = "",
               directory: str | None = None) -> list[str]:
    """
    Finds files matching a glob pattern in a directory.

    ``path`` is the name the schema declares; ``directory`` is accepted as a
    compatibility alias, because the call used to fail with
    ``find_files() got an unexpected keyword argument 'path'``.

    The walk skips vendor folders and stops after ``MAX_RESULTS`` entries: a
    plain ``glob`` over a project root returned a venv's ``site-packages``,
    which the history budget pruned away on the next request, taking the file
    reads the model still needed with it.
    """
    target = directory if directory is not None else path
    if not target or not pattern:
        raise ValueError("Action 'find_files' failed: Missing 'path' or 'pattern' argument.")

    try:
        absolute_directory = self._resolve_path(target)
        logging.info(f"Finding files in '{absolute_directory}' matching pattern '{pattern}'")
        if not os.path.isdir(absolute_directory):
            return []
        found = [entry for entry, parts in walk_entries(absolute_directory, include_dirs=True)
                 if matches(parts, pattern)]
        if len(found) > MAX_RESULTS:
            note = (f"[{len(found)} matches; showing the first {MAX_RESULTS} - "
                    "narrow the pattern or the path]")
            return found[:MAX_RESULTS] + [note]
        return found
    except Exception as e:
        logging.error(f"Error finding files: {e}")
        return [f"Error: {e}"]
