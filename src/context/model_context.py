"""Model-facing context assembly.

``command_history`` and ``conversation_history`` are durable session state:
the journal and the UI need every byte. What is sent to the provider is a
derived view. This module owns the trimming rules for that view so Terminal,
Browser and Chat share one policy.

Microcompaction never drops an entry and never breaks a command/result pair.
Old bulky tool outputs keep their head, tail and full-output path; the recent
tail of the history is sent verbatim because the model is acting on it now.

Real compaction is a separate, manual step: a marker entry in the durable
history splits it into the part covered by the session summary and the live
tail. Only the model-facing view is shortened; the journal keeps everything.
"""
from __future__ import annotations

import json
import logging
import os
import re

from context.tokens import estimate_tokens

logger = logging.getLogger(__name__)

RECENT_TAIL_CHARS = 20_000
PRUNED_TOOL_OUTPUT_CHARS = 2_000
IMAGE_TOKEN_ESTIMATE = 1_000

_TOOL_RESULT_PREFIXES = (
    "**Tool Result:**",
    "**Command Result:**",
    "**Background job #",
)
_PROTECTED_PREFIXES = (
    "**Project instructions (AGENTS.md):**",
    "**Handoff context from earlier work",
    "**Current session context",
    "**Compacted session context",
)

COMPACTION_MARKER = (
    "**Session compaction boundary:** earlier entries above this line were "
    "replaced by the session summary."
)

_COMPLETENESS_NOTE = (
    "\n[The complete result stays in the session journal. Re-observe "
    "with a narrower tool call if the omitted part matters.]"
)

_OUTPUT_PATH_PATTERNS = (
    re.compile(r"\[Full output saved to:\s*(.+?)\]"),
    re.compile(r"(?m)^Full output:\s*(.+?)\s*$"),
    re.compile(r'"output_file"\s*:\s*"((?:[^"\\]|\\.)*)"'),
)


def is_protected_entry(entry: str) -> bool:
    """Prompt blocks built from stable project/session state."""
    return str(entry).startswith(_PROTECTED_PREFIXES)


def is_tool_result_entry(entry: str) -> bool:
    return str(entry).startswith(_TOOL_RESULT_PREFIXES)


def has_compaction_marker(history: list) -> bool:
    return any(
        str(entry).startswith(COMPACTION_MARKER)
        for entry in (history or [])
    )


def split_compacted_history(history: list) -> tuple[list, list]:
    """Split a durable history at its newest compaction marker.

    Returns ``(before, after)``. Without a marker the whole history is the
    live tail. The marker itself belongs to neither part.
    """
    entries = list(history or [])
    for index in range(len(entries) - 1, -1, -1):
        if str(entries[index]).startswith(COMPACTION_MARKER):
            return entries[:index], entries[index + 1:]
    return [], entries


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _head_tail(text: str, limit: int, marker: str) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n{marker.format(omitted=omitted)}\n{text[-tail:]}"


def _trim_tracebacks(text: str) -> str:
    if "Traceback (most recent call last):" not in text:
        return text
    lines = text.splitlines()
    if len(lines) <= 10:
        return text
    kept = lines[:3] + [
        f"[... {len(lines) - 8} traceback lines trimmed ...]"
    ] + lines[-5:]
    return "\n".join(kept)


def _full_output_path(text: str) -> str:
    for pattern in _OUTPUT_PATH_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1).strip()
        if not value:
            continue
        if pattern is _OUTPUT_PATH_PATTERNS[2]:
            try:
                value = json.loads(f'"{value}"')
            except (json.JSONDecodeError, TypeError):
                pass
        return value
    return ""


def _condense_entry(entry: str, limit: int = 160) -> str:
    lines = str(entry).strip().splitlines()
    first_line = lines[0] if lines else ""
    if len(first_line) > limit:
        first_line = first_line[:limit] + "..."
    path = _full_output_path(entry) if is_tool_result_entry(entry) else ""
    if path and path not in first_line:
        first_line += f" [full output: {path}]"
    return f"[older step] {first_line}"


def _dedup_snapshots(entries: list, stats: dict) -> list:
    out, last_snapshot = [], None
    for entry in entries:
        if (isinstance(entry, str) and entry.startswith("**Tool Result:**")
                and "Interactive elements:" in entry):
            if entry == last_snapshot:
                out.append(
                    "**Tool Result:** (snapshot unchanged since previous step)")
                stats["deduped_entries"] += 1
                continue
            last_snapshot = entry
        out.append(entry)
    return out


def _prune_tool_result(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    path = _full_output_path(text)
    pruned = _head_tail(
        text, limit,
        "[... {omitted:,} chars of older tool output trimmed from the middle ...]")
    if path:
        if path not in pruned:
            pruned += f"\n[Full output: {path}]"
    else:
        pruned += _COMPLETENESS_NOTE
    return pruned


def _tail_boundary(entries: list, recent_chars: int) -> int:
    """Index of the first entry in the verbatim recent tail.

    An entry that would overflow the tail budget starts the pruned region
    instead of consuming the whole budget, unless it is the newest entry
    (the model is acting on it right now).
    """
    total = 0
    for index in range(len(entries) - 1, -1, -1):
        size = len(entries[index])
        if total and total + size > recent_chars:
            return index + 1
        total += size
        if total >= recent_chars:
            return index
    return 0


def microcompact_agent_history(
    history: list,
    settings: dict | None = None,
    *,
    mode: str = "agent",
    recent_chars: int = RECENT_TAIL_CHARS,
    prune_chars: int = PRUNED_TOOL_OUTPUT_CHARS,
) -> tuple[list, dict]:
    """Build the model-facing agent history and its compaction stats.

    Baseline microcompaction is always on: tool results older than the recent
    tail are reduced to head+tail with their full-output path. Optional
    Settings -> Context rules apply on top and stay no-ops when disabled. The
    input list is never mutated; the journal keeps the full text.
    """
    entries = [
        entry if isinstance(entry, str) else str(entry)
        for entry in (history or [])
    ]
    stats = {
        "mode": mode,
        "entries": len(entries),
        "chars_before": sum(len(entry) for entry in entries),
        "chars_after": 0,
        "saved": 0,
        "settings_trimmed": 0,
        "condensed_entries": 0,
        "deduped_entries": 0,
        "pruned_results": 0,
    }
    if not entries:
        return entries, stats

    cfg = settings if isinstance(settings, dict) else {}
    max_chars = _as_int(cfg.get("history_max_result_chars"))
    keep_last = _as_int(cfg.get("history_keep_last_n"))
    dedup = bool(cfg.get("history_dedup_snapshots"))
    trim_tracebacks = bool(cfg.get("history_trim_tracebacks"))

    compacted = []
    for entry in entries:
        text = entry
        if trim_tracebacks and not is_protected_entry(text):
            text = _trim_tracebacks(text)
        if max_chars and not is_protected_entry(text) and len(text) > max_chars:
            text = _head_tail(
                text, max_chars,
                "[... {omitted:,} chars trimmed from the middle ...]")
            stats["settings_trimmed"] += 1
        compacted.append(text)

    if dedup:
        compacted = _dedup_snapshots(compacted, stats)

    if keep_last and len(compacted) > keep_last:
        cutoff = len(compacted) - keep_last
        for index in range(cutoff):
            if not is_protected_entry(compacted[index]):
                compacted[index] = _condense_entry(compacted[index])
                stats["condensed_entries"] += 1

    boundary = _tail_boundary(compacted, max(0, int(recent_chars or 0)))
    for index in range(boundary):
        entry = compacted[index]
        if is_protected_entry(entry) or not is_tool_result_entry(entry):
            continue
        pruned = _prune_tool_result(entry, max(1, int(prune_chars or 1)))
        if pruned != entry:
            compacted[index] = pruned
            stats["pruned_results"] += 1

    stats["chars_after"] = sum(len(entry) for entry in compacted)
    stats["saved"] = max(0, stats["chars_before"] - stats["chars_after"])
    return compacted, stats


def compact_chat_history(history: list, settings: dict | None = None) -> list:
    """Model-facing Chat history copy; the journal keeps the full list.

    The compaction boundary is applied by the controller, which drops the
    messages covered by the summary and leaves the persisted list untouched.
    """
    del settings  # reserved for the compaction stage
    return [
        dict(message) if isinstance(message, dict) else message
        for message in (history or [])
    ]


def tool_schema_text(mode: str, schema_name: str | None) -> str:
    """Serialized tool definition sent with a structured request, if any."""
    if not schema_name or not mode:
        return ""
    path = os.path.join("cache", str(mode), "response_formats.json")
    try:
        with open(path, encoding="utf-8") as handle:
            formats = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.debug("Could not load tool schema for %s/%s", mode, schema_name)
        return ""
    info = formats.get(schema_name) if isinstance(formats, dict) else None
    if not isinstance(info, dict):
        return ""
    params = info.get("strict_schema") or info.get("schema") or {}
    return json.dumps(
        {
            "name": schema_name,
            "description": info.get("description", ""),
            "parameters": params,
        },
        ensure_ascii=False,
    )


def payload_token_estimate(payload: dict | None) -> dict:
    """Token estimates for the components of one model request."""
    payload = payload if isinstance(payload, dict) else {}
    images = _as_int(payload.get("image_count"))
    return {
        "system": estimate_tokens(str(payload.get("system_text") or "")),
        "history": estimate_tokens(str(payload.get("history_text") or "")),
        "user": estimate_tokens(str(payload.get("user_text") or "")),
        "tool_schema": estimate_tokens(str(payload.get("tool_schema_text") or "")),
        "images": images * IMAGE_TOKEN_ESTIMATE,
    }
