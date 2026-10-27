# src/llm/mode_capabilities.py
"""Per-mode model capabilities.

The controller has four working modes, but only two capability groups:

  chat / terminal / browser - share the SAME requirements (multimodal input +
                              tools/tool_choice) and are grouped as "general"
  vision                    - native computer use (screenshot loop)

The authoritative source is the ``mode_capabilities`` block produced by the
LLM-models generator (``update_models.py``) and shipped inside ``models.json``:

    "mode_capabilities": {
        "general": ["openai::gpt-5", "openrouter::anthropic/claude-opus-5", ...],
        "vision":  ["openai::gpt-6-luna", "anthropic::claude-sonnet-5-5", ...]
    }

Keys are ``"{provider_id}::{stored_model_id}"`` where ``stored_model_id`` matches
what the app stores in ``settings.json`` (OpenRouter ids are ``"group/model"``).

When that block is missing (older cache / remote not regenerated yet) we fall
back to built-in rules so the UI stays usable: everything is "general"-capable
(the list is already tool/vision filtered) and only the native computer-use
providers expose a curated set of vision models.
"""
import logging
import re

logger = logging.getLogger(__name__)

# The four controller modes (mirrors ControllerState minus IDLE).
MODES = ("chat", "terminal", "browser", "vision")

# Capability groups as emitted by the generator.
GENERAL = "general"
VISION = "vision"

# Controller mode -> capability group in models.json.
_CAPABILITY_KEY = {
    "chat": GENERAL,
    "terminal": GENERAL,
    "browser": GENERAL,
    "vision": VISION,
}

# Settings subsections -> the controller modes they cover.
# The general model (chat/terminal/browser) is chosen in the provider section
# above (the "Model" dropdown); here we only expose a dedicated Vision model,
# used when the active model cannot do computer use.
SUBSECTIONS = {
    VISION: (VISION,),
}
SUBSECTION_LABELS = {
    VISION: "Model",
}

# Native providers whose models expose the provider-side computer-use tool.
CUA_PROVIDERS = ("openai", "anthropic", "google")

# Best-effort fallback patterns for vision (native computer use) used only when
# models.json carries no explicit mode_capabilities block. Curated generously:
# false positives just show an extra entry; false negatives hide a working model.
_FALLBACK_VISION_PATTERNS = {
    "openai": (r"computer-use", r"gpt-5\.[6-9]", r"gpt-[6-9]"),
    "anthropic": (r"^claude-",),
    "google": (r"^gemini-(?:2\.5|[3-9])(?![^\s]*?(?:lite|image|nano-banana))",),
}


def model_key(provider_id: str, model_id: str) -> str:
    """Stable key for a (provider, model) pair as used inside models.json."""
    return f"{provider_id}::{model_id}"


class ModeCapabilities:
    """Resolves which (provider, model) pairs support a given mode."""

    # group -> set of model keys coming from models.json; None => use fallback rules.
    _explicit = None

    @classmethod
    def configure(cls, data):
        """Installs the ``mode_capabilities`` block parsed from models.json.

        Passing anything without usable lists (or ``None``) resets to the
        built-in fallback rules.
        """
        parsed = {}
        if isinstance(data, dict):
            general = data.get(GENERAL)
            if not general:
                # Legacy schema (chat/terminal/browser, all identical) — fold
                # them into the single general group.
                general = data.get("chat") or data.get("terminal") or data.get("browser")
            if isinstance(general, (list, tuple, set)) and general:
                parsed[GENERAL] = {str(v) for v in general}
            vision = data.get(VISION)
            if isinstance(vision, (list, tuple, set)) and vision:
                parsed[VISION] = {str(v) for v in vision}
        cls._explicit = parsed or None
        logger.info("ModeCapabilities configured: %s", {m: len(v) for m, v in (parsed or {}).items()} if parsed else "fallback rules")

    @classmethod
    def has_explicit(cls) -> bool:
        """True when models.json supplied explicit capability data."""
        return cls._explicit is not None

    @classmethod
    def supports(cls, provider_id: str, model_id: str, mode: str) -> bool:
        """Whether (provider_id, model_id) can run in ``mode``."""
        if not provider_id or not model_id:
            return False
        group = _CAPABILITY_KEY.get(mode, mode)
        if group == VISION:
            if provider_id not in CUA_PROVIDERS:
                # The app only drives computer use through native CUA providers;
                # routers like OpenRouter are excluded even when a model under
                # the hood would be capable (their key must not add them here).
                return False
            if cls._explicit is not None and VISION in cls._explicit:
                return model_key(provider_id, model_id) in cls._explicit[VISION]
            return cls._fallback_vision(provider_id, model_id)
        # general (chat/terminal/browser)
        # User-defined providers are not in models.json, so the explicit
        # capability list can never contain them; they are OpenAI-compatible
        # chat models and belong to the general group by construction.
        from llm.custom_providers import is_custom_provider
        if is_custom_provider(provider_id):
            return True
        if cls._explicit is not None and GENERAL in cls._explicit:
            return model_key(provider_id, model_id) in cls._explicit[GENERAL]
        return True

    @classmethod
    def supports_subsection(cls, provider_id: str, model_id: str, subsection: str) -> bool:
        """A model fits a settings subsection only if it fits every mode in it."""
        modes = SUBSECTIONS.get(subsection, ())
        return bool(modes) and all(cls.supports(provider_id, model_id, m) for m in modes)

    @classmethod
    def modes_for(cls, provider_id: str, model_id: str) -> list:
        return [m for m in MODES if cls.supports(provider_id, model_id, m)]

    @staticmethod
    def _fallback_vision(provider_id: str, model_id: str) -> bool:
        if provider_id not in CUA_PROVIDERS:
            return False
        name = model_id.lower()
        return any(re.search(p, name) for p in _FALLBACK_VISION_PATTERNS.get(provider_id, ()))
