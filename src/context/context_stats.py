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
from context.model_context import payload_token_estimate
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
    latest = {"input": 0, "output": 0, "reasoning": 0,
              "cache_read": 0, "cache_write": 0}
    latest_tokens = 0
    latest_input_tokens = 0
    latest_provider = ""
    latest_model = ""
    session_total = 0
    completion_calls = 0
    summary_calls = 0
    summary_tokens = 0
    for event in events:
        tokens = {key: _to_int(event.get(key)) for key in totals}
        for key, value in tokens.items():
            totals[key] += value
        event_total = _to_int(event.get("total")) or sum(tokens.values())
        session_total += event_total
        purpose = str(event.get("purpose") or "completion")
        if purpose == "summary":
            summary_calls += 1
            summary_tokens += event_total
        elif purpose == "completion":
            completion_calls += 1
        if purpose == "completion" and event_total:
            latest = tokens
            latest_tokens = event_total
            latest_provider = str(event.get("provider") or "")
            latest_model = str(event.get("model") or "")
            # Anthropic reports cache tokens outside input_tokens; for other
            # providers the cached part is already inside the prompt count.
            latest_input_tokens = tokens["input"]
            if "anthropic" in latest_provider.lower():
                latest_input_tokens += (
                    tokens["cache_read"] + tokens["cache_write"])
    totals["total"] = session_total
    totals["latest_tokens"] = latest_tokens
    totals["latest_input_tokens"] = latest_input_tokens
    totals["latest"] = latest
    totals["latest_provider"] = latest_provider
    totals["latest_model"] = latest_model
    totals["completion_calls"] = completion_calls
    totals["summary_calls"] = summary_calls
    totals["summary_tokens"] = summary_tokens
    return totals


def build_session_stats(
    events: list,
    provider_id: str,
    model_id: str,
    *,
    summary_text: str = "",
    threshold_ratio: float = handoff.DEFAULT_THRESHOLD_RATIO,
    payload: dict | None = None,
) -> dict:
    """Build an estimated context/session snapshot for display.

    ``payload`` describes what the next request actually carries (system,
    history, user text, tool schema, images). When the provider reported no
    usage, the payload estimate is a better fill figure than the journal text.
    """
    events = events or []
    payload = payload if isinstance(payload, dict) else {}
    payload_components = payload_token_estimate(payload)
    payload_tokens = sum(payload_components.values())
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
    latest_cache_share = None
    if actual["latest"]["cache_read"] and actual["latest_input_tokens"]:
        latest_cache_share = (
            actual["latest"]["cache_read"] / actual["latest_input_tokens"])
    if latest_tokens:
        context_tokens = latest_tokens
        context_source = "actual"
        estimate_basis = "actual"
    elif payload_tokens:
        context_tokens = payload_tokens
        context_source = "payload"
        estimate_basis = "model_payload"
    else:
        context_tokens = estimated_used
        context_source = "estimated"
        estimate_basis = "journal"
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
        "latest_input_tokens": actual["latest_input_tokens"],
        "actual_input": actual["input"],
        "actual_output": actual["output"],
        "actual_reasoning": actual["reasoning"],
        "actual_cache_read": actual["cache_read"],
        "actual_cache_write": actual["cache_write"],
        "actual_tokens": actual["total"],
        "latest_output": actual["latest"]["output"],
        "latest_reasoning": actual["latest"]["reasoning"],
        "latest_cache_read": actual["latest"]["cache_read"],
        "latest_cache_write": actual["latest"]["cache_write"],
        "latest_cache_share": latest_cache_share,
        "latest_provider": actual["latest_provider"],
        "latest_model": actual["latest_model"],
        "completion_calls": actual["completion_calls"],
        "summary_usage_calls": actual["summary_calls"],
        "summary_usage_tokens": actual["summary_tokens"],
        "usage_events": len(usage_events),
        "estimated_used": estimated_used,
        "estimated_input": (groups["user"] + groups["system"]
                            + groups["tool_result"] + groups["error"]
                            + groups["other"]),
        "estimated_output": (groups["assistant"] + groups["reasoning"]
                             + groups["tool_call"]),
        "summary_tokens": estimate_tokens(summary_text),
        "payload_tokens": payload_tokens,
        "payload_components": payload_components,
        "payload_delta": (actual["latest_input_tokens"] - payload_tokens
                          if actual["latest_input_tokens"] and payload_tokens
                          else None),
        "payload_compact": (payload.get("compact")
                            if isinstance(payload.get("compact"), dict) else {}),
        "estimate_basis": estimate_basis,
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


DEFAULT_HEADROOM_RATIO = 0.15
DEFAULT_MIN_HEADROOM_TOKENS = 16000


def needs_auto_compaction(
    stats: dict,
    *,
    headroom_ratio: float = DEFAULT_HEADROOM_RATIO,
    min_headroom_tokens: int = DEFAULT_MIN_HEADROOM_TOKENS,
) -> bool:
    """Whether free context space is down to the response/compaction buffer."""
    window = _to_int((stats or {}).get("window"))
    used = _to_int((stats or {}).get("context_tokens"))
    if not window or not used:
        return False
    try:
        ratio = float(headroom_ratio)
    except (TypeError, ValueError):
        ratio = DEFAULT_HEADROOM_RATIO
    headroom = max(_to_int(min_headroom_tokens), int(window * max(0.0, ratio)))
    return max(0, window - used) <= headroom


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
