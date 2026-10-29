"""Read-only Git status for the workspace panel (stdlib only)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys

GIT_TIMEOUT = 15
HISTORY_LIMIT = 30

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

_GROUPS = ("staged", "changes", "conflicts", "untracked")


def read_status(root: str) -> dict:
    """Collects branch, working-tree state and recent commits for a project.

    Never writes to the repository: only `rev-parse`, `status` and `log` run.
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
        "commits": [],
        **{group: [] for group in _GROUPS},
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
    _parse_status(output, result)
    result["commits"] = _read_history(root)
    return result


def _read_history(root: str) -> list:
    ok, output, _stderr, _code = _run_git(
        root, "log", f"-n{HISTORY_LIMIT}", "--date=format:%Y-%m-%d %H:%M",
        "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%ar%x1f%s%x1f%D%x1e")
    if not ok:
        return []
    commits = []
    for record in output.split("\x1e"):
        record = record.strip("\r\n")
        if not record:
            continue
        fields = record.split("\x1f")
        if len(fields) < 6:
            continue
        full, short, author, date, relative, subject = fields[:6]
        commits.append({
            "hash": full,
            "short": short,
            "author": author,
            "date": date,
            "relative": relative,
            "subject": subject or "(no subject)",
            "refs": fields[6] if len(fields) > 6 else "",
        })
    return commits


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


def _parse_status(output: str, result: dict):
    tokens = output.split("\0")
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
                _add_entry(fields[1], fields[8], "", result)
        elif token.startswith("2 "):
            fields = token.split(" ", 9)
            if len(fields) == 10:
                original = tokens[index] if index < len(tokens) else ""
                index += 1
                _add_entry(fields[1], fields[9], original, result)
        elif token.startswith("u "):
            fields = token.split(" ", 10)
            if len(fields) == 11:
                _add_entry(fields[1], fields[10], "", result, conflict=True)
        elif token.startswith("? "):
            _add_untracked(token[2:], result)


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


def _add_entry(xy: str, path: str, original: str, result: dict,
               conflict: bool = False):
    base = _base_entry(path, original, result)
    if base is None:
        return
    if conflict:
        result["conflicts"].append(_entry(base, "U"))
        return
    staged, changes = (xy or "..")[:2].ljust(2, ".")
    if staged not in ".?":
        result["staged"].append(_entry(base, staged))
    if changes not in ".?":
        result["changes"].append(_entry(base, changes))


def _add_untracked(path: str, result: dict):
    base = _base_entry(path, "", result)
    if base is not None:
        result["untracked"].append(_entry(base, "?"))


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
