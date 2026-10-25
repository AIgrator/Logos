"""Context-window statistics for the current session.

When a provider reports token usage, ``usage`` events supply the current
context size (latest completion) and session totals. Sessions recorded before
usage capture, or providers that report no usage, fall back to estimates
derived from the same journal lines the handoff threshold uses.

Only tokens are reported. Money is deliberately not computed: providers report
prices inconsistently, so Logos never invents a cost.
"""
from __future__ import annotations

from context import handoff
from context.tokens import ModelMetadata, estimate_tokens


def _event_category(event: dict) -> tuple[str, str]:
    """Return ``(token_group, count_key)`` for one journal event."""
    event_type = str(event.get("type") or "")
    if event_type == "message":
        role = str(event.get("role") or "user").lower()
        if role == "assistant":
            return "assistant", "assistant_messages"
        if role == "system":
            return "system", "system_messages"
        return "user", "user_messages"
    if event_type == "reasoning":
        return "reasoning", "reasoning"
    if event_type == "tool_call":
        return "tool_call", "tool_calls"
    if event_type == "tool_result":
        return "tool_result", "tool_results"
    if event_type == "job_result":
        return "tool_result", "job_results"
    if event_type == "error":
        return "error", "errors"
    return "other", "other_events"


def _to_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _aggregate_usage(events: list) -> dict:
    totals = {"input": 0, "output": 0, "reasoning": 0,
              "cache_read": 0, "cache_write": 0}
    latest_tokens = 0
    session_total = 0
    for event in events:
        tokens = {key: _to_int(event.get(key)) for key in totals}
        for key, value in tokens.items():
            totals[key] += value
        event_total = _to_int(event.get("total")) or sum(tokens.values())
        session_total += event_total
        purpose = str(event.get("purpose") or "completion")
        if purpose == "completion" and event_total:
            latest_tokens = event_total
    totals["total"] = session_total
    totals["latest_tokens"] = latest_tokens
    return totals


def build_session_stats(
    events: list,
    provider_id: str,
    model_id: str,
    *,
    summary_text: str = "",
    threshold_ratio: float = handoff.DEFAULT_THRESHOLD_RATIO,
) -> dict:
    """Build an estimated context/session snapshot for display."""
    events = events or []
    counts = {
        "events": len(events),
        "usage_events": 0,
        "messages": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "system_messages": 0,
        "reasoning": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "job_results": 0,
        "errors": 0,
        "other_events": 0,
    }
    groups = {
        "user": 0,
        "assistant": 0,
        "system": 0,
        "reasoning": 0,
        "tool_call": 0,
        "tool_result": 0,
        "error": 0,
        "other": 0,
    }
    lines = []
    usage_events = []
    first_ts = ""
    last_ts = ""

    for event in events:
        if not isinstance(event, dict):
            continue
        stamp = str(event.get("ts") or "")
        if stamp:
            first_ts = first_ts or stamp
            last_ts = stamp
        if event.get("type") == "usage":
            counts["usage_events"] += 1
            usage_events.append(event)
            continue

        group, count_key = _event_category(event)
        counts[count_key] += 1
        if group in ("user", "assistant", "system"):
            counts["messages"] += 1

        line = handoff.event_line(event)
        if not line:
            continue
        lines.append(line)
        groups[group] += estimate_tokens(line)

    estimated_used = estimate_tokens("\n".join(lines))
    actual = _aggregate_usage(usage_events)
    latest_tokens = actual["latest_tokens"]
    context_tokens = latest_tokens or estimated_used
    context_source = "actual" if latest_tokens else "estimated"
    window = ModelMetadata.context_window(provider_id, model_id)
    ratio = (context_tokens / window) if window else 0.0
    threshold = float(threshold_ratio or handoff.DEFAULT_THRESHOLD_RATIO)
    status = "ok"
    if ratio >= 1.0:
        status = "over"
    elif ratio >= threshold:
        status = "near"

    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "window": window,
        "context_tokens": context_tokens,
        "context_source": context_source,
        "latest_usage_tokens": latest_tokens,
        "actual_input": actual["input"],
        "actual_output": actual["output"],
        "actual_reasoning": actual["reasoning"],
        "actual_cache_read": actual["cache_read"],
        "actual_cache_write": actual["cache_write"],
        "actual_tokens": actual["total"],
        "usage_events": len(usage_events),
        "estimated_used": estimated_used,
        "estimated_input": (groups["user"] + groups["system"]
                            + groups["tool_result"] + groups["error"]
                            + groups["other"]),
        "estimated_output": (groups["assistant"] + groups["reasoning"]
                             + groups["tool_call"]),
        "summary_tokens": estimate_tokens(summary_text),
        "ratio": ratio,
        "percent": ratio * 100.0,
        "threshold_ratio": threshold,
        "threshold_tokens": int(window * threshold),
        "status": status,
        "counts": counts,
        "groups": groups,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def format_breakdown(stats: dict) -> str:
    """Compact one-line category breakdown for the dialog."""
    groups = (stats or {}).get("groups") or {}
    parts = []
    for key, label in (
        ("user", "user"),
        ("assistant", "assistant"),
        ("reasoning", "reasoning"),
        ("tool_call", "tool calls"),
        ("tool_result", "tool results"),
        ("system", "system"),
        ("error", "errors"),
        ("other", "other"),
    ):
        value = int(groups.get(key) or 0)
        if value:
            parts.append(f"{label} ~{value:,}")
    return " · ".join(parts)
