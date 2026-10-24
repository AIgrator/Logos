"""Token estimation and per-model metadata.

The LLM-models generator ships a top-level ``model_metadata`` block inside
``models.json``::

    "model_metadata": {
        "openai::gpt-5": {
            "context_window": 400000,
            "reasoning": true,
            "input_modalities": ["text", "image"]
        },
        ...
    }

Keys are ``"{provider_id}::{model_id}"`` (the same scheme used by the
``mode_capabilities`` block). When the block is missing (older cache / source
not regenerated yet) we fall back to a safe default context window so overflow
detection keeps working for unknown models.
"""
import logging

logger = logging.getLogger(__name__)

# Fraction of the context window at which we start warning about overflow.
NEAR_LIMIT_RATIO = 0.6


def estimate_tokens(text: str) -> int:
    """Rough token estimate for mixed Latin/Cyrillic text.

    ~4 characters per token for ASCII, ~2 for non-ASCII. Precision is not the
    goal here — only the order of magnitude, for overflow detection.
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other_chars = len(text) - ascii_chars
    return int(ascii_chars / 4) + int(other_chars / 2) + 1


def model_key(provider_id: str, model_id: str) -> str:
    """Stable key for a (provider, model) pair, matching models.json."""
    return f"{provider_id}::{model_id}"


class ModelMetadata:
    """Resolves per-model context window / reasoning / input modalities."""

    DEFAULT_CONTEXT_WINDOW = 128_000

    _explicit = None  # key -> dict; None => no data installed yet
    _warned = set()

    @classmethod
    def configure(cls, data):
        """Installs the ``model_metadata`` block parsed from models.json."""
        parsed = {}
        if isinstance(data, dict):
            for key, info in data.items():
                if isinstance(info, dict):
                    parsed[str(key)] = info
        cls._explicit = parsed or None
        cls._warned = set()
        logger.info("ModelMetadata configured: %d entries", len(parsed))

    @classmethod
    def has_explicit(cls) -> bool:
        """True when models.json supplied per-model metadata."""
        return cls._explicit is not None

    @classmethod
    def get(cls, provider_id: str, model_id: str) -> dict:
        """Raw metadata dict for the model, or ``{}`` when unknown."""
        if not provider_id or not model_id or cls._explicit is None:
            return {}
        return cls._explicit.get(model_key(provider_id, model_id), {})

    @classmethod
    def context_window(cls, provider_id: str, model_id: str) -> int:
        """Context window in tokens; safe default when unknown."""
        info = cls.get(provider_id, model_id)
        try:
            window = int(info.get("context_window") or 0)
        except (TypeError, ValueError):
            window = 0
        if window > 0:
            return window
        key = model_key(provider_id, model_id)
        if key not in cls._warned:
            cls._warned.add(key)
            logger.warning("Unknown context window for %s; using default %d",
                           key, cls.DEFAULT_CONTEXT_WINDOW)
        return cls.DEFAULT_CONTEXT_WINDOW

    @classmethod
    def supports_reasoning(cls, provider_id: str, model_id: str) -> bool:
        """Whether the model reports a reasoning capability."""
        return bool(cls.get(provider_id, model_id).get("reasoning"))

    @classmethod
    def input_modalities(cls, provider_id: str, model_id: str) -> list:
        """Input modalities reported for the model (e.g. ["text", "image"])."""
        mods = cls.get(provider_id, model_id).get("input_modalities")
        return list(mods) if isinstance(mods, (list, tuple)) else []

    @classmethod
    def pricing(cls, provider_id: str, model_id: str) -> dict:
        """Optional USD prices per million tokens from ``models.json``.

        Recognized keys: ``input_cost_per_mtok``, ``output_cost_per_mtok``,
        ``cache_read_cost_per_mtok`` and ``cache_write_cost_per_mtok``.
        Missing cache prices fall back to the input price.
        """
        info = cls.get(provider_id, model_id)
        prices = {}
        for key in (
            "input_cost_per_mtok",
            "output_cost_per_mtok",
            "cache_read_cost_per_mtok",
            "cache_write_cost_per_mtok",
        ):
            try:
                value = float(info.get(key))
            except (TypeError, ValueError):
                continue
            if value >= 0:
                prices[key] = value
        return prices


def context_usage(provider_id: str, model_id: str, text: str) -> dict:
    """Estimated context usage of ``text`` for a given model.

    Returns ``{"used": int, "window": int, "ratio": float}``.
    """
    used = estimate_tokens(text)
    window = ModelMetadata.context_window(provider_id, model_id)
    ratio = (used / window) if window else 0.0
    return {"used": used, "window": window, "ratio": ratio}
