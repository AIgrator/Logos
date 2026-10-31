"""Application directories shared by source runs and frozen builds."""
import os
import sys

APP_FOLDER_NAME = "Logos"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_root() -> str:
    """Writable application folder.

    An installed one-file build may sit in a read-only location, so settings,
    logs, cache and the materialized prompts/assets live in the per-user
    AppData folder. Source runs keep using the repository root.
    """
    if not is_frozen():
        return source_root()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return os.path.join(base, APP_FOLDER_NAME)
    return os.path.dirname(sys.executable)
