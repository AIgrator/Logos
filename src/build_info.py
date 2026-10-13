"""Build identification for logs and window title.

Answers "which code is running" without guessing: short git hash plus a
dirty marker when the working tree differs (the app runs from source, so
uncommitted edits are live too).
"""
import logging
import os
import subprocess

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cached = None


def _run_git(args, timeout=8):
    try:
        out = subprocess.run(
            ["git"] + args, cwd=_repo_root, capture_output=True,
            text=True, timeout=timeout,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception as e:
        logging.debug(f"git {' '.join(args)} failed: {e}")
    return None


def get_build_id() -> str:
    """Short hash with '+' when dirty, or 'unknown' outside git."""
    global _cached
    if _cached is not None:
        return _cached
    commit = _run_git(["rev-parse", "--short", "HEAD"])
    if not commit:
        _cached = "unknown"
        return _cached
    dirty = _run_git(["status", "--porcelain", "--", "src", "main.py", "prompts"]) or ""
    _cached = commit + ("+" if dirty.strip() else "")
    return _cached
