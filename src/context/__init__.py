"""Unified project context: journal, handoff summary and model metadata.

``tokens`` provides token estimation plus per-model context metadata consumed
from ``models.json``. ``handoff`` stores the rolling ``summary.md`` and its
provenance sidecar, builds summarizer prompts, and creates the deterministic
fallback used when an LLM pass is unavailable.
"""
