# Future Vision: Grounding Backends, New Modes, Thin Client

This document records the agreed evolution plan for Logos AI Assistant
(discussed October 2026). It explains the principles, not the implementation.
Implementation details belong in per-step technical docs.

## Where we stand

Logos today is a provider-agnostic desktop agent with four modes
(Chat / Vision / Terminal / Browser) and a "conductor, not performer" philosophy:
the LLM plans and evaluates, deterministic code executes, the user reviews
and approves plans. Screen understanding is a local pipeline
(YOLO icon detection + EasyOCR + Florence-2 captions) wired directly
into `ParserAdapter`. Toolsets are mutually exclusive per mode
(`src/computer/modes/vision.py` vs `terminal.py` vs `browser.py`, plus web tools for `chat.py`).

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
- **MCP as a toolset source**: an `MCPClientManager` exposes tools from
  user-configured MCP servers (`mcp__<server>__<tool>`) into Terminal
  (execution) and Chat (read-only first). Servers are allowlisted in
  settings, side effects require confirmation, hung servers are isolated
  by a circuit breaker. No per-app integrations are written by hand.
- **Cross-mode pilot (Blender)**: Terminal builds through Blender MCP
  (`create_object`, transforms, materials — headless, no GUI), Vision
  verifies the result via a rendered preview through the existing
  screen-evaluation loop. First scenario where two modes cooperate.

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

## Principle 4: long-lived sessions and tabs

One-off tasks ("empty the recycle bin") stay as they are. Alongside them,
Logos grows **persistent sessions**: named tasks with a goal, a history of
what was done, and attached artifacts (letters, requirements, screenshots),
living for weeks and surviving restarts. Returning to a session restores
full context; watch-type sessions (e.g. tracking a developer console)
additionally diff incoming items since the last visit. The mechanism is
domain-independent: the Play-console example is just one instance.

Sessions are presented as **tabs** (one handler + chat log per tab, any
mode). Tab switching serializes and pauses handler state
(Vision: plan, `current_task_index`, evaluation history) and resumes it
losslessly. Constraint: **one display — one active Vision task** (there is
a single physical mouse). A screen arbiter grants the display to one
Vision session; the rest wait paused with explicit indication.
Terminal/Chat sessions may run in the background.

Escape hatch (future, proven in prototype): **virtual displays**. If the
user installs virtual-display drivers, a session can be moved from the
main screen to a virtual one — each display then runs its own Vision
task in parallel (clicks on a secondary display were already demonstrated
in the video-lecture project). Not on the near-term roadmap; recorded
here so the arbiter is designed display-aware from the start
(session → display binding, not a global singleton lock).

## Guiding scenario: "tell the computer, get the result"

The end state this plan serves: a person says what they want — a file
created or edited, a 3D scene, a picture, Windows settings configured
through endless settings windows — and the computer performs it,
returning the finished result instead of instructions for the user to
click through. (Today the user screenshots a settings window, sends it
to a chatbot, gets back "click here", and still clicks themselves;
Logos inverts this: the user states the goal, the agent clicks.)

Voice input is the natural interface for this scenario but is
explicitly out of scope for now — text input carries the full design
until the execution core is ready. Voice will dock onto the same
`start_task(user_text)` entry point later without architectural changes.

## Agreed build order

1. `GroundingBackend` interface + unified Vision+shell toolset
   (foundation; unblocks everything, near-zero risk to current behavior).
2. Browser mode on Playwright (most visible user value: sites, logins, forms).
3. Native execution pilot for the Google provider, then Anthropic, OpenAI.
4. Sessions + tabs: SQLite session store with autosave, history
   summarization, per-tab handlers, pause/resume, display-aware screen
   arbiter. Testable per piece without touching execution quality.
5. Thin client (needs step 3 to make torch optional; benefits from step 4
   since sessions already survive restarts and reinstalls).
6. MCP client + Blender cross-mode pilot.
7. Replace the local triple pipeline with a single grounding model
   (UI-TARS / OmniParser V2) once `local` serves only remaining providers.
8. Virtual displays for parallel Vision tasks (only on demand; requires
   the arbiter from step 4 to be display-aware).

## Backlog: known execution gaps (low priority, tracked — do not lose)

- **Native terminal tools ignore timeout and Stop.** `execute_shell` is bounded by
  `shell_timeout_seconds` and killed (whole process tree) on Stop, but direct tools
  (`copy_files`, `write_file`, `read_files`, `delete_files`, `move_files`, …) run on
  the worker thread with no time bound and do not honor cancellation: a long or hung
  call blocks subsequent dispatches (they queue behind it), and `quit()` cannot release
  a thread blocked inside `run()` (`QThread.terminate()` is forbidden — state corruption).
  Fix direction (ONLY when it becomes a priority, not now): cooperative cancellation —
  long tools check `Computer.cancel_event` per file/batch. Infinite loops are
  structurally impossible (no loop tool; shell loops die at the timeout). Proven painless
  so far (76s copy completed, no hangs observed); revisit only on real hangs.

## Non-goals (explicitly deferred)

- Adding ~20 new direct LLM providers: other models stay reachable
  through the OpenRouter provider's nested dropdown. A new direct
  provider is justified only for a native API (e.g. Alibaba DashScope
  or Zhipu) with real demand — see `adding_new_llm_provider_guide.md`.
- Chasing benchmark SOTA for its own sake: OSWorld-Verified is adopted
  only as a regression metric, not a target.
