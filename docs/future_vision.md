# Future Vision: Grounding Backends, New Modes, Thin Client

This document records the agreed evolution plan for Logos AI Assistant
(discussed October 2026, status updated 2026-10-26). It explains the principles
and what is already done, not the implementation details. Implementation details
belong in per-step technical docs; current behavior is in
[system_architecture.md](./system_architecture.md).

## Where we stand

Logos is a provider-agnostic desktop agent with four modes
(Chat / Vision / Terminal / Browser) and a "conductor, not performer" philosophy:
the model plans and evaluates, deterministic code executes, the user reviews or
approves risky steps.

What changed since the original plan:

- **Vision is on the agent loop.** The strategic plan, the plan-approval screen,
  `strategic_plan` / `evaluation` schemas and the old Vision FSM are gone.
  `VisionHandler` uses the same observe → act → assess discipline as
  Terminal/Browser and grounds through the provider's native Computer Use
  protocol. Escalation (`communicate_to_user`, safety dialogs, pause on repeated
  failure) replaces plan approval as the safety valve.
- **The local screen-recognition stack is gone.** Torch/OCR/YOLO/Florence,
  EasyOCR language management and the OmniParser analyzer were removed from the
  code and manifests. There is no background model loading; Vision mode is ready
  immediately and works through `native` grounding only. xAI, OpenRouter and
  other providers without a computer-use tool cannot drive Vision today.
- **Browser mode is shipped.** It uses a persistent Playwright Chromium profile,
  DOM snapshots with stable element refs, screenshots with ref badges, URL/private
  network policy and per-action approvals. This is the `dom` grounding path.
- **Projects and persistent sessions exist.** Each project owns `.logos/` with
  JSONL session journals, a rolling handoff summary, restored history and
  attachments. Sessions are mode-locked. There are still no tabs and no screen
  arbiter.
- **The base install is thin.** Removing the ML stack achieved the size/startup
  goal without optional model downloads. If a local grounding backend returns,
  it must stay an optional add-on rather than a base dependency.

Two structural limitations from the original note are therefore resolved:

1. Vision no longer needs a separate planning universe; it runs the same loop
   and can be extended with shell/file tools.
2. Native provider toolsets no longer conflict with plan approval, because plan
   approval no longer exists.

## Principle 1: separate grounding from execution

The intended interface is still `GroundingBackend` — "where do coordinates come
from?" — with three possible implementations:

- `native` — coordinates/actions from the provider's own computer-use tool
  (`computer_use_turn`: OpenAI computer, Anthropic computer toolset, Google
  Gemini 3.x computer use). **Implemented and currently the only Vision
  backend.**
- `dom` — Playwright accessibility/DOM refs for web pages. **Implemented inside
  Browser mode** (`BrowserManager`), though not yet expressed as a shared
  `GroundingBackend` implementation.
- `local` — a recognizer service (UI-TARS / OmniParser V2 or another model) for
  providers without native computer use. **Not implemented.** The old local
  triple pipeline was deleted, not extracted.

`VisionHandler` is already on the shared agent loop: no strategic plan, no
`strategic_plan`/`evaluation` schemas, no plan-review UI. The remaining work is to
formalize the backend seam and make the coordinate source switchable again
without changing the loop.

Order of work: **native first (done), then the recognizer service** if demand for
non-CUA providers justifies it. The service must expose a stable element schema
and a conformance test so any recognizer can plug in.

## Principle 2: modes differ by toolset, not by universe

New modes reuse the same handler core and the same correction/escalation
protocol:

- **Browser mode** (`backend=dom`, Playwright): open site → authenticate → fill
  and submit forms. **Shipped.** The OpenAI `browser_authentication` idea —
  secrets entered by the user in our UI, never shown to the model — is not
  implemented yet; the current approval flow protects password fields but the
  model still supplies the value. This is the next Browser hardening step.
- **Native execution inside Vision mode** (`backend=native`): **shipped** for
  Google, Anthropic and OpenAI through `supports_computer_use()` /
  `computer_use_turn()`. Provider-specific behavior is documented in the
  provider files and the CUA prompt.
- **Unified Vision+shell toolset**: **partially prepared, not active.** The
  `vision` manifest already contains shell and file tools, and Vision settings
  gate `allow_shell_commands`; the CUA loop does not read that manifest. The
  model still acts only through provider-native computer actions. Wiring the
  manifest into the CUA loop would resolve the last part of limitation (1).
- **MCP as a toolset source**: an `MCPClientManager` exposing tools from
  user-configured MCP servers (`mcp__<server>__<tool>`) into Terminal
  (execution) and Chat (read-only first). Servers are allowlisted in settings,
  side effects require confirmation, hung servers are isolated by a circuit
  breaker. **Not implemented.** No per-app integrations are written by hand.
- **Cross-mode pilot (Blender)**: Terminal builds through Blender MCP
  (`create_object`, transforms, materials — headless, no GUI), Vision verifies
  the result via a rendered preview through the screen-evaluation loop.
  **Not implemented**; depends on MCP.

## Principle 3: thin client distribution

The heavy ML dependencies (torch/CUDA, transformers, EasyOCR models) are
**removed**, not optional. The base install is PySide6 plus app code and starts
without model loading. Consequences:

- No more one-file PyInstaller bundles with the whole `.venv` + CUDA + Hugging
  Face caches.
- Use one-dir builds wrapped in an Inno Setup installer; portable ZIP + launcher
  script remains the zero-friction option.
- If `local` grounding returns, it must be downloaded on demand via the setup
  wizard and never become a required dependency.

## Principle 4: long-lived sessions and tabs

One-off tasks ("empty the recycle bin") stay as they are. Alongside them, Logos
grows **persistent sessions**: named tasks with a goal, a history of what was
done, and attached artifacts (letters, requirements, screenshots), living for
weeks and surviving restarts. Returning to a session restores full context;
watch-type sessions (e.g. tracking a developer console) additionally diff
incoming items since the last visit.

**Current state:** project-scoped sessions, JSONL journals, restore/replay,
attachments and the rolling handoff summary are implemented. Sessions are
mode-locked; switching mode in a non-empty session starts a new one. There is no
tabbed UI and no screen arbiter yet.

Sessions should be presented as **tabs** (one handler + chat log per tab, any
mode). Tab switching serializes and pauses handler state; resuming restores it
losslessly. Constraint: **one display — one active Vision task** (there is a
single physical mouse). A screen arbiter grants the display to one Vision
session; the rest wait paused with explicit indication. Terminal/Chat sessions
may run in the background.

Escape hatch (future, proven in prototype): **virtual displays**. If the user
installs virtual-display drivers, a session can be moved from the main screen to
a virtual one — each display then runs its own Vision task in parallel (clicks on
a secondary display were already demonstrated in the video-lecture project). Not
on the near-term roadmap; recorded here so the arbiter is designed
display-aware from the start (session → display binding, not a global singleton
lock).

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

## Status of the agreed build order

| # | Step | Status |
|---|---|---|
| 1 | `GroundingBackend` interface + unified Vision+shell toolset | Native CUA shipped; interface not formalized; Vision manifest not wired into the CUA loop. **Next foundation.** |
| 2 | Browser mode on Playwright | **Shipped** (DOM refs, policy, approvals, budgets). User-entered secrets still pending. |
| 3 | Native execution for Google, then Anthropic/OpenAI | **Shipped** for all three. |
| 4 | Sessions + tabs: SQLite or JSONL store, autosave, summarization, per-tab handlers, pause/resume, display-aware arbiter | Sessions/journal/handoff/autosave **shipped on JSONL**. Tabs, per-tab pause/resume and screen arbiter **pending**. |
| 5 | Thin client | **Effective:** ML stack removed; optional backends are no longer needed. Installer/packaging cleanup remains. |
| 6 | MCP client + Blender cross-mode pilot | **Pending.** |
| 7 | Replace the local triple pipeline with a single grounding model (UI-TARS / OmniParser V2) | **Obsolete in current form** — the local pipeline was deleted. Revisit only if a `local` backend is reintroduced. |
| 8 | Virtual displays for parallel Vision tasks | **Pending**; requires the arbiter from step 4 to be display-aware. |

## Backlog: known execution gaps (low priority, tracked — do not lose)

- **Native terminal tools ignore timeout and Stop.** `execute_shell` is bounded by
  `shell_timeout_seconds` and killed (whole process tree) on Stop, but direct tools
  (`copy_files`, `write_file`, `read_files`, `delete_files`, `move_files`, …) run on
  the worker thread with no time bound and do not honor cancellation: a long or hung
  call blocks subsequent dispatches (they queue behind it), and `quit()` cannot release
  a thread blocked inside `run()` (`QThread.terminate()` is forbidden — state corruption).
  `ToolExecWorker.cancel()` only prevents delivery of a late result; it cannot stop the
  running function. Fix direction (ONLY when it becomes a priority, not now):
  cooperative cancellation — long tools check `Computer.cancel_event` per file/batch.
  Infinite loops are structurally impossible (no loop tool; shell loops die at the
  timeout). Proven painless so far (76s copy completed, no hangs observed); revisit
  only on real hangs.

- **Client-side streaming tool execution (Chat first).** Provider-side streaming
  tool calls are used where the vendor executes them (OpenAI, Anthropic, Google
  server-side search; xAI Live Search). The client-side path is still a dead end:
  `tool_call_received` has no subscribers and `all_tool_calls` is only logged.
  Current state per provider: OpenAI, Anthropic, Google and xAI declare
  `supports_streaming_tools` and yield typed chunks; OpenRouter and Hugging Face
  do not send `tools` in the stream. Reliability win over hand-formed JSON: the
  call arrives framed (name + arguments as separate fields, JSON parsed
  server-side or accumulated to `stop`), so envelope errors (quotes/brackets) are
  structurally impossible; semantic correctness (right tool, right values) stays
  the model's job, guarded by our `json.loads` fallback + pydantic
  `ChatFunctionCall` + skip. Work items (Chat only — agent modes need whole
  validated actions per turn, not fragments): connect `tool_call_received` to
  execution, feed results back into the conversation, and make OpenRouter/HF
  send `tools`. HF additionally blocked by billing (402 on accounts without
  credits).

- **Per-task folders + cross-mode handoff.** A task ("check the news")
  gets its own folder accumulating all context (chat transcript, turn
  screenshots, traces, downloaded artifacts); later modes reuse it
  (browser finds info → terminal/vision consume it). Handoff need not
  be end-to-end automation: manual carry (user grabs browser findings
  into the folder, vision/terminal pick them up) covers most cases and
  is simpler than cross-mode orchestration. Seeds exist
  (per-task `cua_<tag>/` shots, command_history, trace lines) but no
  folder lifecycle, no handoff protocol, no UI. Project sessions and the
  handoff summary cover part of the context problem; task-level artifact
  folders remain separate work.

## Non-goals (explicitly deferred)

- Adding ~20 new direct LLM providers: other models stay reachable
  through the OpenRouter provider's nested dropdown. A new direct
  provider is justified only for a native API (e.g. Alibaba DashScope
  or Zhipu) with real demand — see `adding_new_llm_provider_guide.md`.
- Chasing benchmark SOTA for its own sake: OSWorld-Verified is adopted
  only as a regression metric, not a target.
