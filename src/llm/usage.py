"""Normalize token usage from the different provider APIs.

Only token counts are normalized and stored — never money. Providers report
prices inconsistently (some per request, most only in admin reports, several
not at all), and a price taken from another provider's catalogue is often
wrong, because resellers and gateways run their own discounts. So Logos shows
the tokens the provider reported and says nothing about cost.

Field semantics still differ per provider, which is why the raw values are
kept and a provider-supplied total is treated as authoritative:

* OpenAI (Responses and Chat Completions): ``total_tokens`` is authoritative;
  ``input_tokens_details.cached_tokens`` / ``cache_write_tokens`` are subsets
  of the input tokens, and ``completion_tokens`` already contains reasoning.
* OpenRouter: same shape as OpenAI (``prompt_tokens`` /
  ``completion_tokens`` / ``total_tokens``).
* Anthropic: ``input_tokens`` *excludes* ``cache_read_input_tokens`` and
  ``cache_creation_input_tokens``; ``output_tokens`` includes thinking. There
  is no total field, so the total is the sum of the parts.
* Google Gemini: ``totalTokenCount`` = prompt + tool-use prompt + thoughts +
  candidates. Thoughts are reported separately from candidates.
* xAI: the Responses API reports ``total_tokens``; the SDK's protobuf
  ``SamplingUsage`` reports ``prompt_tokens``, ``completion_tokens``,
  ``reasoning_tokens`` and ``cached_prompt_text_tokens``, where reasoning is
  *not* part of completion tokens.
"""
from __future__ import annotations


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


def normalize_usage(raw, provider_id: str = "", model_id: str = "") -> dict | None:
    """Convert provider usage into Logos' normalized token fields."""
    data = _as_dict(raw)
    if not data:
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
    total_tokens_value = _to_int(total)

    if not any((input_tokens, output_tokens, reasoning_tokens,
                cache_read_tokens, cache_write_tokens, total_tokens_value)):
        return None

    if not total_tokens_value:
        anthropic_style = ("cache_read_input_tokens" in data
                           or "cache_creation_input_tokens" in data
                           or (provider_id or "").lower() == "anthropic")
        google_style = any(key in data for key in
                           ("prompt_token_count", "candidates_token_count",
                            "thoughts_token_count", "total_thought_tokens"))
        xai_style = "cached_prompt_text_tokens" in data
        if anthropic_style:
            # Anthropic keeps cache tokens outside input_tokens.
            total_tokens_value = (input_tokens + cache_read_tokens
                                  + cache_write_tokens + output_tokens)
        else:
            total_tokens_value = input_tokens + output_tokens
            # Gemini and xAI report reasoning separately from candidates.
            if google_style or xai_style:
                total_tokens_value += reasoning_tokens

    return {
        "input": input_tokens,
        "output": output_tokens,
        "reasoning": reasoning_tokens,
        "cache_read": cache_read_tokens,
        "cache_write": cache_write_tokens,
        "total": total_tokens_value,
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
