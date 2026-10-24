"""Normalize token usage from the different provider APIs.

Field semantics differ per provider, and getting them wrong silently inflates
or deflates every context/cost number. The rules below follow the official
documentation of each API:

* OpenAI (Responses and Chat Completions): ``usage.total_tokens`` is
  authoritative; ``input_tokens_details.cached_tokens`` and
  ``input_tokens_details.cache_write_tokens`` are *subsets* of the input
  tokens, and ``completion_tokens`` already contains ``reasoning_tokens``.
* OpenRouter: same shape as OpenAI (``prompt_tokens`` / ``completion_tokens``
  / ``total_tokens``), plus a provider-reported ``cost`` in credits.
* Anthropic: ``input_tokens`` *excludes* ``cache_read_input_tokens`` and
  ``cache_creation_input_tokens``; ``output_tokens`` includes thinking tokens.
  There is no total field, so the total is the sum of the parts.
* Google Gemini: ``totalTokenCount`` = prompt + tool-use prompt + thoughts +
  candidates. Thoughts are reported separately from candidates.
* xAI: the Responses API reports ``total_tokens``; the SDK's protobuf
  ``SamplingUsage`` reports ``prompt_tokens``, ``completion_tokens``,
  ``reasoning_tokens``, ``cached_prompt_text_tokens`` and
  ``cost_in_usd_ticks``, where reasoning is *not* part of completion tokens.

Values are stored as reported. Two flags record the semantics so cost math can
stay correct: ``cache_in_input`` (cached tokens are already counted in
``input``) and ``reasoning_in_output`` (reasoning is already counted in
``output``). ``total`` is the authoritative request size whenever the provider
supplies one.
"""
from __future__ import annotations

# xAI reports cost in "ticks", where one US dollar is 10^10 ticks.
TICKS_PER_USD = 10_000_000_000


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    # Protobuf messages (the xAI SDK returns xai.api.v1.usage_pb2.SamplingUsage).
    if hasattr(value, "DESCRIPTOR") and hasattr(value, "ListFields"):
        try:
            from google.protobuf.json_format import MessageToDict

            data = MessageToDict(value, preserving_proto_field_name=True)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        try:
            return {field.name: getattr(value, field.name)
                    for field in value.DESCRIPTOR.fields}
        except Exception:
            pass
    for method in ("model_dump", "to_dict", "dict"):
        converter = getattr(value, method, None)
        if callable(converter):
            try:
                data = converter()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def _nested(data: dict, *path):
    current = data
    for key in path:
        current = _as_dict(current).get(key)
        if current is None:
            return None
    return current


def _first(data: dict, *keys):
    for key in keys:
        if key in data and data.get(key) is not None:
            return data.get(key)
    return None


def _to_int(value) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_usage(
    raw,
    provider_id: str = "",
    model_id: str = "",
    *,
    cost=None,
    currency: str = "",
) -> dict | None:
    """Convert provider usage into Logos' normalized token fields."""
    data = _as_dict(raw)
    if not data and cost is None:
        return None

    prompt = _first(data, "input_tokens", "prompt_tokens", "prompt_token_count",
                    "input_token_count", "total_input_tokens")
    output = _first(data, "output_tokens", "completion_tokens",
                    "candidates_token_count", "output_token_count",
                    "total_output_tokens")
    reasoning = _first(data, "reasoning_tokens", "thoughts_token_count",
                       "total_thought_tokens")
    if reasoning is None:
        reasoning = _nested(data, "output_tokens_details", "reasoning_tokens")
    if reasoning is None:
        reasoning = _nested(data, "completion_tokens_details", "reasoning_tokens")

    cache_read = _first(data, "cache_read_input_tokens",
                        "cached_content_token_count", "total_cached_tokens",
                        "cached_prompt_text_tokens")
    if cache_read is None:
        cache_read = _nested(data, "input_tokens_details", "cached_tokens")
    if cache_read is None:
        cache_read = _nested(data, "prompt_tokens_details", "cached_tokens")

    cache_write = _first(data, "cache_creation_input_tokens",
                         "cache_write_tokens")
    if cache_write is None:
        cache_write = _nested(data, "input_tokens_details", "cache_write_tokens")
    if cache_write is None:
        cache_write = _nested(data, "prompt_tokens_details", "cache_write_tokens")

    total = _first(data, "total_tokens", "total_token_count")

    input_tokens = _to_int(prompt)
    output_tokens = _to_int(output)
    reasoning_tokens = _to_int(reasoning)
    cache_read_tokens = _to_int(cache_read)
    cache_write_tokens = _to_int(cache_write)
    total_tokens = _to_int(total)

    provider = (provider_id or "").lower()
    openai_style = any(key in data for key in
                       ("prompt_tokens", "completion_tokens"))
    google_style = any(key in data for key in
                       ("prompt_token_count", "total_token_count"))
    anthropic_style = "cache_read_input_tokens" in data or provider == "anthropic"
    xai_style = "cached_prompt_text_tokens" in data

    # Anthropic keeps cache tokens outside input_tokens; everyone else counts
    # them inside the prompt total.
    cache_in_input = not anthropic_style
    # OpenAI, OpenRouter and HuggingFace count reasoning inside
    # completion_tokens; xAI and Gemini report it separately.
    reasoning_in_output = (openai_style and not xai_style
                           and not google_style and not anthropic_style)
    if anthropic_style:
        reasoning_in_output = True  # thinking tokens are part of output_tokens

    if not total_tokens:
        if anthropic_style:
            total_tokens = (input_tokens + cache_read_tokens
                            + cache_write_tokens + output_tokens)
        else:
            total_tokens = input_tokens + output_tokens
            if not reasoning_in_output:
                total_tokens += reasoning_tokens

    reported_cost = cost
    if reported_cost is None:
        reported_cost = _first(data, "cost", "total_cost", "cost_usd")
    if reported_cost is None:
        ticks = _to_float(_first(data, "cost_in_usd_ticks"))
        if ticks:
            reported_cost = ticks / TICKS_PER_USD
    reported_cost = _to_float(reported_cost)

    if not any((input_tokens, output_tokens, reasoning_tokens,
                cache_read_tokens, cache_write_tokens,
                total_tokens)) and reported_cost is None:
        return None

    return {
        "input": input_tokens,
        "output": output_tokens,
        "reasoning": reasoning_tokens,
        "cache_read": cache_read_tokens,
        "cache_write": cache_write_tokens,
        "total": total_tokens,
        "cache_in_input": cache_in_input,
        "reasoning_in_output": reasoning_in_output,
        "cost": reported_cost,
        "cost_source": "provider" if reported_cost is not None else "",
        "currency": (currency or data.get("currency")
                     or ("USD" if reported_cost is not None else "")),
        "provider": provider_id,
        "model": model_id,
    }


def merge_usage(base: dict | None, new: dict | None) -> dict | None:
    """Combine cumulative stream updates, keeping the largest token counts."""
    if not base:
        return new
    if not new:
        return base
    merged = dict(base)
    for key in ("input", "output", "reasoning", "cache_read", "cache_write",
                "total"):
        merged[key] = max(int(base.get(key) or 0), int(new.get(key) or 0))
    for flag in ("cache_in_input", "reasoning_in_output"):
        merged[flag] = new.get(flag, base.get(flag))
    if new.get("cost") is not None:
        merged["cost"] = new["cost"]
        merged["cost_source"] = new.get("cost_source") or "provider"
    if new.get("currency"):
        merged["currency"] = new["currency"]
    if new.get("provider"):
        merged["provider"] = new["provider"]
    if new.get("model"):
        merged["model"] = new["model"]
    return merged


def total_tokens(usage: dict | None) -> int:
    """Authoritative request size: provider total when known, else the parts."""
    usage = usage or {}
    total = int(usage.get("total") or 0)
    if total:
        return total
    return sum(int(usage.get(key) or 0) for key in
               ("input", "output", "reasoning", "cache_read", "cache_write"))
