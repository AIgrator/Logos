"""Unified project context: journal, session summaries and model metadata.

``tokens`` provides token estimation plus per-model context metadata consumed
from ``models.json``. ``handoff`` stores each session's rolling summary next to
its journal, builds summarizer prompts, and creates the deterministic fallback
used when an LLM pass is unavailable. ``project_rules`` reads the optional root
``AGENTS.md`` baseline. ``skills`` discovers Agent Skills (``SKILL.md``) in the
project and user homes. ``model_context`` builds the model-facing view of agent
and Chat history and estimates the request payload. ``context_stats`` merges
actual provider ``usage`` events with those estimates for the Context dialog.
"""
