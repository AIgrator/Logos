"""Rolling handoff summary for one session.

The journal (``sessions/*.jsonl``) is the source of truth. Each session keeps a
small, fixed-shape Markdown summary next to its journal
(``sessions/<name>.summary.md``) so resuming or switching modes does not replay
every tool result into the prompt. Summaries never cross session boundaries;
a mode switch explicitly seeds the new session once from the previous one.

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
from project_manager import (
    SESSION_META_EXT,
    SESSION_SUMMARY_EXT,
    event_text,
)

logger = logging.getLogger(__name__)

SUMMARY_SUFFIX = SESSION_SUMMARY_EXT
META_SUFFIX = SESSION_META_EXT
SUMMARY_SCHEMA_VERSION = 2
SUMMARY_SECTIONS = (
    "Objective",
    "Done",
    "Decisions",
    "Constraints",
    "Checks",
    "Current",
    "Next",
    "Unfinished",
    "Files",
    "Blockers",
)
_LIST_SECTIONS = frozenset({
    "Done", "Decisions", "Constraints", "Checks",
    "Next", "Unfinished", "Files", "Blockers",
})
DEFAULT_MAX_TOKENS = 1200
DEFAULT_THRESHOLD_RATIO = 0.6
# Uncovered-journal budget: refreshing on a window fraction alone never fires
# on 1M+ windows, so the summary is also refreshed once this many tokens of
# new events are not covered yet.
DEFAULT_REFRESH_TOKENS = 30_000
MIN_EVENTS_FOR_SUMMARY = 4
MAX_PROMPT_CHARS = 40_000
MAX_EVENT_CHARS = 2_400
MAX_SUMMARY_CHARS = 16_000
FOCUS_MAX_CHARS = 500
# opencode-style compaction: keep the recent tail intact, prune older tool
# output to head/tail excerpts, and cap what one event may contribute.
RECENT_TAIL_CHARS = 20_000
RECENT_EVENT_MAX_CHARS = 12_000
PRUNED_TOOL_OUTPUT_CHARS = 2_000

_PATH_RE = re.compile(
    r"(?<![\w.-])((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\w.-]+[\\/])?"
    r"[\w.-]+\.[A-Za-z0-9]{1,8})(?![\w.-])"
)
_SECTION_RE = re.compile(
    r"^\s*##\s+(" + "|".join(SUMMARY_SECTIONS) + r")\s*$",
    re.IGNORECASE,
)
_CHECK_COMMAND_RE = re.compile(
    r"(?i)\b(?:"
    r"pytest|ruff|flake8|pylint|mypy|compileall|"
    r"npm\s+(?:run\s+)?(?:test|build|lint|typecheck)|"
    r"yarn\s+(?:test|build|lint|typecheck)|"
    r"pnpm\s+(?:test|build|lint|typecheck)|"
    r"cargo\s+(?:test|build|check|clippy)|"
    r"go\s+(?:test|build|vet)|"
    r"mvn\s+(?:test|verify)|gradle(?:w)?\s+(?:test|check)|"
    r"dotnet\s+(?:test|build)|make\s+(?:test|check)|tox|tsc"
    r")\b"
)


def _default_body(name: str) -> str:
    return "- None." if name in _LIST_SECTIONS else "(not set)"


def summary_path(session_file: str) -> str:
    return os.path.splitext(session_file)[0] + SUMMARY_SUFFIX


def meta_path(session_file: str) -> str:
    return os.path.splitext(session_file)[0] + META_SUFFIX


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def redact(text: str) -> str:
    """Remove obvious credentials before they reach the summary file."""
    return redact_text(text)


def read_summary(session_file: str) -> str:
    if not session_file:
        return ""
    try:
        with open(summary_path(session_file), encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, UnicodeError):
        return ""


def read_meta(session_file: str) -> dict:
    if not session_file:
        return {}
    try:
        with open(meta_path(session_file), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_summary(
    session_file: str,
    text: str,
    *,
    source_session: str = "",
    event_count: int = 0,
    provider: str = "",
    model: str = "",
    source: str = "llm",
    handoff_from: str = "",
    compacted_through: str = "",
    compacted_event_count: int = 0,
) -> str:
    """Persist one normalized summary and its provenance metadata.

    Summary rewrites for the same session keep the existing compaction
    boundary, and an explicit ``compacted_through`` may advance it. A handoff
    seed (another source session) never inherits the target's boundary.
    """
    text = normalize_summary(text)
    if not text or not session_file:
        return ""
    existing = read_meta(session_file)
    same_target = bool(
        source_session and session_file
        and os.path.normcase(os.path.abspath(source_session))
        == os.path.normcase(os.path.abspath(session_file)))
    _atomic_write(summary_path(session_file), text + "\n")
    meta = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "source_session": source_session,
        "event_count": int(event_count or 0),
        "provider": provider,
        "model": model,
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if handoff_from:
        meta["handoff_from"] = handoff_from
    # The handoff flags describe this session, not the summary's origin.
    for key in ("handoff_from", "handoff_consumed"):
        if key in existing and key not in meta:
            meta[key] = existing[key]
    if same_target:
        for key in ("compacted_through", "compacted_event_count", "compacted_at"):
            if key in existing:
                meta.setdefault(key, existing[key])
        if compacted_through and int(compacted_event_count or 0) >= int(
                existing.get("compacted_event_count") or 0):
            meta["compacted_through"] = str(compacted_through)
            meta["compacted_event_count"] = int(compacted_event_count or 0)
            meta["compacted_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write(meta_path(session_file), json.dumps(meta, ensure_ascii=False, indent=2))
    return text


def summary_coverage(meta: dict, events: list) -> int:
    """How many current events the rolling summary claims to cover."""
    if not meta:
        return 0
    try:
        count = int(meta.get("event_count") or 0)
    except (TypeError, ValueError):
        count = 0
    return max(0, min(count, len(events or [])))


def compacted_event_count(meta: dict, events: list) -> int:
    """Events before the compaction boundary; 0 when nothing is compacted."""
    events = events or []
    if not meta or not events:
        return 0
    boundary = str(meta.get("compacted_through") or "")
    if boundary:
        for index, event in enumerate(events):
            if str(event.get("id") or "") == boundary:
                return index + 1
    try:
        count = int(meta.get("compacted_event_count") or 0)
    except (TypeError, ValueError):
        count = 0
    return max(0, min(count, len(events)))


def set_compaction_boundary(
    session_file: str,
    event_id: str,
    event_count: int,
) -> dict:
    """Advance the persisted boundary without rewriting the summary text."""
    if not session_file or not event_id:
        return {}
    meta = read_meta(session_file)
    if not meta:
        return {}
    count = max(0, int(event_count or 0))
    try:
        previous = int(meta.get("compacted_event_count") or 0)
    except (TypeError, ValueError):
        previous = 0
    if count < previous:
        return meta
    meta["compacted_through"] = str(event_id)
    meta["compacted_event_count"] = count
    meta["compacted_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        _atomic_write(meta_path(session_file), json.dumps(meta, ensure_ascii=False, indent=2))
    except OSError as exc:
        logger.warning("Could not store compaction boundary: %s", exc)
        return {}
    return meta


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
        match = _SECTION_RE.match(line)
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


def _section_lines(previous_sections: dict, name: str) -> list[str]:
    return [
        line.strip() for line in previous_sections.get(name, "").splitlines()
        if line.strip() and line.strip().lower() != "- none."
    ]


def _merge_lines(previous: list[str], new: list[str], limit: int) -> list[str]:
    merged, seen = [], set()
    for line in list(previous) + list(new):
        key = line.strip().lower()
        if not key or key == "- none." or key in seen:
            continue
        seen.add(key)
        merged.append(line.strip())
    return merged[-limit:]


def _fallback_checks(events: list) -> list[str]:
    """Verification commands actually executed, paired with their status."""
    results = {
        str(event.get("call_id") or ""): event
        for event in events
        if event.get("type") == "tool_result" and event.get("call_id")
    }
    checks = []
    for event in events:
        if event.get("type") != "tool_call":
            continue
        args = event.get("input")
        if not isinstance(args, dict):
            args = {}
        command = str(args.get("command") or "").strip()
        if not command or not _CHECK_COMMAND_RE.search(command):
            continue
        result = results.get(str(event.get("call_id") or ""))
        status = str((result or {}).get("status") or "").strip()
        outcome = status or ("result not recorded" if not result else "result")
        checks.append(f"- `{_clip(command, 180)}` → {outcome}")
    return checks


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
    previous_done = _section_lines(previous_sections, "Done")
    done = previous_done + done
    if not done:
        done = ["- No completed steps recorded yet."]
    done = done[-12:]

    decisions = _section_lines(previous_sections, "Decisions") or ["- None."]
    constraints = _section_lines(previous_sections, "Constraints") or ["- None."]
    checks = _merge_lines(
        _section_lines(previous_sections, "Checks"),
        _fallback_checks(events),
        6,
    ) or ["- None."]
    unfinished = _section_lines(previous_sections, "Unfinished") or ["- None."]

    # Current describes where the work stands, so it comes from the last
    # assistant message or, failing that, the latest request. Raw tool output
    # is the last resort: it says what ran, not what the session is doing.
    last_assistant = next(
        (event_text(event) for event in reversed(events)
         if event.get("type") == "message" and event.get("role") == "assistant"
         and event_text(event)),
        "",
    )
    last_user = next(
        (event_text(event) for event in reversed(events)
         if event.get("type") == "message" and event.get("role") == "user"
         and event_text(event)),
        "",
    )
    current = _clip(
        last_assistant or last_user or _event_line(events[-1]), 700) or "(not set)"

    next_steps = []
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
        "## Decisions",
        *decisions,
        "",
        "## Constraints",
        *constraints,
        "",
        "## Checks",
        *checks,
        "",
        "## Current",
        current,
        "",
        "## Next",
        *next_steps,
        "",
        "## Unfinished",
        *unfinished,
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
            sections[name] = fallback_sections.get(name) or _default_body(name)
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
        body = sections.get(name) or _default_body(name)
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
    focus: str = "",
) -> tuple[str, str]:
    """Prompts for a rolling update of the existing handoff summary.

    ``focus`` is an optional user instruction for a manual compaction; it
    biases what the summary keeps rather than changing its fixed sections.
    """
    focus = (focus or "").strip()
    system_prompt = (
        "You maintain a compact handoff summary for a local desktop agent. "
        "The event journal is the source of truth. Update the previous summary "
        "with the new events, preserving completed work, decisions, constraints, "
        "verified checks and open threads. Return ONLY Markdown with exactly "
        "these sections, in order:\n\n"
        "## Objective\n"
        "## Done\n"
        "## Decisions\n"
        "## Constraints\n"
        "## Checks\n"
        "## Current\n"
        "## Next\n"
        "## Unfinished\n"
        "## Files\n"
        "## Blockers\n\n"
        "Rules:\n"
        "- Be factual; do not invent actions, decisions or checks that are not in the events.\n"
        "- Keep the user's language (usually Russian).\n"
        "- Objective and Current are one or two sentences; the other sections use short bullet lists.\n"
        "- Done: completed work and its observable outcome.\n"
        "- Decisions: choices made and why; carry earlier decisions forward unless the new events reverse them.\n"
        "- Constraints: rules and limits that must keep holding (user restrictions, project rules, invariants); carry earlier constraints forward.\n"
        "- Checks: verification actually performed (tests, lint, build, manual checks) with its result; never claim a check that was not run.\n"
        "- Next must state the concrete immediate next step, or say that the task appears complete.\n"
        "- Unfinished: started but incomplete work and open questions; do not repeat Next.\n"
        "- Files must be paths that were actually read, written or mentioned.\n"
        "- Do not mention this summarization process or the event journal.\n"
        f"- Stay under {int(max_tokens)} tokens.\n"
        "- Never include passwords, API keys or access tokens."
    )
    if focus:
        system_prompt += (
            "\n- The user asked to focus this compaction on: "
            f"{focus[:FOCUS_MAX_CHARS]}\n"
            "  Keep everything needed for that focus in detail; compress "
            "unrelated parts more aggressively while keeping the sections."
        )
    user_prompt = (
        f"Project: {project_name or '(unnamed)'}\n"
        f"Session mode: {mode or 'unknown'}\n\n"
        "Previous summary:\n"
        f"{previous.strip() or '(none)'}\n\n"
    )
    if focus:
        user_prompt += f"Compaction focus:\n{focus[:FOCUS_MAX_CHARS]}\n\n"
    user_prompt += (
        "New events:\n"
        f"{_select_event_lines(events) or '(none)'}"
    )
    return system_prompt, user_prompt


def _latest_completion(events: list) -> tuple[int, int]:
    """Index and size of the latest completion usage event, or ``(-1, 0)``."""
    for index in range(len(events or []) - 1, -1, -1):
        event = events[index]
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
            return index, total
    return -1, 0


def needs_refresh(
    events: list,
    provider_id: str,
    model_id: str,
    meta: dict,
    session_file: str,
    *,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
    refresh_tokens: int = DEFAULT_REFRESH_TOKENS,
    force: bool = False,
) -> bool:
    """Whether the rolling summary is missing or worth updating now.

    The uncovered-journal token budget is the primary trigger; the window
    fraction stays as a secondary trigger for small windows.
    """
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
        latest_index, actual = _latest_completion(events)
        # A completion the summary already covers describes the context
        # before compaction, not the next request: after a boundary the
        # window fraction must come from a newer completion.
        if actual and latest_index >= processed:
            window = context_usage(provider_id, model_id, "")["window"]
            if window and (actual / window) >= threshold:
                return True
    if count < MIN_EVENTS_FOR_SUMMARY:
        return False
    budget = int(refresh_tokens or 0)
    if budget > 0:
        uncovered = events[processed:] if processed > 0 else list(events)
        lines = []
        for event in uncovered:
            line = _event_line(event)
            if line:
                lines.append(line)
        if lines:
            used = context_usage(provider_id, model_id, "\n".join(lines))["used"]
            if used >= budget:
                return True
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
    if meta.get("handoff_consumed"):
        return False
    source = str(meta.get("source_session") or "")
    if not source or not session_file:
        return True
    return os.path.normcase(os.path.abspath(source)) != os.path.normcase(
        os.path.abspath(session_file))


def mark_handoff_consumed(session_file: str) -> dict:
    """Record that a mode-switch seed has been sent to the model once."""
    meta = read_meta(session_file)
    if not meta or meta.get("source") != "handoff" or meta.get("handoff_consumed"):
        return meta
    meta["handoff_consumed"] = True
    try:
        _atomic_write(meta_path(session_file), json.dumps(meta, ensure_ascii=False, indent=2))
    except OSError as exc:
        logger.warning("Could not mark handoff seed as consumed: %s", exc)
        return {}
    return meta


HANDOFF_LABEL = "Handoff context from earlier work"
COMPACTION_LABEL = "Compacted session context"


def compaction_block(
    summary: str,
    mode: str = "",
    covered_events: int = 0,
) -> str:
    """Prompt block replacing the part of the active history that was compacted."""
    summary = (summary or "").strip()
    if not summary:
        return ""
    label = COMPACTION_LABEL
    if mode:
        label += f" ({mode} mode)"
    if int(covered_events or 0) == 1:
        scope = " 1 earlier event was replaced by this summary to save context."
    elif covered_events:
        scope = (f" {int(covered_events):,} earlier events were replaced by this "
                 "summary to save context.")
    else:
        scope = " Earlier events were replaced by this summary to save context."
    return (
        f"**{label}:**\n\n"
        f"{summary}\n\n"
        f"{scope.strip()} Re-read files or re-run checks if the summary is "
        "not enough; the full history remains in the session journal."
    )


def injection_block(summary: str, mode: str = "") -> str:
    """Prompt prefix carrying prior-session context into the current mode."""
    summary = (summary or "").strip()
    if not summary:
        return ""
    label = HANDOFF_LABEL
    if mode:
        label += f" (continuing in {mode} mode)"
    return (
        f"**{label}:**\n\n"
        f"{summary}\n\n"
        "Use this as background. The current user request is authoritative; "
        "respect the recorded decisions and constraints, and do not repeat "
        "completed steps unless verification is needed."
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
        "Resume from this state. Respect the recorded decisions and "
        "constraints. The live browser tabs or desktop are not replayed; "
        "observe the current screen/page and continue with the user's latest "
        "request."
    )
