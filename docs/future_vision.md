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

> **Status — 4a done.** The UI, README and developer docs now speak of
> capabilities and panels: the composer carries the capability controls instead
> of a selector, notices say "Terminal panel opened…" / "capability detached",
> the journal banner and the session recap print `Panel:` and `Capabilities:`,
> and the Vision strings read "Vision" rather than "Vision mode". The dead
> `handoff_on_mode_switch` setting and its checkbox are gone (a capability change
> no longer starts a session, so there was nothing to seed). `mode_models` keeps
> its key name for compatibility and is documented as the per-capability default
> (`mode_models.vision`); `mode` in a session header stays readable forever.
>
> **Still open — 4b.** Chat is not yet a base-only agent session: `ChatHandler`
> has no tool loop, so `base` tools (file reads, `open_terminal` /
> `open_browser`) are unreachable from the Chat panel. That is the last item of
> the plan and the one that changes behaviour.

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
7. **Clean stop.** Stop closes Chromium and the dev server; no orphan processes
   remain (`tasklist` shows neither `chrome.exe` from our profile nor the
   `python -m http.server`).
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
4. **Stop semantics.** Keep "stop everything" (today) or "stop the turn, keep the
   server"? Recommendation: keep today's behavior in Phase 1, revisit with real
   usage data.
5. **Vision placement.** A separate panel, or a toggle inside Terminal once a CUA
   model is selected? Recommendation: separate panel, because it changes the
   provider toolset.
