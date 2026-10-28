"""Reasoning effort levels shared by the model picker and the providers.

The catalogue (models.json) reports which levels each model accepts. Native
APIs accept a narrower set than OpenRouter, so ``levels`` filters them here
once and both the UI and the request builders use the same list.
"""
from context.tokens import ModelMetadata

EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

LABELS = {
    "none": "Off",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Maximum",
}

# Anthropic has no "none"/"minimal"; Google has no "xhigh"/"max".
PROVIDER_LEVELS = {
    "anthropic": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "google": frozenset({"minimal", "low", "medium", "high"}),
}


def levels(provider_id: str, model_id: str) -> list:
    """Usable effort levels for the model, canonical order, possibly empty."""
    reported = ModelMetadata.reasoning_efforts(provider_id, model_id)
    if not reported:
        return []
    rank = {name: index for index, name in enumerate(EFFORT_ORDER)}
    ordered = sorted(set(reported),
                     key=lambda name: (rank.get(name, len(rank)), name))
    allowed = PROVIDER_LEVELS.get(provider_id)
    if allowed is not None:
        ordered = [name for name in ordered if name in allowed]
    return ordered


def default(provider_id: str, model_id: str, available: list | None = None) -> str:
    """Catalogue default when valid, else a sensible level from the list."""
    available = available if available is not None else levels(provider_id, model_id)
    if not available:
        return ""
    reported = ModelMetadata.default_effort(provider_id, model_id)
    if reported in available:
        return reported
    for candidate in ("medium", "high", "low"):
        if candidate in available:
            return candidate
    return available[0]


def effective(provider_id: str, model_id: str, selected: str = "") -> str:
    """Level to send: the saved choice if still valid, else the default."""
    available = levels(provider_id, model_id)
    if not available:
        return ""
    if selected in available:
        return selected
    return default(provider_id, model_id, available)


def label(level: str) -> str:
    return LABELS.get(level, level)
