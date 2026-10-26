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
import shutil
import sys
import time
import uuid
from datetime import UTC, datetime

from context.secrets import redact_value


PROJECT_DIRNAME = ".logos"
PROJECT_META_FILE = "project.json"
MAX_RECENT_PROJECTS = 50
SESSION_SCHEMA_VERSION = 1
SESSION_EXT = ".jsonl"
LEGACY_SESSION_EXT = ".json"
MAX_EVENT_CHARS = 1_000_000


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


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return ""


def _message_text(message: dict) -> str:
    if not isinstance(message, dict):
        return ""
    return _content_text(message.get("content"))


def event_text(event: dict) -> str:
    """Plain text carried by a message/tool/job/error session event."""
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "message":
        return _message_text(event)
    output = event.get("output")
    if isinstance(output, dict):
        text = _content_text(output.get("content"))
        if text:
            return text
        if isinstance(output.get("text"), str):
            return output["text"]
    if isinstance(output, str):
        return output
    text = event.get("text")
    if isinstance(text, str):
        return text
    message = event.get("message")
    return message if isinstance(message, str) else ""


def _session_title(messages: list) -> str:
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        line = " ".join(_message_text(item).split())
        if line:
            return line[:80] + ("..." if len(line) > 80 else "")
    return "Empty session"


def _cap_event(event: dict) -> dict:
    """Keep one event below a sane size without dropping its shape."""
    changed = False

    def cap(value):
        nonlocal changed
        if isinstance(value, str) and len(value) > MAX_EVENT_CHARS:
            changed = True
            return value[:MAX_EVENT_CHARS] + "\n… [truncated]"
        if isinstance(value, list):
            return [cap(item) for item in value]
        if isinstance(value, dict):
            return {key: cap(item) for key, item in value.items()}
        return value

    capped = cap(event)
    if changed:
        capped["truncated"] = True
    return capped


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
        return self.project_display_name(self._active_root)

    def recent_projects(self) -> list:
        return list(self._recent)

    # --------------------------------------------------------------- projects
    @staticmethod
    def project_dir(root: str) -> str:
        return os.path.join(root, PROJECT_DIRNAME)

    @staticmethod
    def _read_project_meta(root: str) -> dict:
        meta_path = os.path.join(root, PROJECT_DIRNAME, PROJECT_META_FILE)
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh) or {}
        except Exception:
            return {}
        return meta if isinstance(meta, dict) else {}

    def project_display_name(self, root: str = "") -> str:
        """Custom project name from ``project.json`` (folder name as fallback)."""
        root = _clean_path(root) or self._active_root
        if not root:
            return ""
        name = str(self._read_project_meta(root).get("name") or "").strip()
        return name or os.path.basename(root.rstrip("\\/")) or root

    def sessions_dir(self, root: str = "") -> str:
        root = root or self._active_root
        return os.path.join(self.project_dir(root), "sessions")

    def context_dir(self, root: str = "") -> str:
        root = root or self._active_root
        return os.path.join(self.project_dir(root), "context")

    def attachments_dir(self, root: str = "") -> str:
        root = root or self._active_root
        return os.path.join(self.project_dir(root), "attachments")

    def session_attachments_dir(self, session_path: str) -> str:
        session_id = self.session_id(session_path)
        root = self.session_project_root(session_path)
        if not session_id or not root:
            return ""
        return os.path.join(self.attachments_dir(root), session_id)

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
        meta.setdefault("name", os.path.basename(root.rstrip("\\/")) or root)
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

    def rename_project(self, path: str, new_name: str) -> str:
        """Stores a custom display name without touching the project folder."""
        root = _clean_path(path)
        name = " ".join(str(new_name or "").split())
        if not root or not os.path.isdir(root):
            raise ValueError("Project folder not found.")
        if not name:
            raise ValueError("The project name cannot be empty.")
        logos_dir = self.project_dir(root)
        os.makedirs(logos_dir, exist_ok=True)
        meta = self._read_project_meta(root)
        meta.setdefault("id", uuid.uuid4().hex)
        meta["name"] = name
        meta.setdefault("created_at", _now_iso())
        meta["path"] = root
        meta["last_opened"] = meta.get("last_opened") or _now_iso()
        self._write_json_atomic(os.path.join(logos_dir, PROJECT_META_FILE), meta)
        for item in self._recent:
            if item.get("path") == root:
                item["name"] = name
        self._persist()
        return name

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

    def remove_project(self, path: str, delete_data: bool = False,
                       persist: bool = True):
        """Drops a project from the list; optionally deletes its ``.logos`` data.

        The project folder itself (the user's own files) is never touched.
        """
        root = _clean_path(path)
        if not root:
            return
        if delete_data:
            logos_dir = self.project_dir(root)
            if os.path.isdir(logos_dir):
                shutil.rmtree(logos_dir)
                logging.info(f"Deleted project data: {logos_dir}")
        self.forget_project(root, persist=persist)

    def resolve_startup_project(self, candidate: str = "") -> str:
        """Return a usable project root for startup, or "" when the user must
        pick one. An explicit candidate wins over the remembered project."""
        for path in (candidate, self._active_root):
            root = _clean_path(path)
            if root and os.path.isdir(root) and not self.is_app_dir(root):
                return root
        return ""

    # --------------------------------------------------------------- sessions
    @staticmethod
    def _write_jsonl_atomic(path: str, records: list) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
        os.replace(tmp, path)

    @staticmethod
    def make_event(event_type: str, **fields) -> dict:
        event = {
            "type": event_type,
            "id": fields.pop("id", uuid.uuid4().hex),
            "ts": fields.pop("ts", _now_iso()),
        }
        event.update(fields)
        return event

    def _session_header(self, session_id: str, mode: str, root: str) -> dict:
        return {
            "type": "session",
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": session_id,
            "mode": mode or "chat",
            "created_at": _now_iso(),
            "project_root": root,
            "cwd": root,
        }

    def new_session_file(self, root: str = "", mode: str = "chat") -> str:
        root = root or self._active_root
        stamp = time.strftime("%Y%m%d-%H%M%S")
        session_id = uuid.uuid4().hex
        filename = f"session-{stamp}-{session_id[:6]}{SESSION_EXT}"
        path = os.path.join(self.sessions_dir(root), filename)
        self._write_jsonl_atomic(path, [self._session_header(session_id, mode, root)])
        return path

    def append_event(self, path: str, event: dict):
        if not path or not event:
            return None
        if not path.endswith(SESSION_EXT):
            raise ValueError(f"Not a JSONL session: {path}")
        record = redact_value(dict(event))
        record.setdefault("id", uuid.uuid4().hex)
        record.setdefault("ts", _now_iso())
        record = _cap_event(record)
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
                fh.flush()
            return record
        except Exception as exc:
            logging.warning(f"Could not append session event to {path}: {exc}")
            return None

    def set_session_mode(self, path: str, mode: str) -> bool:
        """Change the mode of an empty JSONL session (rewrites only the header)."""
        if not path or not os.path.exists(path) or not path.endswith(SESSION_EXT):
            return False
        try:
            with open(path, encoding="utf-8") as fh:
                records = [json.loads(line) for line in fh if line.strip()]
        except Exception as exc:
            logging.warning(f"Could not read session header {path}: {exc}")
            return False
        if len(records) != 1 or not isinstance(records[0], dict):
            return False
        header = records[0]
        if header.get("type") != "session":
            return False
        header["mode"] = mode or "chat"
        try:
            self._write_jsonl_atomic(path, [header])
        except Exception as exc:
            logging.warning(f"Could not update session mode {path}: {exc}")
            return False
        return True

    def _update_session_header(self, path: str, field: str, value: str) -> bool:
        """Rewrites one header field atomically; legacy JSON uses the top level."""
        text = " ".join(str(value or "").split())
        if not text or not path or not os.path.isfile(path):
            return False
        updated_key = f"{field}_updated_at"
        if path.endswith(LEGACY_SESSION_EXT):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:
                logging.warning(f"Could not read session {path}: {exc}")
                return False
            if not isinstance(data, dict):
                return False
            data[field] = text
            data[updated_key] = _now_iso()
            try:
                self._write_json_atomic(path, data)
            except Exception as exc:
                logging.warning(f"Could not update session {field} {path}: {exc}")
                return False
            return True
        if not path.endswith(SESSION_EXT):
            return False
        try:
            with open(path, encoding="utf-8") as fh:
                records = [json.loads(line) for line in fh if line.strip()]
        except Exception as exc:
            logging.warning(f"Could not read session {path}: {exc}")
            return False
        if not records or not isinstance(records[0], dict):
            return False
        header = records[0]
        if header.get("type") != "session":
            return False
        header[field] = text
        header[updated_key] = _now_iso()
        try:
            self._write_jsonl_atomic(path, records)
        except Exception as exc:
            logging.warning(f"Could not update session {field} {path}: {exc}")
            return False
        return True

    def set_session_title(self, path: str, title: str) -> bool:
        """Stores a custom session title in the session header."""
        return self._update_session_header(path, "title", title)

    def set_session_objective(self, path: str, objective: str) -> bool:
        """Stores the editable one-line goal in the session header."""
        return self._update_session_header(path, "objective", objective)

    def session_id(self, path: str) -> str:
        """Reads the session id from the header without loading the journal."""
        if not path or not os.path.isfile(path):
            return ""
        if path.endswith(LEGACY_SESSION_EXT):
            return str(self.load_session_data(path).get("id") or "")
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if isinstance(record, dict) and record.get("type") == "session":
                        return str(record.get("id") or "")
                    break
        except Exception as exc:
            logging.warning(f"Could not read session id from {path}: {exc}")
        return ""

    def delete_session(self, path: str) -> bool:
        """Deletes one session file and its copied attachments."""
        if not path or not os.path.isfile(path):
            return False
        if not path.endswith((SESSION_EXT, LEGACY_SESSION_EXT)):
            return False
        attachment_dir = self.session_attachments_dir(path)
        try:
            os.remove(path)
        except OSError as exc:
            logging.warning(f"Could not delete session {path}: {exc}")
            return False
        if attachment_dir and os.path.isdir(attachment_dir):
            shutil.rmtree(attachment_dir, ignore_errors=True)
        logging.info(f"Deleted session: {path}")
        return True

    def latest_session_file(self, root: str = "") -> str:
        sessions = self.sessions_dir(root)
        try:
            files = [
                os.path.join(sessions, name)
                for name in os.listdir(sessions)
                if name.startswith("session-")
                and name.endswith((SESSION_EXT, LEGACY_SESSION_EXT))
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
            if (not name.startswith("session-")
                    or not name.endswith((SESSION_EXT, LEGACY_SESSION_EXT))):
                continue
            path = os.path.join(sessions, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            data = self.load_session_data(path)
            messages = data.get("messages") or []
            result.append({
                "path": path,
                "project_root": root,
                "project_name": self.project_display_name(root),
                "title": data.get("title") or _session_title(messages),
                "objective": data.get("objective") or "",
                "message_count": len(messages),
                "mode": data.get("mode") or "chat",
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

    @staticmethod
    def _message_from_event(event: dict):
        role = event.get("role")
        if role not in ("user", "assistant", "system"):
            return None
        return {"role": role, "content": _content_text(event.get("content"))}

    def _load_jsonl_session(self, path: str) -> dict:
        header = {}
        events = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception as exc:
                        logging.warning(f"Skipping malformed session line in {path}: {exc}")
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("type") == "session" and not header:
                        header = record
                        continue
                    events.append(record)
        except Exception as exc:
            logging.warning(f"Could not load session {path}: {exc}")
        messages = []
        for event in events:
            if event.get("type") == "message":
                message = self._message_from_event(event)
                if message is not None:
                    messages.append(message)
        return {
            "path": path,
            "id": header.get("id") or os.path.splitext(os.path.basename(path))[0],
            "mode": header.get("mode") or "chat",
            "created_at": header.get("created_at") or "",
            "title": header.get("title") or "",
            "objective": header.get("objective") or "",
            "messages": messages,
            "events": events,
        }

    def _load_legacy_session(self, path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            logging.warning(f"Could not load legacy session {path}: {exc}")
            return {"path": path, "id": "", "mode": "chat", "created_at": "",
                    "title": "", "objective": "", "messages": [], "events": []}
        if not isinstance(data, dict):
            data = {}
        raw_messages = data.get("messages")
        messages = raw_messages if isinstance(raw_messages, list) else []
        events = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            text = _message_text(message)
            if not text:
                continue
            events.append(self.make_event(
                "message", turn_id="legacy", role=role,
                content=[{"type": "text", "text": text}]))
        return {
            "path": path,
            "id": data.get("id") or os.path.splitext(os.path.basename(path))[0],
            "mode": data.get("mode") or "chat",
            "created_at": data.get("created_at") or "",
            "title": data.get("title") or "",
            "objective": data.get("objective") or "",
            "messages": [m for m in (self._message_from_event(e) for e in events)
                         if m is not None],
            "events": events,
        }

    def load_session_data(self, path: str) -> dict:
        if not path or not os.path.exists(path):
            return {"path": path, "id": "", "mode": "chat", "created_at": "",
                    "title": "", "objective": "", "messages": [], "events": []}
        if path.endswith(SESSION_EXT):
            return self._load_jsonl_session(path)
        return self._load_legacy_session(path)

    def load_session(self, path: str) -> list:
        return self.load_session_data(path).get("messages") or []

    def migrate_session(self, path: str, mode: str = "") -> str:
        """Convert a legacy .json session into the JSONL format."""
        if not path or not os.path.exists(path) or path.endswith(SESSION_EXT):
            return path
        data = self.load_session_data(path)
        root = self.session_project_root(path)
        new_path = os.path.splitext(path)[0] + SESSION_EXT
        if os.path.exists(new_path):
            new_path = os.path.splitext(path)[0] + f"-{uuid.uuid4().hex[:6]}{SESSION_EXT}"
        header = self._session_header(
            data.get("id") or uuid.uuid4().hex,
            mode or data.get("mode") or "chat",
            root,
        )
        if data.get("created_at"):
            header["created_at"] = data["created_at"]
        if data.get("title"):
            header["title"] = data["title"]
        if data.get("objective"):
            header["objective"] = data["objective"]
        records = [header]
        for message in data.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            records.append(self.make_event(
                "message", turn_id="legacy", role=role,
                content=[{"type": "text", "text": _message_text(message)}]))
        try:
            self._write_jsonl_atomic(new_path, records)
            os.replace(path, f"{path}.bak")
        except Exception as exc:
            logging.warning(f"Could not migrate session {path}: {exc}")
            return path
        return new_path

    def save_session(self, path: str, messages: list):
        """Legacy full-save helper; new code appends events instead."""
        if not path:
            return
        try:
            if path.endswith(SESSION_EXT):
                existing = self.load_session_data(path)
                root = self.session_project_root(path)
                header = self._session_header(
                    existing.get("id") or uuid.uuid4().hex,
                    existing.get("mode") or "chat", root)
                if existing.get("created_at"):
                    header["created_at"] = existing["created_at"]
                if existing.get("title"):
                    header["title"] = existing["title"]
                if existing.get("objective"):
                    header["objective"] = existing["objective"]
                records = [header]
                for message in messages or []:
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role")
                    if role not in ("user", "assistant", "system"):
                        continue
                    records.append(redact_value(self.make_event(
                        "message", turn_id="legacy", role=role,
                        content=[{"type": "text", "text": _message_text(message)}])))
                self._write_jsonl_atomic(path, records)
                return
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
                "messages": redact_value(messages),
            }
            self._write_json_atomic(path, data)
        except Exception as exc:
            logging.warning(f"Could not save session {path}: {exc}")
