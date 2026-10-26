"""Durable session attachments.

Files that already live on disk are referenced, not copied. Bytes with no
stable source path (pasted images) are copied into the session's attachment
folder and deduplicated by SHA-256.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
MAX_COPY_BYTES = 25 * 1024 * 1024
MAX_SESSION_COPY_BYTES = 250 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_size(size) -> str:
    try:
        size = max(0, int(size or 0))
    except (TypeError, ValueError):
        size = 0
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


def _relative_path(path: str, root: str) -> str:
    if not path or not root:
        return path
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return path
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return path
    return rel.replace(os.sep, "/")


def resolve_path(record: dict, root: str) -> str:
    path = str(record.get("path") or "")
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(root, path.replace("/", os.sep)) if root else path


def _dir_size(path: str) -> int:
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _base_record(name: str, kind: str) -> dict:
    return {
        "name": name or "attachment",
        "kind": kind,
        "size": 0,
        "sha256": "",
        "path": "",
        "stored": False,
        "stored_name": "",
        "status": "ok",
        "error": "",
    }


def _record_reference(path: str, root: str, kind: str) -> dict:
    name = os.path.basename(path.rstrip("\\/")) or path
    record = _base_record(name, kind)
    if not path or not os.path.exists(path):
        record["status"] = "missing"
        record["error"] = "not found"
        return record
    record["path"] = _relative_path(path, root)
    if kind == "folder":
        return record
    try:
        record["size"] = os.path.getsize(path)
        record["sha256"] = sha256_file(path)
    except OSError as exc:
        record["error"] = str(exc)
        logging.warning(f"Could not fingerprint attachment {path}: {exc}")
    return record


def _image_extension(data: bytes) -> str:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "PNG").lower()
        return {"jpeg": ".jpg"}.get(fmt, f".{fmt}")
    except Exception:
        return ".png"


def _record_image(project_manager, session_path: str, attachment: dict,
                  root: str) -> dict:
    data = attachment.get("data") or b""
    name = str(attachment.get("name") or "Pasted image")
    record = _base_record(name, "image")
    if not data:
        record["status"] = "missing"
        record["error"] = "empty image"
        return record
    record["size"] = len(data)
    record["sha256"] = sha256_bytes(data)
    if len(data) > MAX_COPY_BYTES:
        record["status"] = "missing"
        record["error"] = f"not saved: larger than {format_size(MAX_COPY_BYTES)}"
        return record
    session_dir = project_manager.session_attachments_dir(session_path)
    if not session_dir:
        record["status"] = "missing"
        record["error"] = "not saved: no session folder"
        return record
    try:
        os.makedirs(session_dir, exist_ok=True)
        if _dir_size(session_dir) + len(data) > MAX_SESSION_COPY_BYTES:
            record["status"] = "missing"
            record["error"] = (
                f"not saved: session exceeds {format_size(MAX_SESSION_COPY_BYTES)}")
            return record
        filename = f"{record['sha256'][:16]}{_image_extension(data)}"
        target = os.path.join(session_dir, filename)
        if not os.path.exists(target):
            tmp = f"{target}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        record["stored"] = True
        record["stored_name"] = filename
        record["path"] = _relative_path(target, root)
    except OSError as exc:
        record["status"] = "missing"
        record["error"] = f"not saved: {exc}"
        logging.warning(f"Could not store image attachment {name}: {exc}")
    return record


def record_attachment(project_manager, session_path: str, attachment: dict,
                      root: str) -> dict:
    """Turns one pending attachment into journal metadata."""
    if not isinstance(attachment, dict):
        attachment = {"type": "image", "data": attachment}
    kind = str(attachment.get("type") or "file")
    if kind == "image":
        return _record_image(project_manager, session_path, attachment, root)
    path = str(attachment.get("path") or "")
    path = os.path.abspath(path) if path else ""
    if kind == "folder":
        return _record_reference(path, root, "folder")
    return _record_reference(path, root, "file")


def verify_attachment(project_manager, session_path: str, record: dict,
                      root: str) -> str:
    """Returns ``ok``, ``changed`` or ``missing`` for a recorded attachment."""
    if not isinstance(record, dict):
        return "missing"
    kind = str(record.get("kind") or "file")
    path = resolve_path(record, root)
    if not path:
        session_dir = project_manager.session_attachments_dir(session_path)
        stored_name = str(record.get("stored_name") or "")
        if session_dir and stored_name:
            path = os.path.join(session_dir, stored_name)
    if not path or not os.path.exists(path):
        return "missing"
    if kind == "folder":
        return "ok" if os.path.isdir(path) else "missing"
    if not os.path.isfile(path):
        return "missing"
    try:
        size = os.path.getsize(path)
    except OSError:
        return "missing"
    try:
        recorded_size = int(record.get("size") or 0)
    except (TypeError, ValueError):
        recorded_size = 0
    if recorded_size and size != recorded_size:
        return "changed"
    expected = str(record.get("sha256") or "")
    if expected:
        try:
            if sha256_file(path) != expected:
                return "changed"
        except OSError:
            return "missing"
    return "ok"
