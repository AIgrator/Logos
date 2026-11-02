"""Build identification for the window title, logs and Settings > About.

The release number lives in exactly one place: ``[project] version`` in
``pyproject.toml``. This module reads it at runtime (source runs) or from the
copy PyInstaller bundles next to the executable (installed builds), so bumping
a release never requires touching Python code, the spec or the installer.
"""
import logging
import re
import subprocess
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent
_cached_version = None
_cached_commit = None
_cached_commit_done = False

#: Only used when pyproject.toml cannot be read at all (broken packaging).
_FALLBACK_VERSION = "0.0.0"

_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)


def _version_file_candidates():
    """pyproject.toml copies to try, most specific first."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        # One-file build: logos.spec ships pyproject.toml at the bundle root.
        yield Path(bundle) / "pyproject.toml"
    yield _repo_root / "pyproject.toml"


def _read_version(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import tomllib
        data = tomllib.loads(text)
        version = str((data.get("project") or {}).get("version") or "").strip()
        if version:
            return version
    except Exception as e:  # noqa: BLE001 - fall back to the regex below
        logging.debug(f"Could not parse {path} as TOML: {e}")
    match = _VERSION_RE.search(text)
    return match.group(1).strip() if match else None


def get_version() -> str:
    """Release version (for example ``0.1.0``) from ``pyproject.toml``."""
    global _cached_version
    if _cached_version is not None:
        return _cached_version
    for path in _version_file_candidates():
        version = _read_version(path)
        if version:
            _cached_version = version
            return version
    logging.warning("Could not read the application version; using a fallback")
    _cached_version = _FALLBACK_VERSION
    return _cached_version


def get_commit() -> str | None:
    """Short git hash with a ``+`` when the working tree is dirty, or None.

    Only meaningful for runs from a checkout: an installed build has no
    repository, which is why the UI shows the version instead.
    """
    global _cached_commit, _cached_commit_done
    if _cached_commit_done:
        return _cached_commit
    _cached_commit_done = True
    commit = _run_git(["rev-parse", "--short", "HEAD"])
    if not commit:
        return None
    dirty = _run_git(["status", "--porcelain", "--", "src", "main.py", "prompts"]) or ""
    _cached_commit = commit + ("+" if dirty.strip() else "")
    return _cached_commit


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


def get_version_label() -> str:
    """Version as shown in the window title, e.g. ``v0.1.0``."""
    return f"v{get_version()}"


def get_build_id() -> str:
    """Version plus the commit of a source run: ``v0.1.0 (2bc1344)``."""
    commit = get_commit()
    return f"{get_version_label()} ({commit})" if commit else get_version_label()


#: Kept for callers that only need the number.
APP_VERSION = get_version()
