"""Rolling handoff summary for one project.

The journal (``sessions/*.jsonl``) is the source of truth. This module keeps a
small, fixed-shape Markdown summary next to it (``context/summary.md``) so a
new mode, model or application run can be seeded with what happened without
replaying every tool result into the prompt.

The summary is intentionally lossy. It is regenerated from the journal whenever
the context threshold is crossed; a deterministic fallback is written first so
mode switches never wait for an LLM round-trip.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from context.secrets import redact_text
from context.tokens import context_usage
from project_manager import event_text

logger = logging.getLogger(__name__)

SUMMARY_FILENAME = "summary.md"
META_FILENAME = "summary.json"
SUMMARY_SECTIONS = ("Objective", "Done", "Current", "Next", "Files", "Blockers")
DEFAULT_MAX_TOKENS = 1200
DEFAULT_THRESHOLD_RATIO = 0.6
MIN_EVENTS_FOR_SUMMARY = 4
MAX_PROMPT_CHARS = 40_000
MAX_EVENT_CHARS = 2_400
MAX_SUMMARY_CHARS = 16_000
# opencode-style compaction: keep the recent tail intact, prune older tool
# output to head/tail excerpts, and cap what one event may contribute.
RECENT_TAIL_CHARS = 20_000
RECENT_EVENT_MAX_CHARS = 12_000
PRUNED_TOOL_OUTPUT_CHARS = 2_000

_PATH_RE = re.compile(
    r"(?<![\w.-])((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\w.-]+[\\/])?"
    r"[\w.-]+\.[A-Za-z0-9]{1,8})(?![\w.-])"
)


def summary_path(context_dir: str) -> str:
    return os.path.join(context_dir, SUMMARY_FILENAME)


def meta_path(context_dir: str) -> str:
    return os.path.join(context_dir, META_FILENAME)


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def redact(text: str) -> str:
    """Remove obvious credentials before they reach the summary file."""
    return redact_text(text)


def read_summary(context_dir: str) -> str:
    if not context_dir:
        return ""
    try:
        with open(summary_path(context_dir), encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, UnicodeError):
        return ""


def read_meta(context_dir: str) -> dict:
    if not context_dir:
        return {}
    try:
        with open(meta_path(context_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_summary(
    context_dir: str,
    text: str,
    *,
    source_session: str = "",
    event_count: int = 0,
    provider: str = "",
    model: str = "",
    source: str = "llm",
) -> str:
    """Persist one normalized summary and its provenance metadata."""
    text = normalize_summary(text)
    if not text:
        return ""
    _atomic_write(summary_path(context_dir), text + "\n")
    meta = {
        "schema_version": 1,
        "source_session": source_session,
        "event_count": int(event_count or 0),
        "provider": provider,
        "model": model,
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write(meta_path(context_dir), json.dumps(meta, ensure_ascii=False, indent=2))
    return text


def _clip(text, limit: int = MAX_EVENT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head - 20
    return text[:head].rstrip() + " … " + text[-tail:].lstrip()


def _event_line(event: dict, limit: int = MAX_EVENT_CHARS) -> str:
    event_type = event.get("type")
    text = redact(event_text(event))
    if event_type == "message":
        role = str(event.get("role") or "message").upper()
        return f"{role}: {_clip(text, limit)}"
    if event_type == "reasoning":
        return f"REASONING: {_clip(text, limit)}"
    if event_type == "tool_call":
        name = str(event.get("name") or "action")
        args = event.get("input") or {}
        try:
            args_text = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            args_text = str(args)
        return f"ACTION {name}: {_clip(args_text, limit)}"
    if event_type == "tool_result":
        name = str(event.get("name") or "tool")
        status = str(event.get("status") or "ok")
        return f"RESULT {name} [{status}]: {_clip(text, limit)}"
    if event_type == "job_result":
        status = str(event.get("status") or "finished")
        return f"JOB #{event.get('job_id', '?')} [{status}]: {_clip(text, limit)}"
    if event_type == "error":
        return f"ERROR: {_clip(text or event.get('message', ''), limit)}"
    return ""


def event_line(event: dict) -> str:
    """Public journal line shared by summarization and context statistics."""
    return _event_line(event)


def _split_sections(text: str) -> dict:
    sections = {}
    current = None
    body = []
    for line in (text or "").splitlines():
        match = re.match(r"^\s*##\s+(Objective|Done|Current|Next|Files|Blockers)\s*$",
                         line, re.IGNORECASE)
        if match:
            if current:
                sections[current] = "\n".join(body).strip()
            current = match.group(1).title()
            body = []
        elif current is not None:
            body.append(line)
    if current:
        sections[current] = "\n".join(body).strip()
    return sections


def _normalize_files(lines) -> list[str]:
    files = []
    seen = set()
    for value in lines or []:
        for path in _PATH_RE.findall(str(value or "")):
            path = path.strip("`'\".,;:()[]{}")
            if not path or path.lower() in ("http", "https", "www"):
                continue
            key = path.lower().replace("\\", "/")
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return files[:20]


def _fallback_files(events) -> list[str]:
    values = []
    for event in events:
        event_type = event.get("type")
        if event_type == "tool_call":
            values.append(json.dumps(event.get("input") or {}, ensure_ascii=False))
        elif event_type in ("tool_result", "job_result", "error"):
            values.append(event_text(event))
        elif event_type == "message":
            values.append(event_text(event))
    return _normalize_files(values)


def fallback_summary(events: list, previous: str = "") -> str:
    """Deterministic extractive summary used before/without an LLM pass."""
    events = [event for event in (events or []) if isinstance(event, dict)]
    previous_sections = _split_sections(previous)
    if not events:
        return normalize_summary(previous)

    first_user = next(
        (event_text(event) for event in events
         if event.get("type") == "message" and event.get("role") == "user"
         and event_text(event)),
        "",
    )
    objective = _clip(first_user, 500) or "(not set)"

    done = []
    for event in events:
        if event.get("type") == "message" and event.get("role") == "assistant":
            text = _clip(event_text(event), 400)
            if text:
                done.append(f"- {text}")
        elif event.get("type") == "tool_call":
            name = str(event.get("name") or "action")
            done.append(f"- Executed `{name}`")
        elif event.get("type") == "job_result":
            status = event.get("status") or "finished"
            done.append(f"- Background job #{event.get('job_id', '?')}: {status}")
    previous_done = [
        line for line in previous_sections.get("Done", "").splitlines()
        if line.strip()
    ]
    done = previous_done + done
    if not done:
        done = ["- No completed steps recorded yet."]
    done = done[-12:]

    last_event = events[-1]
    current = _clip(event_text(last_event) or _event_line(last_event), 700) or "(not set)"

    next_steps = []
    last_user = next(
        (event_text(event) for event in reversed(events)
         if event.get("type") == "message" and event.get("role") == "user"
         and event_text(event)),
        "",
    )
    if last_user:
        next_steps.append(f"- Continue from the latest request: {_clip(last_user, 500)}")
    else:
        next_steps.append("- Continue from the current state.")
    previous_next = previous_sections.get("Next", "").splitlines()
    for line in previous_next:
        if line.strip() and line.strip().lower() not in (
                item.strip().lower() for item in next_steps):
            next_steps.append(line.strip())

    files = _normalize_files(previous_sections.get("Files", "").splitlines())
    for path in _fallback_files(events):
        if path.lower() not in {item.lower() for item in files}:
            files.append(path)

    blockers = [
        line for line in previous_sections.get("Blockers", "").splitlines()
        if line.strip() and line.strip() != "- None."
    ]
    for event in events:
        if event.get("type") != "error":
            continue
        text = _clip(event_text(event) or event.get("message", ""), 500)
        if text:
            blockers.append(f"- {text}")
    blockers = blockers[-6:] or ["- None."]

    parts = [
        "## Objective",
        objective,
        "",
        "## Done",
        *done,
        "",
        "## Current",
        current,
        "",
        "## Next",
        *next_steps,
        "",
        "## Files",
        *([f"- `{path}`" for path in files] or ["- None recorded."]),
        "",
        "## Blockers",
        *blockers,
    ]
    return normalize_summary("\n".join(parts))


def normalize_summary(text: str, fallback: str = "") -> str:
    """Force the fixed section order and fill missing sections from fallback."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            raw = "\n".join(lines[1:-1]).strip()
    sections = _split_sections(raw)
    fallback_sections = _split_sections(fallback or "")
    if not sections and fallback_sections:
        sections = fallback_sections
    if not sections:
        return ""
    for name in SUMMARY_SECTIONS:
        if not sections.get(name):
            sections[name] = fallback_sections.get(name) or (
                "- None." if name in ("Done", "Next", "Files", "Blockers") else "(not set)")
    parts = []
    for name in SUMMARY_SECTIONS:
        body = redact(sections[name]).strip()
        parts.append(f"## {name}\n{body}")
    result = "\n\n".join(parts)
    if len(result) > MAX_SUMMARY_CHARS:
        result = result[:MAX_SUMMARY_CHARS].rstrip() + "\n\n… [summary truncated]"
    return result


def session_summary(events: list, objective: str = "") -> str:
    """State block for resuming one session (deterministic, no LLM call)."""
    text = fallback_summary(events)
    objective = " ".join(str(objective or "").split())
    if not objective:
        return text
    sections = _split_sections(text)
    sections["Objective"] = objective
    parts = []
    for name in SUMMARY_SECTIONS:
        body = sections.get(name) or (
            "- None." if name in ("Done", "Next", "Files", "Blockers") else "(not set)")
        parts.append(f"## {name}\n{body}")
    return normalize_summary("\n\n".join(parts))


def _recent_boundary(events: list) -> int:
    """Index from which events are kept verbatim (bounded recent tail)."""
    total = 0
    boundary = len(events)
    for index in range(len(events) - 1, -1, -1):
        line = _event_line(events[index], RECENT_EVENT_MAX_CHARS)
        if not line:
            continue
        if total + len(line) > RECENT_TAIL_CHARS and boundary < len(events):
            break
        total += len(line) + 1
        boundary = index
    return boundary


def _select_event_lines(events: list) -> str:
    events = [event for event in (events or []) if isinstance(event, dict)]
    boundary = _recent_boundary(events)
    selected = []
    for index, event in enumerate(events):
        limit = RECENT_EVENT_MAX_CHARS if index >= boundary else MAX_EVENT_CHARS
        if index < boundary and event.get("type") in ("tool_result", "job_result"):
            limit = PRUNED_TOOL_OUTPUT_CHARS
        line = _event_line(event, limit)
        if line:
            selected.append(line)

    first_user = next(
        (_event_line(event, RECENT_EVENT_MAX_CHARS) for event in events
         if event.get("type") == "message" and event.get("role") == "user"),
        "",
    )
    if first_user and first_user not in selected:
        selected.insert(0, first_user)

    total = sum(len(line) + 1 for line in selected)
    while selected and total > MAX_PROMPT_CHARS and len(selected) > 1:
        if selected[0] == first_user:
            removed = selected.pop(1)
        else:
            removed = selected.pop(0)
        total -= len(removed) + 1
    return "\n".join(selected)


def build_summary_prompts(
    previous: str,
    events: list,
    *,
    project_name: str = "",
    mode: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[str, str]:
    """Prompts for a rolling update of the existing handoff summary."""
    system_prompt = (
        "You maintain a compact handoff summary for a local desktop agent. "
        "The event journal is the source of truth. Update the previous summary "
        "with the new events, preserving completed work, decisions and open "
        "threads. Return ONLY Markdown with exactly these sections, in order:\n\n"
        "## Objective\n"
        "## Done\n"
        "## Current\n"
        "## Next\n"
        "## Files\n"
        "## Blockers\n\n"
        "Rules:\n"
        "- Be factual; do not invent actions that are not in the events.\n"
        "- Keep the user's language (usually Russian).\n"
        "- Use short bullet lists for Done, Next, Files and Blockers.\n"
        "- Files must be paths that were actually read, written or mentioned.\n"
        "- Next must state the concrete next step, or say that the task appears complete.\n"
        "- Do not mention this summarization process or the event journal.\n"
        f"- Stay under {int(max_tokens)} tokens.\n"
        "- Never include passwords, API keys or access tokens."
    )
    user_prompt = (
        f"Project: {project_name or '(unnamed)'}\n"
        f"Session mode: {mode or 'unknown'}\n\n"
        "Previous summary:\n"
        f"{previous.strip() or '(none)'}\n\n"
        "New events:\n"
        f"{_select_event_lines(events) or '(none)'}"
    )
    return system_prompt, user_prompt


def _latest_completion_tokens(events: list) -> int:
    for event in reversed(events or []):
        if not isinstance(event, dict) or event.get("type") != "usage":
            continue
        if str(event.get("purpose") or "completion") != "completion":
            continue
        total = 0
        try:
            total = max(0, int(event.get("total") or 0))
        except (TypeError, ValueError):
            total = 0
        if not total:
            for key in ("input", "output", "reasoning", "cache_read", "cache_write"):
                try:
                    total += max(0, int(event.get(key) or 0))
                except (TypeError, ValueError):
                    continue
        if total:
            return total
    return 0


def needs_refresh(
    events: list,
    provider_id: str,
    model_id: str,
    meta: dict,
    session_file: str,
    *,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
    force: bool = False,
) -> bool:
    """Whether the rolling summary is missing or worth updating now."""
    count = len(events or [])
    if not count:
        return False
    meta = meta or {}
    same_session = bool(
        session_file and meta.get("source_session")
        and os.path.normcase(os.path.abspath(str(meta["source_session"])))
        == os.path.normcase(os.path.abspath(session_file))
    )
    processed = int(meta.get("event_count") or 0) if same_session else 0
    if count <= processed:
        return False
    if force:
        return True
    threshold = float(threshold_ratio or DEFAULT_THRESHOLD_RATIO)
    if provider_id and model_id:
        actual = _latest_completion_tokens(events)
        if actual:
            window = context_usage(provider_id, model_id, "")["window"]
            return bool(window and (actual / window) >= threshold)
    if count < MIN_EVENTS_FOR_SUMMARY:
        return False
    if not meta:
        return True
    if not provider_id or not model_id:
        return False
    lines = []
    for event in events:
        line = _event_line(event)
        if line:
            lines.append(line)
    usage = context_usage(provider_id, model_id, "\n".join(lines))
    return usage["ratio"] >= threshold


def should_inject(meta: dict, session_file: str) -> bool:
    """Inject only when the summary came from a different session/mode."""
    if not meta:
        return True
    source = str(meta.get("source_session") or "")
    if not source or not session_file:
        return True
    return os.path.normcase(os.path.abspath(source)) != os.path.normcase(
        os.path.abspath(session_file))


def injection_block(summary: str, mode: str = "") -> str:
    """Prompt prefix carrying prior-session context into the current mode."""
    summary = (summary or "").strip()
    if not summary:
        return ""
    label = "Handoff context from earlier work"
    if mode:
        label += f" (continuing in {mode} mode)"
    return (
        f"**{label}:**\n\n"
        f"{summary}\n\n"
        "Use this as background. The current user request is authoritative; "
        "do not repeat completed steps unless verification is needed."
    )


def session_injection_block(
    summary: str,
    mode: str = "",
) -> str:
    """Prompt prefix for resuming the live session itself."""
    summary = (summary or "").strip()
    if not summary:
        return ""
    label = "Current session context"
    if mode:
        label += f" ({mode} mode)"
    return (
        f"**{label}:**\n\n"
        f"{summary}\n\n"
        "Resume from this state. The live browser tabs or desktop are not "
        "replayed; observe the current screen/page and continue with the "
        "user's latest request."
    )
