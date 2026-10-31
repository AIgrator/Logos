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
  JSONL session journals, a rolling handoff summary and restored history.
  Sessions are mode-locked: one session runs one mode. Attachments are durable:
  stable files are referenced by path and pasted images are copied into the
  session folder. Tabs/parallel sessions are deliberately out of scope.
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
- **MCP as a toolset source**: Terminal connects to user-configured MCP
  servers through three meta-tools (`mcp_list`, `mcp_get`, `mcp_call`). Servers
  come from settings and the project's `.mcp.json`; project entries require
  explicit trust, every call passes the Terminal access policy, hung servers are
  isolated by a circuit breaker and oversized output spills into the project.
  **Shipped.** Browser/Chat MCP, direct `mcp__<server>__<tool>` names and OAuth
  were deliberately dropped from this round. No per-app integrations are
  written by hand.
- **Cross-mode pilot (Blender)**: Terminal builds through Blender MCP
  (`create_object`, transforms, materials — headless, no GUI), Vision verifies
  the result via a rendered preview through the screen-evaluation loop.
  **Not implemented**; the MCP client it depends on is now shipped.

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

## Principle 4: long-lived sessions (one mode per session)

One-off tasks ("empty the recycle bin") stay as they are. Alongside them, Logos
grows **persistent sessions**: a named thread with a history of what was done, a
rolling summary and the user's attachments, living for weeks and surviving
restarts. A session is a conversation plus an action log, not a snapshot of the
live machine.

Chat and Terminal behave like mature CLI agents: the transcript and project
files are the state. Browser and Vision are different — they drive an external
live environment. Browser/CUA products persist auth profiles and task history,
not a frozen page or desktop: ChatGPT agent keeps cookies across sessions like a
regular browser; Claude Code with Chrome shares the user's browser login state;
browser-use persists profiles (cookies, localStorage, login) but not open tabs;
Anthropic's computer use leaves screenshots and actions in the application's
environment, not on the provider. Cloud agents such as Devin and Manus can
snapshot a VM/sandbox; Logos runs on the user's real desktop and cannot copy that
approach.

**Scope decision (2026-10-26):** one session runs one mode. There are no tabs, no
parallel sessions in one window, no screen arbiter and no virtual displays on the
roadmap. Switching sessions is explicit: Logos restores completed history from
the journal and stops in-flight work instead of pretending to suspend it.

**Current state:** project-scoped sessions, JSONL journals, autosave,
restore/replay, rename/delete, mode locking, the auto-derived session objective,
the session-scoped rolling summary and the `AGENTS.md` project baseline are
implemented. Browser/Vision resumes also get a deterministic summary derived
from the current session's journal. Attachments follow the policy below: files
are referenced by path, pasted images are copied into
`.logos/attachments/<session-id>/` and deduplicated by SHA-256.

### Session contents

- **Title.** Derived from the first request and renameable. It is a short label,
  not a mandatory goal form.
- **Objective.** An auto-derived one-line goal stored in the session header. It
  starts as the first request. Browser and Vision need it because the live
  page/desktop is gone.
- **Journal.** Append-only typed events: messages, reasoning, tool calls and
  results, job results, usage, model switches, errors. It is the source of truth
  for replay and for rebuilding the model-facing agent history.
- **Session summary.** A rolling summary with fixed sections (Objective, Done,
  Decisions, Constraints, Checks, Current, Next, Unfinished, Files, Blockers)
  stored next to the journal. It is scoped to one session; a Browser or Vision
  resume also gets a deterministic state block derived from that session's
  journal.
- **Mode-switch handoff.** Switching mode copies the previous session's summary
  once into the new session (`source=handoff`, `handoff_from`). Explicit "New
  session" starts empty. Nothing is shared through a project-wide summary file.
- **Project baseline.** Root `AGENTS.md` is read as stable rules, never
  generated, and injected separately in every mode.
- **Attachments.** Handled by the policy below.

### Attachments policy

Do not copy the whole project into `.logos`. Use three tiers:

1. **Files with a stable source path are referenced, not copied.** The journal
   records the name, project-relative path when the file is inside the project
   (absolute otherwise), size and content hash. On reopen a changed file is
   marked "changed", a missing one "missing". The source stays the source of
   truth.
2. **Pasted images and explicit user attachments with no stable source path** are
   copied to `.logos/attachments/<session-id>/` and deduplicated by SHA-256.
   Without a copy these bytes are lost on restart; Claude Code solves the same
   problem with a per-session `images/` folder.
3. **Automatic Vision/CUA screenshots are not session artifacts.** They stay in
   the debug screenshot folder with the existing retention window (7 days by
   default). Only screenshots the user explicitly attaches or pins are copied
   into the session.

Attachments appear as cards (name, type, size, changed/missing badge), not as a
re-sent image on every turn. Deleting a session removes its attachment folder
after confirmation, and per-file/per-session caps prevent disk bloat.

Model-facing delivery follows the same reference-first rule. Terminal and
Browser receive a block of attachment references (name, kind, size, path, live
status) and open the file on demand with `read_files`, `list_directory` or
`search_file_content`; Browser includes those read-only filesystem tools.
Chat has no tool loop, so text-file contents are inlined once in the current
request and only referenced afterwards. The Session journal shows the recorded
path, size, SHA-256, stored flag and live status under the carrying message.

### What "resume" means

On reopen Logos restores the transcript and tool cards, rebuilds the model-facing
agent history and injects the session objective plus the relevant summary. What
"the relevant summary" means depends on the mode:

- **Chat / Terminal.** The transcript and rebuilt agent history are enough;
  project files are read live, not snapshotted.
- **Browser.** The journal supplies the action and URL history; the persistent
  Playwright profile supplies cookies/logins. Open tabs, DOM and scroll position
  are not restored; the agent can navigate to the last URL from history.
- **Vision.** A fresh CUA chain observes the current screen. It gets the session
  objective and summary, not the old screenshots or desktop state.

In all modes, in-flight shell jobs and a running agent turn are stopped, not
resumed. Switching sessions intentionally cancels the current run.

This is the same boundary browser/CUA and CLI products draw: they persist
app-owned history and auth profiles, not a frozen page or desktop. Claude Code
persists the conversation, not the filesystem; Cline restores the conversation
and checks file states against checkpoints; opencode compacts old history into a
summary and keeps the recent tail.

### Explicitly not planned

- **Tabs / parallel sessions in one window.** Useful for IDE agents that
  orchestrate several coding agents, but Logos deliberately runs one session at a
  time. The sidebar session list is the navigation surface.
- **Watch-type sessions with diffing.** No mature analogue; a monitored feed is a
  scheduling/automation feature, not a session type.
- **Screen arbiter, session → display binding, virtual displays.** Only needed
  when several Vision tasks run in parallel. With one active session there is
  nothing to arbitrate; revisit only if parallel Vision becomes a real goal.
- **Per-tab pause/serialize.** Pause/resume belongs to the one active agent.
  Session switching intentionally stops work; there is no suspended handler to
  restore.
- **Full browser/desktop snapshot.** Cloud agents such as Devin and Manus can
  snapshot a VM/sandbox; Logos runs on the user's real machine and persists only
  app-owned state.

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
| 4 | Long-lived sessions: JSONL store, autosave, summarization, restore, durable attachments, per-session objective/state | **Shipped:** JSONL sessions, autosave, restore/replay, auto-derived objective, deterministic per-session summary for Browser/Vision resume, durable attachments with reference/copy tiers. Tabs, parallel sessions and the screen arbiter were **dropped** (one mode per session). |
| 5 | Thin client | **Effective:** ML stack removed; optional backends are no longer needed. Installer/packaging cleanup remains. |
| 6 | MCP client + Blender cross-mode pilot | MCP client **shipped** (Terminal meta-tools, settings page, trust gate, keychain secrets, circuit breaker, output spill). Blender pilot **pending.** |
| 7 | Replace the local triple pipeline with a single grounding model (UI-TARS / OmniParser V2) | **Obsolete in current form** — the local pipeline was deleted. Revisit only if a `local` backend is reintroduced. |
| 8 | Parallel Vision sessions / virtual displays | **Dropped.** Only meaningful with several simultaneous Vision sessions; out of scope while one session runs at a time. |

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
