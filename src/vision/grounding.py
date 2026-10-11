"""Grounding backends: where do UI coordinates come from?

A GroundingBackend answers one question: given a screenshot, what UI
elements exist and where are they? Execution (Executor) and planning
(prompts) stay backend-agnostic: they consume "location references"
without knowing which model produced them.

Backends:
- "local"  — the built-in YOLO + EasyOCR + Florence-2 pipeline
  (ParserAdapter). Works offline, for every provider.
- "native" — reserved: coordinates returned by the provider's own
  computer-use tool (OpenAI computer_call, Anthropic computer_toolset,
  Google computer_use). Not implemented yet.
- "dom"    — reserved: Playwright accessibility tree for Browser mode.
  Not implemented yet.
"""
import logging

logger = logging.getLogger(__name__)

LOCAL = "local"
NATIVE = "native"
DOM = "dom"

KNOWN_BACKENDS = (LOCAL, NATIVE, DOM)


class GroundingBackend:
    """Abstract coordinate source. Returns the same triple everywhere:
    (image_base64, annotated_frame, parsed_content_list)."""

    name = "base"

    def initialize(self, settings: dict) -> None:
        raise NotImplementedError

    def analyze(self, image_input, settings_manager):
        raise NotImplementedError


class LocalGroundingBackend(GroundingBackend):
    """The current on-device pipeline, wrapped unchanged."""

    name = LOCAL

    def __init__(self):
        self._adapter = None

    def initialize(self, settings: dict) -> None:
        from src.vision.parser_adapter import ParserAdapter
        self._adapter = ParserAdapter()
        self._adapter.initialize_parser(settings)

    def analyze(self, image_input, settings_manager):
        if self._adapter is None:
            raise RuntimeError("LocalGroundingBackend has not been initialized.")
        return self._adapter.analyze_image(image_input, settings_manager)

    def reinitialize_ocr(self, new_settings: dict, status_callback=None):
        if self._adapter is None:
            raise RuntimeError("LocalGroundingBackend has not been initialized.")
        self._adapter.reinitialize_ocr(new_settings, status_callback)

    def update_settings(self, new_settings: dict):
        if self._adapter is not None:
            self._adapter.update_settings(new_settings)


def normalize_backend_name(name: str | None) -> str:
    """Unknown or missing backend names fall back to local (safe default)."""
    if name in KNOWN_BACKENDS and name != LOCAL:
        logger.warning(
            f"Grounding backend '{name}' is registered but not implemented yet; "
            f"falling back to '{LOCAL}'."
        )
        return LOCAL
    if name not in KNOWN_BACKENDS:
        if name:
            logger.warning(f"Unknown grounding backend '{name}'; falling back to '{LOCAL}'.")
        return LOCAL
    return name


def create_backend(name: str | None) -> GroundingBackend:
    """Factory. Currently only 'local' is implemented; the seam is what matters."""
    resolved = normalize_backend_name(name)
    if resolved == LOCAL:
        return LocalGroundingBackend()
    # Future: NativeGroundingBackend / DomGroundingBackend live here.
    return LocalGroundingBackend()
