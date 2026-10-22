"""
Project manager: keeps track of the active project directory and stores all
per-project data (chats, context, attachments) inside a ``.logos`` folder that
lives next to the project itself.

Global state (which project is active, the recent-project list) is kept in the
application settings file; everything project-specific stays portable.
"""
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime


PROJECT_DIRNAME = ".logos"
PROJECT_META_FILE = "project.json"
MAX_RECENT_PROJECTS = 12


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean_path(path) -> str:
    try:
        return os.path.abspath(os.path.expanduser(str(path)))
    except Exception:
        return ""


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return _clean_path(os.path.dirname(sys.executable))
    return _clean_path(os.path.dirname(os.path.dirname(__file__)))


APP_DIR = _app_dir()


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _session_title(messages: list) -> str:
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        line = " ".join(content.split())
        if line:
            return line[:80] + ("..." if len(line) > 80 else "")
    return "Empty session"


class ProjectManager:
    """Resolves, creates and remembers the active project directory."""

    def __init__(self, settings_manager):
        self.settings_manager = settings_manager
        self._active_root = ""
        self._recent = []
        self._load_from_settings()

    # ------------------------------------------------------------------ state
    def _load_from_settings(self):
        active = self.settings_manager.get("active_project", "") or ""
        active = _clean_path(active)
        if active and os.path.isdir(active) and not self.is_app_dir(active):
            self._active_root = active
        else:
            self._active_root = ""

        recent = self.settings_manager.get("recent_projects", []) or []
        cleaned = []
        for item in recent:
            if not isinstance(item, dict):
                continue
            path = _clean_path(item.get("path", ""))
            if not path or self.is_app_dir(path):
                continue
            cleaned.append({
                "path": path,
                "name": item.get("name") or os.path.basename(path) or path,
                "last_opened": item.get("last_opened", ""),
            })
        self._recent = cleaned

    def _persist(self):
        try:
            self.settings_manager.set_many({
                "active_project": self._active_root,
                "recent_projects": self._recent,
            }, emit=False)
        except Exception as exc:
            logging.warning(f"Could not persist project bookkeeping: {exc}")

    def get_active_root(self) -> str:
        return self._active_root

    @staticmethod
    def is_app_dir(path: str) -> bool:
        """The Logos installation folder is an application, never a project."""
        return bool(path) and _same_path(path, APP_DIR)

    def get_active_name(self) -> str:
        if not self._active_root:
            return ""
        return os.path.basename(self._active_root.rstrip("\\/")) or self._active_root

    def recent_projects(self) -> list:
        return list(self._recent)

    # --------------------------------------------------------------- projects
    @staticmethod
    def project_dir(root: str) -> str:
        return os.path.join(root, PROJECT_DIRNAME)

    def sessions_dir(self, root: str = "") -> str:
        root = root or self._active_root
        return os.path.join(self.project_dir(root), "sessions")

    def context_dir(self, root: str = "") -> str:
        root = root or self._active_root
        return os.path.join(self.project_dir(root), "context")

    def attachments_dir(self, root: str = "") -> str:
        root = root or self._active_root
        return os.path.join(self.project_dir(root), "attachments")

    @staticmethod
    def _write_json_atomic(path: str, data) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def open_project(self, path: str, persist: bool = True) -> dict:
        """Validates/creates a project directory and makes it the active one."""
        root = _clean_path(path)
        if not root or not os.path.isdir(root):
            raise ValueError(f"Not a directory: {path}")
        if self.is_app_dir(root):
            raise ValueError("The Logos installation folder cannot be used as a project. "
                             "Choose another folder.")

        logos_dir = self.project_dir(root)
        for sub in (
            logos_dir,
            self.sessions_dir(root),
            self.context_dir(root),
            self.attachments_dir(root),
        ):
            os.makedirs(sub, exist_ok=True)

        # Keep project data out of the project's git repository without touching
        # the user's root .gitignore.
        gitignore_path = os.path.join(logos_dir, ".gitignore")
        if not os.path.exists(gitignore_path):
            try:
                with open(gitignore_path, "w", encoding="utf-8") as fh:
                    fh.write("*\n!.gitignore\n")
            except Exception as exc:
                logging.warning(f"Could not write {gitignore_path}: {exc}")

        meta_path = os.path.join(logos_dir, PROJECT_META_FILE)
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh) or {}
            except Exception as exc:
                logging.warning(f"Could not read {meta_path}: {exc}")
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.setdefault("id", uuid.uuid4().hex)
        meta["name"] = os.path.basename(root.rstrip("\\/")) or root
        meta.setdefault("created_at", _now_iso())
        meta["path"] = root
        meta["last_opened"] = _now_iso()
        try:
            self._write_json_atomic(meta_path, meta)
        except Exception as exc:
            logging.warning(f"Could not write {meta_path}: {exc}")

        self._active_root = root
        self._touch_recent(root, meta.get("name") or root)
        if persist:
            self._persist()
        logging.info(f"Active project set to: {root}")
        return meta

    def _touch_recent(self, root: str, name: str):
        entry = {"path": root, "name": name, "last_opened": _now_iso()}
        self._recent = [item for item in self._recent if item.get("path") != root]
        self._recent.insert(0, entry)
        del self._recent[MAX_RECENT_PROJECTS:]

    def forget_project(self, path: str, persist: bool = True):
        root = _clean_path(path)
        self._recent = [item for item in self._recent if item.get("path") != root]
        if self._active_root == root:
            self._active_root = ""
        if persist:
            self._persist()

    def resolve_startup_project(self, candidate: str = "") -> str:
        """Return a usable project root for startup, or "" when the user must
        pick one. An explicit candidate wins over the remembered project."""
        for path in (candidate, self._active_root):
            root = _clean_path(path)
            if root and os.path.isdir(root) and not self.is_app_dir(root):
                return root
        return ""

    # --------------------------------------------------------------- sessions
    def new_session_file(self, root: str = "") -> str:
        root = root or self._active_root
        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"session-{stamp}-{uuid.uuid4().hex[:6]}.json"
        path = os.path.join(self.sessions_dir(root), filename)
        self._write_json_atomic(path, {
            "id": uuid.uuid4().hex,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "messages": [],
        })
        return path

    def latest_session_file(self, root: str = "") -> str:
        sessions = self.sessions_dir(root)
        try:
            files = [
                os.path.join(sessions, name)
                for name in os.listdir(sessions)
                if name.endswith(".json") and name.startswith("session-")
            ]
        except OSError:
            return ""
        if not files:
            return ""
        return max(files, key=lambda p: os.path.getmtime(p))

    def list_sessions(self, root: str = "", limit: int = 0) -> list:
        """Session summaries for one project, newest first."""
        root = root or self._active_root
        if not root:
            return []
        sessions = self.sessions_dir(root)
        result = []
        try:
            names = os.listdir(sessions)
        except OSError:
            return []
        for name in names:
            if not name.endswith(".json") or not name.startswith("session-"):
                continue
            path = os.path.join(sessions, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            messages = []
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and isinstance(data.get("messages"), list):
                    messages = data["messages"]
            except Exception:
                pass
            result.append({
                "path": path,
                "project_root": root,
                "project_name": os.path.basename(root.rstrip("\\/")) or root,
                "title": _session_title(messages),
                "message_count": len(messages),
                "mtime": mtime,
            })
        result.sort(key=lambda item: item["mtime"], reverse=True)
        return result[:limit] if limit else result

    def recent_sessions(self, limit: int = 30) -> list:
        """Session summaries across recent projects, newest first."""
        result = []
        for project in self._recent:
            result.extend(self.list_sessions(project.get("path", ""), limit=limit))
        result.sort(key=lambda item: item["mtime"], reverse=True)
        return result[:limit] if limit else result

    def session_project_root(self, session_path: str) -> str:
        """Returns the project root that owns a session file, or ""."""
        path = _clean_path(session_path)
        if not path:
            return ""
        logos_dir = os.path.dirname(os.path.dirname(path))
        sessions_dir = os.path.dirname(path)
        if os.path.basename(logos_dir) != PROJECT_DIRNAME:
            return ""
        if os.path.basename(sessions_dir) != "sessions":
            return ""
        root = os.path.dirname(logos_dir)
        return root if os.path.isdir(root) else ""

    def load_session(self, path: str) -> list:
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            logging.warning(f"Could not load session {path}: {exc}")
            return []
        messages = data.get("messages") if isinstance(data, dict) else None
        return messages if isinstance(messages, list) else []

    def save_session(self, path: str, messages: list):
        if not path:
            return
        try:
            created_at = _now_iso()
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        existing = json.load(fh)
                    if isinstance(existing, dict) and existing.get("created_at"):
                        created_at = existing["created_at"]
                except Exception:
                    pass
            data = {
                "id": os.path.splitext(os.path.basename(path))[0],
                "created_at": created_at,
                "updated_at": _now_iso(),
                "messages": messages,
            }
            self._write_json_atomic(path, data)
        except Exception as exc:
            logging.warning(f"Could not save session {path}: {exc}")
