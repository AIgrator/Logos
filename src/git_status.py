"""Read-only Git status for the workspace panel (stdlib only)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys

GIT_TIMEOUT = 15
LINE_COUNT_LIMIT = 1024 * 1024

STATUS_LABELS = {
    "M": "modified",
    "T": "type changed",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "conflict",
    "?": "untracked",
}


def read_status(root: str) -> dict:
    """Collects the branch and uncommitted changes for a project.

    Never writes to the repository: only `rev-parse`, `status` and `diff`
    run. Entries mirror agent-app change lists: one row per uncommitted file
    with its status and added/deleted line counts.
    """
    root = os.path.abspath(root) if (root or "").strip() else ""
    result = {
        "available": False,
        "project_root": root,
        "toplevel": "",
        "branch": "",
        "commit": "",
        "detached": False,
        "upstream": "",
        "ahead": 0,
        "behind": 0,
        "error": "",
        "error_code": "",
        "files_changed": 0,
        "added": 0,
        "deleted": 0,
        "entries": [],
    }
    if not root or not os.path.isdir(root):
        result["error"] = "Open a project to see Git status."
        result["error_code"] = "no_project"
        return result

    ok, toplevel, stderr, code = _run_git(root, "rev-parse", "--show-toplevel")
    if not ok or not toplevel.strip():
        result["error"], result["error_code"] = _repository_error(stderr, code)
        return result

    ok, output, stderr, code = _run_git(
        root, "status", "--porcelain=v2", "--branch", "-z",
        "--untracked-files=normal")
    if not ok:
        if code in ("git_missing", "timeout"):
            result["error"], result["error_code"] = _repository_error(
                stderr, code)
        elif stderr.strip():
            result["error"] = "Could not read Git status. " + stderr.strip()
            result["error_code"] = "failed"
        else:
            result["error"] = "Could not read Git status."
            result["error_code"] = "failed"
        return result

    result["available"] = True
    result["toplevel"] = toplevel.strip()
    entries = _parse_status(output, result)
    _attach_stats(entries, root)
    entries.sort(key=_entry_sort_key)
    result["entries"] = entries
    result["files_changed"] = len(entries)
    result["added"] = sum(
        entry["added"] for entry in entries
        if isinstance(entry.get("added"), int))
    result["deleted"] = sum(
        entry["deleted"] for entry in entries
        if isinstance(entry.get("deleted"), int))
    return result


def _repository_error(stderr: str, code: str) -> tuple[str, str]:
    if code == "git_missing":
        return ("Git is not available. Install Git to see repository status.",
                "git_missing")
    if code == "timeout":
        return "Git did not respond in time.", "timeout"
    if "not a git repository" in (stderr or "").lower():
        return "This project is not a Git repository.", "not_repo"
    if stderr.strip():
        return "Could not read Git status. " + stderr.strip(), "failed"
    return "This project is not a Git repository.", "not_repo"


def _run_git(root: str, *args: str):
    """Runs one read-only git command; returns (ok, stdout, stderr, code)."""
    # --no-optional-locks keeps `status` from refreshing/writing the index.
    command = ["git", "--no-optional-locks", "-C", root, *args]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": GIT_TIMEOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(command, **kwargs)
    except FileNotFoundError:
        return False, "", "", "git_missing"
    except subprocess.TimeoutExpired:
        return False, "", "", "timeout"
    except OSError as exc:
        logging.warning("Could not run git: %s", exc)
        return False, "", str(exc), "failed"
    if completed.returncode != 0:
        return False, completed.stdout or "", completed.stderr or "", ""
    return True, completed.stdout or "", completed.stderr or "", ""


def _parse_status(output: str, result: dict) -> list:
    tokens = output.split("\0")
    entries = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if token.startswith("# "):
            _parse_branch_header(token[2:], result)
        elif token.startswith("1 "):
            fields = token.split(" ", 8)
            if len(fields) == 9:
                _append_entry(entries, fields[1], fields[8], "", result)
        elif token.startswith("2 "):
            fields = token.split(" ", 9)
            if len(fields) == 10:
                original = tokens[index] if index < len(tokens) else ""
                index += 1
                _append_entry(entries, fields[1], fields[9], original, result)
        elif token.startswith("u "):
            fields = token.split(" ", 10)
            if len(fields) == 11:
                _append_entry(
                    entries, "UU", fields[10], "", result, conflict=True)
        elif token.startswith("? "):
            _append_entry(entries, "??", token[2:], "", result)
    return entries


def _append_entry(entries: list, xy: str, path: str, original: str,
                  result: dict, conflict: bool = False):
    base = _base_entry(path, original, result)
    if base is None:
        return
    if conflict:
        entry = _entry(base, "U")
        entry["conflict"] = True
    elif xy == "??":
        entry = _entry(base, "?")
        entry["unstaged"] = True
    else:
        staged, unstaged = (xy or "..")[:2].ljust(2, ".")
        entry = _entry(base, _combined_code(staged, unstaged))
        entry["staged"] = staged not in ".?"
        entry["unstaged"] = unstaged not in ".?"
    entries.append(entry)


def _combined_code(staged: str, unstaged: str) -> str:
    """Net index+worktree status shown as one letter per file."""
    if unstaged == "D":
        return "D"
    if staged in ("R", "C"):
        return staged
    if staged == "A":
        return "A"
    if unstaged not in ".?":
        return unstaged
    if staged not in ".?":
        return staged
    return "M"


def _parse_branch_header(header: str, result: dict):
    key, _, value = header.partition(" ")
    value = value.strip()
    if key == "branch.oid":
        if value and value != "(initial)":
            result["commit"] = value
    elif key == "branch.head":
        if value == "(detached)":
            result["detached"] = True
            result["branch"] = "Detached HEAD"
        elif value:
            result["branch"] = value
    elif key == "branch.upstream":
        result["upstream"] = value
    elif key == "branch.ab":
        for part in value.split():
            try:
                if part.startswith("+"):
                    result["ahead"] = int(part[1:])
                elif part.startswith("-"):
                    result["behind"] = int(part[1:])
            except ValueError:
                continue


def _attach_stats(entries: list, root: str):
    tracked = [entry for entry in entries if entry.get("code") != "?"]
    stats = {}
    if tracked:
        _accumulate_numstat(
            stats, _read_numstat(root, "diff", "--numstat", "-z"))
        _accumulate_numstat(
            stats, _read_numstat(root, "diff", "--cached", "--numstat", "-z"))
    for entry in entries:
        if entry.get("code") == "?":
            lines = _count_lines(str(entry.get("absolute") or ""))
            if lines is not None:
                entry["added"], entry["deleted"] = lines, 0
            continue
        values = stats.get(entry.get("path") or "")
        if values is not None:
            entry["added"], entry["deleted"] = values


def _read_numstat(root: str, *args: str) -> dict:
    ok, output, _stderr, _code = _run_git(root, *args)
    if not ok:
        return {}
    stats = {}
    tokens = output.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        parts = token.split("\t", 2)
        if len(parts) < 3:
            continue
        added, deleted, path = parts
        if not path:
            # Rename/copy records carry the old and new paths as NUL tokens.
            if index + 1 >= len(tokens):
                break
            path = tokens[index + 1]
            index += 2
        if not path:
            continue
        if added == "-" or deleted == "-":
            stats[path] = (None, None)
            continue
        try:
            stats[path] = (int(added), int(deleted))
        except ValueError:
            stats[path] = (None, None)
    return stats


def _accumulate_numstat(target: dict, values: dict):
    """Adds staged and unstaged counts for the same file together."""
    for path, (added, deleted) in values.items():
        if path not in target:
            target[path] = (added, deleted)
            continue
        current_added, current_deleted = target[path]
        if current_added is None or added is None:
            target[path] = (None, None)
        else:
            target[path] = (current_added + added, current_deleted + deleted)


def _count_lines(path: str):
    """Counts added lines for an untracked file; None means no stat."""
    try:
        if not path or not os.path.isfile(path):
            return None
        if os.path.getsize(path) > LINE_COUNT_LIMIT:
            return None
        with open(path, "rb") as handle:
            data = handle.read(LINE_COUNT_LIMIT)
    except OSError:
        return None
    if b"\0" in data:
        return None
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _entry_sort_key(entry: dict):
    return (0 if entry.get("conflict") else 1,
            str(entry.get("display") or "").casefold())


def _base_entry(path: str, original: str, result: dict):
    if not path:
        return None
    toplevel = result["toplevel"] or result["project_root"]
    absolute = os.path.join(toplevel, *path.split("/"))
    if _is_app_data(absolute, result["project_root"]):
        return None
    return {
        "path": path,
        "display": _display_path(absolute, result["project_root"], path),
        "absolute": absolute,
        "original": original,
    }


def _entry(base: dict, code: str) -> dict:
    return {
        **base,
        "code": code,
        "label": STATUS_LABELS.get(code, "changed"),
        "conflict": False,
        "staged": False,
        "unstaged": False,
        "added": None,
        "deleted": None,
    }


def _is_app_data(absolute: str, project_root: str) -> bool:
    """Hides Logos' own .logos data (journals, attachments) from the list."""
    app_data = os.path.normcase(
        os.path.normpath(os.path.join(project_root, ".logos")))
    absolute = os.path.normcase(os.path.normpath(absolute))
    return absolute == app_data or absolute.startswith(app_data + os.sep)


def _display_path(absolute: str, project_root: str, repo_path: str) -> str:
    try:
        relative = os.path.relpath(absolute, project_root)
    except ValueError:
        return repo_path
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return repo_path
    return relative.replace(os.sep, "/")
