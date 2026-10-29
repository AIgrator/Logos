"""Agent Skills discovered on disk (the open ``SKILL.md`` format).

A skill is a folder with a ``SKILL.md`` file whose YAML frontmatter carries
``name`` and ``description``. Only that metadata reaches the system prompt;
the body loads on demand when the model calls the ``use_skill`` tool, and
auxiliary files are read with the normal filesystem tools.

Roots are scanned in priority order: the active project first, then the
user's home directory. Inside a scope the native Logos folder wins over the
shared ``.agents`` folder and the per-product folders installed by other
agents (Cursor, Claude Code, Codex, Qoder), so an existing skill library
works without copying anything.
"""
from __future__ import annotations

import logging
import os
import re

from .secrets import redact_text

FILENAME = "SKILL.md"
MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_FILES = 40
MAX_DESCRIPTION = 1024
NAME_LIMIT = 64

_PROMPT_BUDGET = 6_000
_PROMPT_DESCRIPTION = 300

_PROJECT_ROOTS = (
    ((".logos", "skills"), "Logos"),
    ((".agents", "skills"), "Agent Skills"),
    ((".cursor", "skills"), "Cursor"),
    ((".claude", "skills"), "Claude Code"),
    ((".codex", "skills"), "Codex"),
    ((".qoder", "skills"), "Qoder"),
)

# path -> (mtime_ns, size, frontmatter dict or None)
_cache: dict[str, tuple[int, int, dict | None]] = {}


def roots(project_root: str, user_home: str | None = None) -> list:
    """Skill directories in priority order as ``(path, scope, source)``."""
    home = user_home if user_home is not None else os.path.expanduser("~")
    found = []
    if project_root:
        for parts, source in _PROJECT_ROOTS:
            found.append(
                (os.path.join(project_root, *parts), "project", source))
    if home:
        for parts, source in _PROJECT_ROOTS:
            found.append((os.path.join(home, *parts), "user", source))
    return found


def discover(project_root: str, user_home: str | None = None) -> list:
    """Effective skills sorted by name; the first root wins on a name clash."""
    found: dict[str, dict] = {}
    for root, scope, source in roots(project_root, user_home):
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            path = os.path.join(entry.path, FILENAME)
            meta = _read_meta(path)
            if meta is None:
                continue
            name = _normalise_name(meta.get("name") or entry.name)
            description = str(meta.get("description") or "").strip()
            if not name or not description or name in found:
                continue
            found[name] = {
                "name": name,
                "description": description[:MAX_DESCRIPTION],
                "dir": os.path.abspath(entry.path),
                "path": os.path.abspath(path),
                "scope": scope,
                "source": source,
            }
    return [found[name] for name in sorted(found)]


def prompt_block(project_root: str, user_home: str | None = None) -> str:
    """Name + description list for the agent system prompt."""
    items = discover(project_root, user_home)
    if not items:
        return "No skills are installed."
    lines, used, omitted = [], 0, 0
    for skill in items:
        description = " ".join(skill["description"].split())
        if len(description) > _PROMPT_DESCRIPTION:
            description = description[:_PROMPT_DESCRIPTION - 3] + "..."
        line = f"- {skill['name']}: {description}"
        if used + len(line) + 1 > _PROMPT_BUDGET:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if omitted:
        lines.append(f"- ({omitted} more skill(s) not listed.)")
    return "\n".join(lines)


def load(project_root: str, name: str, user_home: str | None = None) -> str:
    """Full instructions for one skill, plus its auxiliary file paths."""
    items = discover(project_root, user_home)
    key = _normalise_name(name)
    match = next((skill for skill in items if skill["name"] == key), None)
    if match is None:
        available = ", ".join(skill["name"] for skill in items) or "none"
        raise ValueError(
            f"Unknown skill '{name}'. Available skills: {available}.")
    try:
        with open(match["path"], encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read(MAX_BODY_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"Could not read skill '{match['name']}': {exc}") from exc
    truncated = len(text) > MAX_BODY_BYTES
    _, body = _split_frontmatter(text[:MAX_BODY_BYTES])
    body = redact_text(body).strip()
    if truncated:
        body += "\n\n[Skill file truncated at 64 KB.]"
    parts = [
        f"**Skill loaded: {match['name']}**",
        match["description"],
        "",
        f"Skill directory: {match['dir']}",
        "",
        body or "(The skill has no instructions.)",
    ]
    files = _list_files(match["dir"])
    if files:
        parts.append("")
        parts.append("Auxiliary files (relative to the skill directory; read "
                     "them with read_files when the instructions need them):")
        parts.extend(f"- {rel}" for rel in files)
    logging.info(
        f"Skill loaded: {match['name']} ({match['scope']} {match['source']}, "
        f"{len(files)} auxiliary file(s)).")
    return "\n".join(parts)


def _normalise_name(value) -> str:
    raw = str(value or "").strip().lower()
    candidate = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    candidate = re.sub(r"-{2,}", "-", candidate)
    return candidate[:NAME_LIMIT].strip("-") if candidate else ""


def _read_meta(path: str) -> dict | None:
    try:
        stat = os.stat(path)
    except OSError:
        _cache.pop(path, None)
        return None
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(path)
    if cached and cached[0] == signature[0] and cached[1] == signature[1]:
        return cached[2]
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            head = fh.read(MAX_FRONTMATTER_BYTES)
    except OSError:
        _cache[path] = (signature[0], signature[1], None)
        return None
    meta, body = _split_frontmatter(head)
    if meta is None:
        meta, body = {}, head
    if not str(meta.get("description") or "").strip():
        meta["description"] = _first_paragraph(body)
    _cache[path] = (signature[0], signature[1], meta)
    return meta


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    end = None
    for index in range(1, min(len(lines), 400)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return None, text
    return _parse_yaml(lines[1:end]), "\n".join(lines[end + 1:])


def _parse_yaml(lines: list) -> dict:
    """Small YAML subset: scalars, quotes, comments, ``|``/``>`` blocks."""
    data: dict = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line[:1].isspace():
            index += 1
            continue
        key, separator, rest = line.partition(":")
        if not separator:
            index += 1
            continue
        key = key.strip().lower()
        rest = rest.strip()
        if rest in ("|", ">", "|-", ">-", "|+", ">+"):
            block, index = _collect_block(lines, index + 1)
            data[key] = _block_text(block, rest)
            continue
        data[key] = _unquote(rest)
        index += 1
    return data


def _collect_block(lines: list, start: int) -> tuple[list, int]:
    block, index = [], start
    while index < len(lines):
        line = lines[index]
        if line.strip() and not line[:1].isspace():
            break
        block.append(line)
        index += 1
    return block, index


def _block_text(lines: list, style: str) -> str:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    indent = min(len(line) - len(line.lstrip())
                 for line in lines if line.strip())
    cleaned = [line[indent:] if line.strip() else "" for line in lines]
    if style.startswith(">"):
        paragraphs, current = [], []
        for line in cleaned:
            if line.strip():
                current.append(line.strip())
            elif current:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))
        return "\n\n".join(paragraphs)
    return "\n".join(cleaned).strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            inner = (inner.replace('\\"', '"')
                     .replace("\\\\", "\\")
                     .replace("\\n", "\n"))
        else:
            inner = inner.replace("''", "'")
        return inner
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _first_paragraph(body: str) -> str:
    for block in re.split(r"\n\s*\n", body or ""):
        lines = [line.strip() for line in block.splitlines()]
        lines = [line for line in lines
                 if line and not line.startswith(("#", "<!--", "---"))]
        if lines:
            return " ".join(lines)[:MAX_DESCRIPTION]
    return ""


def _list_files(skill_dir: str) -> list:
    found = []
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        dirnames[:] = sorted(
            name for name in dirnames
            if not name.startswith(".") and name != "__pycache__")
        rel_dir = os.path.relpath(dirpath, skill_dir)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= 2:
            dirnames[:] = []
        for filename in sorted(filenames):
            if filename == FILENAME or filename.startswith("."):
                continue
            found.append(os.path.relpath(
                os.path.join(dirpath, filename), skill_dir
            ).replace(os.sep, "/"))
            if len(found) >= MAX_FILES:
                return found
    return found
