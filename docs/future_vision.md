# Future Vision: One Session, Three Capabilities

> Status: agreed 2026-11-04. This document replaces the mode-centric plan that
> used to live here. It describes **what the application should become**, the
> principles behind it and how we will know we got there. Implementation details
> belong in per-step technical notes; current behavior stays documented in
> [system_architecture.md](./system_architecture.md).
>
> Work happens on a **local branch only** for now. Nothing here is pushed until
> the plan is executed and reviewed.

## The goal in one paragraph

A person should be able to say "run my project's dev server and check that the
login page works" in **one session** and watch the assistant open a terminal,
start the server, open a browser, click through the form, read the console and
fix the code — without the human touching a mode switcher. Chat, Terminal and
Browser stop being separate universes with separate sessions and separate
histories. They become **capabilities of one agent in one timeline**. Vision
stays the fourth capability, granted only where the chosen model can drive it.

## Where we are today

Honest inventory of the current build (0.3.1), because the target is defined
against it.

**Modes are sessions.** A session is locked to one mode. The journal header
stores `mode`, `AppController.session_mode` is the single source of truth, and
switching mode in a non-empty session starts a **new session** plus a one-time
handoff summary. Each mode therefore has its own transcript; nothing interleaves
terminal steps and browser steps in one timeline.

**Modes are toolsets.** `src/computer/modes/{chat,terminal,browser,vision}.py`
declare the tools of a mode, and `src/computer/tool_registry.py` renders them
into `cache/<mode>/tools.txt` and `cache/<mode>/response_formats.json`. The
pydantic schemas in `src/computer/schemas/` carry a hardcoded name list per
mode. Baseline: Chat 2, Terminal 29, Browser 26, Vision 21 tools (the counts
now read 31 and 28 after `open_browser` and `open_terminal`; both agent schemas
list the union from `schemas/agent_tools.py` instead of a per-mode list).

**Modes are prompts.** `prompts/<mode>/system.txt` and per-mode user templates;
`Brain.get_<mode>_prompts()` assembles them. The system prompt, the tool block
and the response format are all mode-shaped.

**Handlers are mode-shaped.** `TerminalHandler`, `BrowserHandler` and
`VisionHandler` share the same loop from `BaseAgentHandler` (observe → act →
assess). Terminal owns `ShellJobManager` and the MCP manager; the Playwright
`BrowserManager` and Chromium belong to the whole app (`BrowserService`, see
Phase 1) and both agent handlers dispatch into it; Vision owns the provider's
Computer Use chain. `ChatHandler` has no tool loop at all.

**The UI is mode-shaped.** A mode selector switches handlers, `_active_mode_key`
drives the toolbar, the composer and the panels; compact mode is a window
variant, not a mode.

**Model capability is already unified.** `ModeCapabilities` puts chat, terminal
and browser in one `general` group; only Vision requires a CUA provider. So the
model layer is already ready for "one session, several capabilities"; the
session and tool layers are what still say "one mode".

**What already works in our favor:** background jobs with no timeout and
`ready_url` probing, the screenshot tool (window / screen / region) with
one-shot attachment, the browser ref-based toolset, console/network capture
written to `<project>/.logos/browser_logs/`, per-capability approval policies,
project-scoped journals with replay, and a rolling summary.

**What breaks for the user:** a web-development loop needs three manual
switches per iteration (Terminal → Browser → Terminal), three sessions, three
histories, and a summary instead of the real context. The dev server survives
the switch, the page does not: Chromium closes on leaving Browser mode, so the
next iteration starts with `browser_goto` again.

## What we want instead

1. **One session, one timeline.** A session is a conversation plus an action
   log, not a mode. Terminal steps, browser steps and chat answers interleave in
   one journal and one transcript.
2. **Capabilities, not modes.** A session carries a set of granted capabilities:
   `base` (always), `terminal`, `browser`, `vision`. Tools and prompt sections
   are derived from that set.
3. **No mode switcher.** The UI exposes capabilities through their panels: the
   Terminal panel and the Browser panel attach their capability when used. A
   single toggle can revoke one. Vision is a panel too, but it can only be
   attached when the active model supports Computer Use.
4. **The model can attach capabilities itself.** `open_terminal()` and
   `open_browser(url)` are real, callable tools that attach the capability and
   return a handle. A sentence in the system prompt is a hint; a tool is a
   mechanism. After a successful call the capability's tools are available on
   the **next** step.
5. **Nothing silently widens access.** Attaching a capability is visible in the
   transcript ("Terminal attached"), and each capability keeps its own approval
   policy: Terminal access mode, Browser `classify_action`, Vision confirmation.
   A grant changes *what is possible*, never *what is allowed without asking*.

## Principles

### P1. Grants and exposure are different axes

- **Grants** answer "what may this session do now": `base`, `terminal`,
  `browser`, `vision`.
- **Exposure** answers "how do these tools reach the model": `native` (declared
  in the request schema), `catalog` (listed in a short catalogue, loaded on
  demand by `search_tools`), `hidden` (exists but is not advertised).

Tool availability is a function of the **current grants**, never of history
("the browser was opened once, so always send browser tools").

### P2. Base is always on

`base` tools are the ones that make the agent useful before any capability is
attached: `read_files`, `list_directory`, `search_file_content`, `finish`,
`communicate_to_user`, `update_plan`, `use_skill`, `open_terminal`,
`open_browser`. They are project-scoped and read-only except for the two
openers. Chat becomes a base-only session — which is exactly what "chat with
file access" should have been.

### P3. The tool list changes on capability change, never per message

A turn must keep a stable tool block: changing it every request destroys prompt
caching and confuses the model. Recompute the tool block only when a grant is
added or removed, and keep the rest of the system prompt identical.

### P4. A capability owns its resources

`terminal` owns `ShellJobManager` and the MCP manager; `browser` owns the single
Playwright `BrowserManager` and its Chromium; `vision` owns the CUA chain. A
capability is attached lazily on first need and stays attached for the session.
Handlers stop being session owners and become capability providers.

### P5. One browser, one shell manager, app-wide

There is exactly one Chromium per application, owned by the controller, shared
by whatever capability is attached — never one per handler. Same for the shell
job manager. Two persistent contexts on one profile cannot coexist, and two
profiles would silently split logins and cookies. The browser is also bound to
one long-lived worker thread it was started on — a capability borrows the
browser, it never runs it on its own tool thread, which may be replaced after a
hard Stop.

### P6. Approvals stay per capability and fail closed

- Terminal keeps `terminal.access_mode` (`ask` / `auto` / `full`) and the
  read-only allowlist.
- Browser keeps `classify_action` and `browser.require_approval`; `browser_eval`
  keeps asking every time.
- Vision keeps its confirmation flow.
- MCP keeps its trust gate and its read-only/risky split.

Attaching a capability is a grant of possibility, not a grant of trust.

### P7. Read-only work never asks; anything that changes the world does

The existing split stays: `read_files`, `browser_snapshot`, `browser_console`,
`screenshot` and friends run silently; `execute_shell` with a state-changing
command, `browser_click` on a risky control, file deletes, sensitive paths and
out-of-project writes ask according to their capability's policy.

### P8. Revoking is explicit and immediate

A capability is revoked only by an explicit action (toggle, session end, project
switch, application exit). Closing a panel hides it, it does not revoke — the
model may still be mid-task. On revoke, the capability closes its resources
(Chromium, MCP clients, background jobs) in a bounded, logged way.

## Session model

**Header.** The journal header gains `capabilities: ["base", "terminal", ...]`.
The legacy `mode` field stays readable forever, so 0.3.1 sessions keep opening.

**Migration.** `chat → base`, `terminal → base+terminal`,
`browser → base+browser`, `vision → base+vision`. Migration happens on load and
is written back on the next append; no separate migration pass.

**No new session on capability change.** Adding browser to a terminal session
keeps the same journal, the same transcript and the same summary. "New session"
resets grants to `base`.

**Summaries.** The handoff summary stops being the bridge between modes, because
there are no mode boundaries inside a session. It remains for explicit session
switches and for compaction of long sessions. The compaction boundary and the
summary sections are unchanged.

**Transcript.** One timeline. Steps from different capabilities sit in the same
turn blocks; the step icon/color still identifies the capability, so a reader can
tell a shell step from a browser step at a glance.

## Tool exposure plan

Phase 1 shipped **all tools of every granted capability** as `native`, because
that was the smallest correct step. Phase 3 started by splitting them: the
common step-by-step tools stay `native` (declared with full schemas in every
request), and the rest are granted but only *named* in a closing
"Tools on demand" section of the prompt. The model calls `search_tools` to load
one; it is declared (and accepted by the strict envelope) from the next request
on.

| Capability | Native (always declared) | Catalog (on demand) |
|---|---|---|
| base | read_files, list_directory, search_file_content, open_terminal, open_browser, search_tools, finish, communicate_to_user, update_plan, use_skill | — |
| terminal | find_files, path_exists, create_directory, write_file, replace, replace_many, delete_files, copy_files, move_files, execute_shell, poll_shell_job, stop_shell_job, wait_seconds, screenshot, get_clipboard_content, web_search, web_fetchs, download_file, display_message | mcp_list, mcp_get, mcp_call |
| browser | browser_goto, browser_snapshot, browser_click, browser_fill, browser_press, browser_read, browser_screenshot, browser_wait_for, browser_tabs, browser_close | browser_eval, browser_console, browser_network, browser_frames, browser_handle_dialog, browser_select_option, browser_hover, browser_find, browser_navigate_back |
| vision | the provider's Computer Use toolset plus the CUA prompt | — |

Actual counts: a terminal session declares 29 tools (3 on demand), a browser
session 20 (9 on demand), and a session holding both capabilities 39. That is
above the target of ≈30 for the mixed case because a few tools were kept native
deliberately — `find_files`, `path_exists` and `create_directory` are ordinary
file work, and hiding them would have changed behaviour for a marginal saving.
Measured on the generated blocks: the mixed session went from 51 declared tools
and 20.8 KB to 39 and 15.9 KB (≈1.2k tokens per request), and a pure browser
block from 11.4 KB to 7.7 KB.
The catalog is a **prompt budget, not a permission**: `search_tools` only
reaches tools the grants already hold.

Code Mode — a JavaScript sandbox that composes tool calls — stays out of scope;
our scale does not need it.

## Prompts

The assembled system prompt becomes:

```
base instructions
+ Terminal section      (if terminal granted)
+ Browser section       (if browser granted)
+ Vision CUA section    (if vision granted)
+ skills catalogue
+ MCP servers block     (if terminal granted)
+ AGENTS.md baseline
+ response format for the granted tool set
```

Sections keep their current text; they are no longer selected by mode but
concatenated by grant. The single-line "you may open a browser" hint is not the
mechanism — `open_browser` is. The prompt only explains when using it is worth
it.

## UI

- The mode selector is replaced by capability controls: **Terminal**,
  **Browser**, **Vision**. Each shows attached/not attached.
- Opening the Terminal or Browser panel attaches that capability (same click
  that reveals the panel). Vision is disabled, with an explanation, when the
  active model has no Computer Use.
- The workspace shows the panels of attached capabilities; the right panel's
  Session tab lists capability chips instead of a mode.
- The composer's access chip (Terminal) and the browser approval dialogs keep
  working unchanged.
- Home screen: "New session" starts with `base` only. A first message like "run
  the tests" lets the model attach Terminal by itself.
- Compact mode remains a window variant and is unaffected.

## Lifecycle and safety

- **Attach** is lazy: first use of a capability tool, an `open_*` call, or a
  panel click.
- **Stop** cancels the current step and, as today, stops background jobs and the
  running turn. We keep today's predictable "Stop means stop everything"
  semantics; a softer "stop the turn, keep the server" is a later, separate
  decision.
- **Project switch and exit** close Chromium, MCP clients and jobs, as today.
- **Grants do not leak across projects**: a capability attaches to the current
  project's session only.
- **Vision** is never attached automatically: without a CUA-capable provider it
  cannot run, and silently switching models is not allowed.

## Model handling

Chat, Terminal and Browser share the `general` group, so one model can serve all
three capabilities; the picker does not change when a capability is attached.
Vision is the exception: attaching it requires a CUA model, so the UI offers the
configured Vision model and explains the requirement otherwise. A model switch
inside a session stays journaled as a `model` event, exactly as today.

## Roadmap

Each phase must leave the previous behavior intact and keep old sessions
readable.

**Phase 0 - foundation, no visible change.**
`ToolSpec(capability, exposure)` in the registry; `capabilities` in the session
header with migration; tool block and prompt assembled from grants; per-mode
`tools.txt` files replaced by one capability catalogue. Modes still behave as
today, one mode per session, but the data model is capability-first.
*Done when:* the three agent modes behave identically, old journals open, the
generated tool block for a terminal session is equivalent to today's, and the
schema check reports no problems.

> **Status (branch `feature/one-session-capabilities`).** The capability registry
> (`src/computer/capabilities.py`), the `capabilities` header field with its
> migration from `mode`, the generated `cache/capabilities/tools.json` and
> `AppController.session_capabilities` have landed. The per-mode cache files and
> the strict schemas were regenerated and diffed byte-for-byte against the old
> build, so nothing the model sees has changed yet.
>
> Phase 1 has started: choosing another panel now attaches its capability to the
> open session (`_prepare_session_for_mode` merges grants and rewrites only the
> header, `ProjectManager.update_session_grants`), and the single Chromium of the
> app lives in `BrowserService`, which Terminal can dispatch `browser_*` calls
> into. The tool block and the response format now follow the grants
> (`ToolRegistry.generate_session_profile` writes `cache/session/<handler>/`,
> `session_tools.py` feeds the prompt, `tool_catalog.py` merges the session files
> over the per-mode ones and stamps the provider caches), so a terminal turn in a
> session that also holds Browser is told about the browser tools and its
> envelope accepts them; a pure terminal or browser grant set stays byte-for-byte
> identical to the per-mode files. The last piece of this phase is the merged
> loop: `TerminalHandler` is now the app's only agent loop and presents as
> either Terminal or Browser (`presentation`, `schema_name`, `tool_view`), so
> `AppController.browser_handler` is the same object as `terminal_handler`. A
> browser turn can run shell tools and a terminal turn can run `browser_*` ones,
> because one handler owns both the exec thread and the shared browser service;
> the panel only picks the system prompt, the response envelope and the
> `cache/session` view, and the browser step/time budgets and the persistent
> screenshot rule follow the browser presentation. The capability controls that
> replace the mode selector have landed too: `src/ui/widgets/capability_bar.py`
> holds Chat / Terminal / Browser / Vision, a check marks a granted capability
> and the checked control is the open panel, a click opens the panel and attaches
> its capability, and right-click detaches (explicit revoke, P8) with a fallback
> to the nearest panel the session still holds. Phase 2 has started with the first
> half: `open_browser(url)`
> is a real tool that attaches the browser capability from inside a terminal turn
> (grants grow, nothing is summarized, the page opens on the shared service), and
> `browser_snapshot` works on the next step. The `open_terminal()` half is
> described under Phase 2 below.

**Phase 1 — one session, granted capabilities.**
Capability changes stop creating sessions. UI panels attach/revoke. The selector
becomes three capability controls. Every granted capability's tools are native.
*Done when:* attaching Browser inside a terminal session keeps the same journal
and transcript, and the model can use terminal and browser tools in one turn.

**Phase 2 — the model attaches capabilities.**
`open_terminal()` and `open_browser(url)` as real tools with visible notices and
the "next step" availability rule.
*Done when:* asked to "start the dev server and check the page", the model
attaches Terminal, starts the server with `ready_url`, attaches Browser, checks
the page, and everything is one turn in one session.

> **Status.** Both halves have landed. `open_browser(url)` is a base tool that
> attaches the browser capability and navigates in one step; `open_terminal()`
> is a base control tool that attaches the terminal capability (and starts the
> MCP manager) with a visible notice and a tool result telling the model the
> shell/file/MCP tools are available on the next step. Because the two panels
> share one loop, either call grants tools the running handler can actually
> execute.

**Phase 3 — exposure budget.**
Native/catalog split from the table above, `search_tools`, measurement of the
prompt prefix. *Done when:* base+terminal+browser fits a sane native budget
(≈30 tools) and the rest is reachable on demand without confusing the model.

> **Status.** The split has landed. `capabilities.CATALOG_TOOLS` names the
> on-demand tools; `native_tools_for_grants` / `catalog_tools_for_grants` derive
> both sets from the grants (so `tools_for_handler` still never widens beyond
> them), and `ToolRegistry` writes the declared set plus a names-only
> "Tools on demand" section — in the session views **and** in the per-mode files,
> so a pure grant set still equals its mode block byte for byte. `search_tools`
> is a `base` control tool intercepted by the agent loop (like `update_plan`):
> it matches a name or a description, returns the loaded schema and promotes the
> tool into `_promoted_tools`, which the next prompt and strict schema include and
> which `start_task` clears. The strict envelope is built from the declared set,
> so a catalog tool is accepted by the grammar only after it was loaded. Numbers:
> 29 native for terminal, 20 for browser, 39 for both (the ≈30 target is not met
> in the mixed case — see the note above the table), and the mixed prompt block
> shrank from 20.8 KB to 15.9 KB.

**Phase 4 — cleanup.**
Mode defaults become capability defaults, the mode selector and mode notices
disappear from the UI, docs and README move to capability wording, and `mode`
stays only as a legacy field read by the loader.
*Done when:* the word "mode" no longer shapes the agent, the UI or the docs, and
a fresh install has no switcher to click.

> **Status — done.** The UI, README and developer docs now speak of capabilities
> and panels: the composer carries the capability controls instead of a selector,
> notices say "Terminal panel opened…" / "capability detached", the journal
> banner and the session recap print `Panel:` and `Capabilities:`, and the Vision
> strings read "Vision" rather than "Vision mode". The dead
> `handoff_on_mode_switch` setting and its checkbox are gone (a capability change
> no longer starts a session, so there was nothing to seed).
>
> What deliberately keeps the word: `mode` in a session header is still read
> forever as the legacy field, and `mode_models` keeps its settings key so
> existing files stay valid — it is the per-capability default
> (`mode_models.vision`). `src/computer/modes/*` and `Computer(mode=…)` also keep
> the name: those are the delivery modules the capability registry filters, not a
> user-facing mode. Chat with base tools is **not** part of this phase; it is open
> question 2 below and would change behaviour.

## Acceptance checklist

Use this to decide whether the plan was achieved. Every line is something a
person can observe, not an internal detail.

1. **One loop.** "Run the dev server, open the login page, click through it and
   fix what breaks" completes in one session, with terminal and browser steps in
   one timeline, and no mode switch performed by the human.
2. **One journal.** That session's JSONL contains both shell and browser
   events, one objective and one summary; its header lists the capabilities.
3. **No switcher.** The main window has no mode selector; the capability
   controls are the entry point.
4. **Self-attach.** The model can attach a capability by calling `open_browser` /
   `open_terminal`; the transcript shows the attachment, and the capability's
   tools are usable on the next step.
5. **Self-repair loop.** Started from the console error or the network 500, the
   model edits the code, restarts the server and re-checks the page without the
   human copying anything between sessions.
6. **One Chromium.** While testing, the machine runs a single Chromium instance
   (one profile, one login state). Entering or leaving a panel does not spawn a
   second one.
7. **Clean stop.** Stop ends the turn and the command in flight, closes the
   live block as `Stopped`, and leaves no orphan process from *that command*.
   The dev server and Chromium are session resources: they survive a Stop
   (open question 4) and end on a project switch or application exit, when
   `tasklist` shows neither `chrome.exe` from our profile nor the
   `python -m http.server` we started.
8. **Restart fidelity.** Reopening the session restores the transcript, the
   capabilities and the summary; the browser is not resurrected, and the agent
   can navigate to the last URL from history.
9. **Old sessions.** Sessions created by 0.3.1 open, replay and continue with
   their capability mapped from `mode`.
10. **Approvals unchanged.** In `ask`, a state-changing shell command still
    prompts; a risky browser click still prompts; `browser_eval` still asks every
    time; sensitive paths are still refused. Attaching a capability asks for
    nothing extra.
11. **Prompt stability.** Within a turn the tool block and prompt prefix do not
    change; they change only when a capability is attached or revoked.
12. **Vision is fenced.** Vision cannot be attached with a non-CUA model, and no
    model switch happens silently.

> **Verification (2026-11-06, branch `feature/one-session-capabilities`).**
> Phases 0-4 are in the tree. Checked against the code and the journals of the
> `test` project:
>
> - **One journal, self-attach (2, 4) - proven by real runs.**
>   `session-20261105-160414-26bcb9` holds capabilities
>   `base+terminal+browser` with two `execute_shell` steps and a browser step in
>   one timeline (its first `open_browser` was denied, then succeeded);
>   `session-20261105-170804-a755b2` has two shell and four browser steps;
>   `session-20261105-160840-6cdb92` carries all five capabilities
>   (`base+terminal+browser+vision+chat`) in one header.
> - **No switcher (3).** No mode selector is left in `src/ui`.
> - **One Chromium (6).** `BrowserManager` is constructed in exactly one place,
>   `BrowserService.dispatch`, lazily on the first browser call.
> - **Restart fidelity (8).** `_restore_agent_history` rebuilds the model-facing
>   history from the journal, browser calls with their arguments and results
>   included, so the last URL stays in context; opening a session does not
>   launch Chromium.
> - **Old sessions (9).** `capabilities.grants_from_header` maps `mode` when the
>   header carries no `capabilities`, and `update_session_grants` writes the
>   field on the next append.
> - **Approvals (10).** Unchanged: terminal access mode, `classify_action`,
>   `browser_eval`'s `page_javascript` check, MCP trust, path guards.
> - **Prompt stability (11).** `ensure_session_profile` is signature-checked, so
>   the block changes on a grant change - or on a promoted catalog tool, which
>   is the second case Phase 3 documents - and not per message.
> - **Vision fenced (12).** `_vision_availability_error` refuses a non-CUA model;
>   the capability default model switch is announced, never silent.
>
> Confirmed in substance, not literally: **1** and **5** (the app has one loop
> and mixed journals exist, but no stored journal shows a full
> console-error -> edit -> restart -> re-check cycle; that is the acceptance
> scenario, not a code gap).
>
> **7 (clean stop) was reworded, not met as it stood.** The original line
> promised that Stop closes Chromium and the dev server, which contradicts open
> question 4 ("stop the turn, keep the server"). The code was also inconsistent
> with itself: the Browser branch of `handle_cancel_request` ran
> `TerminalHandler._cleanup_task`, which calls `shell_jobs.kill_all()`, while the
> Terminal branch only idled the handler - the same shared handler killed a dev
> server in one panel and kept it in the other, and neither closed the live
> block, cleared the plan pill or stopped a running command. Since 2026-11-06
> both panels go through `BaseAgentHandler.stop_turn`: it closes the block as
> `Stopped`, terminates the command in flight, journals that call as a
> `stopped` result, drops the actions queued behind it and keeps history, jobs
> and Chromium. The checklist line above now says exactly that; resources are
> released by a project switch and by `AppController.cleanup`.
>
> Two plan paragraphs are superseded rather than broken. **Prompts**: the build
> still picks one presentation's prompt (`Brain.get_terminal_prompts` /
> `get_browser_prompts`) and only the tool block follows the grants, so the
> "base + Terminal section + Browser section" concatenation was not built (the
> Phase 1 status note describes the shipped approach). **Grounding**: of
> "native / dom / local", native and DOM are implemented, while `local` is a
> placeholder setting with no recognizer behind it - the torch/OCR stack was
> dropped deliberately, and `system_architecture.md` says so.

## What stays from the previous vision

The grounding split (`native` / `dom` / `local`), the thin client, project-scoped
persistent sessions with JSONL journals, the rolling summary, durable
attachments, the MCP client, the approval philosophy and the "tell the computer,
get the result" scenario are still the plan. This document only replaces the
part that said "one session runs one mode".

## Non-goals

- **Tabs / parallel sessions.** One active session stays the rule.
- **`switch_mode` ping-pong.** The agent never hops between modes; capabilities
  attach in place.
- **Code Mode / JS sandbox.** A catalogue and `search_tools` are enough at our
  scale.
- **Porting or forking OpenCode.** The Qt/Python product and its own agent loop
  stay; only architectural ideas are borrowed.
- **Per-mode MCP servers or per-mode approval policies.** Capabilities own them,
  not modes.
- **Auto-granting Vision.** Computer Use needs a paid CUA provider and an
  explicit user choice.
- **Parallel Vision sessions / virtual displays.** Revisit only if parallel
  sessions ever become a goal.

## Open questions

1. **Revoke on panel close?** Recommendation: keep the grant until session end,
   revoke only by an explicit toggle, so a task can finish after the user hides a
   panel.
2. **Chat with base tools?** Recommendation: yes — read-only filesystem plus
   `open_terminal` / `open_browser`. It turns Chat into the entry point instead of
   a dead end, and removes the "Chat cannot start work" limitation.
3. **Native budget.** 30 tools or 35? Decide after Phase 3 measurement.
4. **Stop semantics. Decided 2026-11-06: stop the turn, keep the server.**
   Stop aborts the in-flight command and closes the block, but background jobs
   and Chromium stay until a project switch or exit. The per-job Stop links and
   `browser_close` cover the explicit cases.
5. **Vision placement.** A separate panel, or a toggle inside Terminal once a CUA
   model is selected? Recommendation: separate panel, because it changes the
   provider toolset.

## Backlog: browser tool surface (2026-11-06)

> Status: slice 1 is implemented (see **Done: slice 1** at the end of this
> section), and the defects its first live run exposed are fixed (see **Done:
> live-check fixes**); slice 2 and the on-request items are still proposed. Compared
> against the in-app browser tools of OpenCode desktop. That tool list is used
> as a checklist only: their Review pane, tab focus model and RPC file transfer
> are consequences of their architecture (the browser lives in the desktop app,
> the agent is a server), and porting or forking OpenCode stays a non-goal.

### Already covered

Our browser capability has 25 tools. The original 19: `goto`, `snapshot` (walks
every frame), `click` (ref or viewport coordinates), `fill`, `press`,
`select_option`, `hover`, `tabs` (list/new/select/close), `navigate_back`,
`wait_for` (time / text / text gone), `handle_dialog`, `find`, `screenshot`,
`read`, `console`, `network`, `frames`, `eval`, `close`. Slice 1 added `reload`,
`navigate_forward`, `scroll`, `check`, `uncheck` and `set_files`, and gave
`screenshot`/`eval` new arguments. These already match the bulk of the
OpenCode list, and in three places we are ahead: console and network keep a
bounded ring **plus** a session log file, so a noisy SPA does not flood the
context; coordinate clicks reach canvas and widgets that have no ref; a
snapshot covers cross-origin frames instead of requiring a frame id per call.

### Not applicable

`preview` and `files.get`/`files.upload` exist there because the agent process
and the desktop app do not share a filesystem, and tabs live in the app's Review
pane. Here the browser is in-process, images already render as transcript
tiles, and files open in the editor. Nothing to adopt.

### Candidates

Cost is for this repository: **S** 1-2 h, **M** about half a day, **L** 2-4 days,
each including prompt text, journal/replay and a test.

| Candidate | Why | Exposure | Cost |
|---|---|---|---|
| `screenshot` with `full_page` and `ref` | today only the viewport; checking a whole page or one section after an edit means scrolling | arguments of the existing tool, no new name | S |
| `reload`, `navigate_forward` | after `write_file` the dev server serves new code, but `goto` the same URL loses POST state and scroll; only `back` exists | catalog | S each |
| `scroll` (dx/dy) | virtualized lists and lazy images: a snapshot sees the DOM but does not trigger loading | catalog | S |
| `check` / `uncheck` by ref | a custom checkbox can toggle twice; setting the state is deterministic | catalog | S |
| `eval` on an element ref | removes selector guessing inside the script | argument of `browser_eval` | S |
| `set_files` on `<input type=file>` | the agent cannot test an upload UI today, only describe it | catalog | M |
| downloads: list + save | a download now disappears; "fetch the report and read it" is a normal task | catalog | M |
| response bodies (XHR/fetch, bounded) | a 200 with an empty screen is the typical SPA failure; status alone does not explain it | catalog | M |
| `drag` ref -> ref | kanban, sorting, range sliders; HTML5 drag needs care | catalog | M |
| `fill_form` (batch) | only saves tokens on long forms; batches already cover the step count | catalog | S |
| CPU profile / performance trace via CDP | only for performance work; the hard part is summarizing the raw profile, not collecting it | catalog | L each |
| heap compare / Lighthouse | heap needs an interactive UI to be actionable; Lighthouse pulls a Node package for a rare audit | - | not planned |

### Proposed slices

1. **See and act** (about 1.5-2 days): screenshot arguments, reload, forward,
   scroll, check, `set_files`. Closes "cannot see the whole page", "cannot see
   the new code", "cannot test an upload".
2. **Diagnose** (about 1.5 days): downloads, bounded response bodies, drag.
   Turns the browser from "look at a page" into "find out why it broke".
3. **Only on request**: CPU profile and trace, behind the catalog, when a real
   performance task appears.

### Constraints

- New names go to `CATALOG_TOOLS` (`src/computer/capabilities.py`) so they cost
  nothing in the prompt until `search_tools` loads them; anything expressible as
  an argument (`full_page`, `ref`) must not become a new name.
- Every new tool also needs a `BrowserManager` method, a `@tool_meta` entry in
  `src/computer/tools/browser/actions.py`, a `classify_action` branch in
  `src/browser/policy.py`, journal/replay handling and a test - that is most of
  the cost above.
- Response bodies must be bounded and limited to documents/XHR, stored in
  `src/browser/logs.py` beside the existing rings, or a large response hangs the
  step.
- Observed in `session-20261105-170804-a755b2`: the first `open_browser` after a
  cold launch timed out after 30 s (`navigating ... waiting until "commit"`) and
  an immediate retry worked. Fixed in slice 1: the first navigation of a fresh
  browser gets twice the timeout and one retry.

### Done: slice 1 (2026-11-09)

- `browser_screenshot` takes `full_page` and `ref`. Ref badges are drawn only on
  a plain viewport capture, because they are fixed-position overlays.
- New catalog tools: `browser_reload`, `browser_navigate_forward`,
  `browser_scroll` (`dx`/`dy`, or `ref` to scroll into view), `browser_check`,
  `browser_uncheck`, `browser_set_files` (`paths`); `browser_eval` takes `ref`
  and runs the script against that element. `browser_set_files` always asks for
  approval (`file_upload`), and `browser_check` shares the risky-action word
  list with `browser_click`.
- `browser_scroll` sends a real wheel event and reports the page position, so
  virtualised lists and lazy images load the way a user's scroll makes them.
- `check`/`uncheck` prefer the native `check()`/`uncheck()` and read the state
  back; a custom `role=checkbox` falls back to a click and is verified through
  `aria-checked`.
- The first navigation of a fresh browser gets twice the timeout and one retry,
  which closes the cold-start timeout recorded above.
- `src/browser/test_cli.py slice1 --headless` verifies the whole surface on a
  local page: full-page and element screenshots, wheel and into-view scrolling,
  reload, check/uncheck (native and `role=checkbox`), eval by ref, `set_files`,
  and back/forward.

### Done: live-check fixes (2026-11-09)

Found while driving the slice-1 surface through a real task
(`session-20261107-105225-03370f`), fixed before slice 2:

- **A. Portable tokens.** File tools went through `Computer._resolve_path`,
  which did not expand `{WORKDIR}` and friends, so `write_file` created a
  literal `{WORKDIR}` folder inside the project (`path_exists` then said it
  existed). Tokens now expand there and in `execute_shell`'s command line, not
  only its `cwd`.
- **B. Argument names.** The envelope accepts a free-form `arguments` object,
  so `stop_shell_job` was called with `description` and no `job_id` and died
  with an opaque Python `TypeError`. `src/computer/tool_args.py` validates the
  names against the real signature before the call (both `ToolExecWorker` and
  the Vision `Executor`) and reports the accepted arguments; `**kwargs` tools
  are exempt.
- **C. Snapshot rows.** Checkbox/radio/switch rows carry `(checked)` /
  `(unchecked)`, rendered by `BrowserManager.snapshot_line` (also used by
  `browser_find`), and a bare control takes the text after it before its
  parent's, so two checkboxes in one `<div>` no longer both read "checkbox 1".
- **D. Approval dialog.** A risky browser step asked through the old English
  Yes/No `QMessageBox` while Terminal used its own dialog.
  `AppController.ask_browser_approval` now feeds the check messages into the
  Terminal permission dialog (Deny default), so every approval looks the same.
- **E. `search_tools` journal.** The control tool ran invisibly: no
  `tool_call`, so it never appeared in the Activity tab or a restored
  transcript. It now journals a `tool_call`/`tool_result` pair and, on "no
  matches", names `open_browser` / `open_terminal` when the owning capability
  is not attached.
- **F. Deferred to slice 2.** `browser_screenshot` still draws ref badges on a
  plain viewport capture only and does not restrict `ref` to interactive
  elements; deciding what a screenshot should label is part of the slice-2
  "Diagnose" work.

