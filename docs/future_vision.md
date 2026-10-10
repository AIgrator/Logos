# Future Vision: Grounding Backends, New Modes, Thin Client

This document records the agreed evolution plan for Logos AI Assistant
(discussed October 2026). It explains the principles, not the implementation.
Implementation details belong in per-step technical docs.

## Where we stand

Logos today is a provider-agnostic desktop agent with three modes
(Chat / Vision / Terminal) and a "conductor, not performer" philosophy:
the LLM plans and evaluates, deterministic code executes, the user reviews
and approves plans. Screen understanding is a local pipeline
(YOLO icon detection + EasyOCR + Florence-2 captions) wired directly
into `ParserAdapter`. Toolsets are mutually exclusive per mode
(`src/computer/modes/vision.py` vs `terminal.py`).

Two structural limitations follow from this:

1. **Vision plans cannot mix clicks with shell/file commands.** The
   reference architectures for native computer-use (Anthropic:
   `computer + bash + text_editor` in one loop; OpenAI CUA sample:
   `native` + `code` modes) assume a single agent loop with both.
   Our mode manifests forbid it.
2. **Native provider toolsets don't fit the plan-approval loop.**
   `computer_call` (OpenAI), `computer_toolset` (Anthropic) and
   `computer_use` (Google Gemini 3.x) stream actions without an
   upfront user-approved plan, which conflicts with the conductor concept.

## Principle 1: separate grounding from execution

Introduce a `GroundingBackend` interface — "where do coordinates come from?"
with three implementations:

- `local` — the current YOLO + OCR + Florence pipeline (kept for
  xAI/OpenRouter/other providers and offline use);
- `native` — coordinates returned by the provider's own computer-use
  tool (`computer_call` / `computer_toolset` / `computer_use`);
- `dom` — Playwright accessibility tree for web pages (precise
  selectors for forms, free of charge, no pixels involved).

`VisionHandler` keeps its Planning → Execution → Evaluation → Correction
loop untouched; only the coordinate source changes. Native providers
currently capable: **OpenAI, Anthropic, Google** (Gemini 3.x has a
built-in `computer_use` tool with desktop/browser/mobile environments,
normalized 0–999 coordinates, safety decisions and prompt-injection
detection). xAI, OpenRouter (a proxy) and Chinese API providers expose
no equivalent tool — for them `local` remains.

## Principle 2: modes differ by toolset, not by universe

New modes reuse the same handler core and the same correction protocol:

- **Browser mode** (`backend=dom`, Playwright): open site → authenticate
  → fill and submit forms. Authentication follows the OpenAI
  `browser_authentication` pattern: secrets are entered by the user
  in our UI, the model never sees them. Strategy: DOM first,
  pixels as fallback for exotic widgets.
- **Native execution** inside Vision mode (`backend=native`): pilot with
  the Google provider, then Anthropic and OpenAI behind a settings flag.
- **Unified Vision+shell toolset**: a plan step may mix clicks and
  `execute_shell`/file tools, mirroring the Anthropic reference loop.
  This resolves limitation (1) above.

## Principle 3: thin client distribution

Heavy ML dependencies (torch/CUDA, transformers, EasyOCR models)
become **optional offline backends**, downloaded on demand via the
existing setup wizard. The base install (PySide6 + app code) stays at
hundreds of megabytes instead of ~9 GB, starts instantly, and works
fully through native/DOM backends. Consequences:

- No more one-file PyInstaller bundles with the whole `.venv` + CUDA
  + Hugging Face caches (the cause of the 9 GB unstartable build).
- Use one-dir builds wrapped in an Inno Setup installer while the
  thin-client refactor is in progress; portable ZIP + launcher script
  as the zero-friction option (no terminal needed by end users).

## Agreed build order

1. `GroundingBackend` interface + unified Vision+shell toolset
   (foundation; unblocks everything, near-zero risk to current behavior).
2. Browser mode on Playwright (most visible user value: sites, logins, forms).
3. Native execution pilot for the Google provider, then Anthropic, OpenAI.
4. Thin client (possible only after step 3 makes torch optional).
5. Replace the local triple pipeline with a single grounding model
   (UI-TARS / OmniParser V2) once `local` serves only remaining providers.

## Non-goals (explicitly deferred)

- Adding ~20 new direct LLM providers: other models stay reachable
  through the OpenRouter provider's nested dropdown. A new direct
  provider is justified only for a native API (e.g. Alibaba DashScope
  or Zhipu) with real demand — see `adding_new_llm_provider_guide.md`.
- Chasing benchmark SOTA for its own sake: OSWorld-Verified is adopted
  only as a regression metric, not a target.
