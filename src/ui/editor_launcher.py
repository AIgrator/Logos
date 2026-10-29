"""Open project files in the user's code editor or the system file manager."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

_EDITOR_COMMANDS = ("code", "cursor", "windsurf")

_WINDOWS_EDITOR_PATHS = (
    ("LOCALAPPDATA", ("Programs", "Microsoft VS Code", "Code.exe")),
    ("LOCALAPPDATA", ("Programs", "cursor", "Cursor.exe")),
    ("LOCALAPPDATA", ("Programs", "Windsurf", "Windsurf.exe")),
    ("PROGRAMFILES", ("Microsoft VS Code", "Code.exe")),
)


def find_editor() -> str | None:
    """Finds an installed editor executable, newest-generation first."""
    if sys.platform == "win32":
        # Prefer the real .exe: CreateProcess cannot run the `code.cmd`
        # shim that VS Code puts on PATH.
        for env_name, parts in _WINDOWS_EDITOR_PATHS:
            base = os.environ.get(env_name)
            if not base:
                continue
            candidate = os.path.join(base, *parts)
            if os.path.isfile(candidate):
                return candidate
    for name in _EDITOR_COMMANDS:
        executable = shutil.which(name)
        if executable:
            return executable
    return None


def open_path(path: str, editor: str | None = None) -> bool:
    """Opens a file or folder in the editor, falling back to the OS default."""
    target = os.path.abspath(path)
    if not os.path.exists(target):
        logging.warning(f"Cannot open missing path: {target}")
        return False
    editor = editor or find_editor()
    try:
        if editor:
            argv = [editor, target]
            if sys.platform == "win32" and editor.lower().endswith((".cmd", ".bat")):
                argv = ["cmd", "/c", *argv]
            _spawn(argv)
        elif sys.platform == "win32":
            os.startfile(target)
        elif sys.platform == "darwin":
            _spawn(["open", target])
        else:
            _spawn(["xdg-open", target])
        return True
    except OSError as exc:
        logging.warning(f"Could not open '{target}': {exc}")
        return False


def reveal_path(path: str) -> bool:
    """Shows the path in the system file manager."""
    target = os.path.abspath(path)
    try:
        if sys.platform == "win32":
            _spawn(["explorer", "/select,", target])
        elif sys.platform == "darwin":
            _spawn(["open", "-R", target])
        else:
            folder = target if os.path.isdir(target) else os.path.dirname(target)
            _spawn(["xdg-open", folder or target])
        return True
    except OSError as exc:
        logging.warning(f"Could not reveal '{target}': {exc}")
        return False


def _spawn(argv: list[str]) -> None:
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
