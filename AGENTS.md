# AGENTS.md

Notes for AI coding agents working in this repository.

## Commands

- Install / sync dependencies: `uv sync` (uses `pyproject.toml` + `uv.lock`; Python pinned to 3.12).
- Lint: `uv run ruff check <paths>` (config in `pyproject.toml`, line-length 500).
- Entry point: `main.py`. There is no automated test suite (no pytest); changes are verified manually.

## Packaging

Both builders are ephemeral tools, never dependencies: do not add PyInstaller or Inno Setup to `pyproject.toml` / `requirements.txt`.

- One-file executable: `uv run --with pyinstaller pyinstaller logos.spec --noconfirm --clean` -> `dist/Logos.exe` (about 6 minutes, ~204 MB).
- Installer: `"$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe" installer/logos.iss` -> `dist/LogosSetup.exe`. Per-user only (`PrivilegesRequired=lowest`, default `%LOCALAPPDATA%\Programs\Logos`), user picks the folder, Start Menu shortcut, HKCU uninstall entry for Settings > Installed apps, and the uninstaller deletes the whole data folder below. Its optional Tasks (all checked by default) carry the Windows integration the source setup script does: `logos.cmd` in `{app}` plus an append to the user PATH (`ChangesEnvironment`, removed on uninstall), the HKCU `Directory\shell\Logos` "Open with Logos" folder menu, and the desktop shortcut. Because the installer owns these, a frozen build skips step 2 of `src/initial_setup.py`; source setups keep it because `setup_windows.ps1` reads `setup_choices.json`.

`build/` and `dist/` are packaging artifacts and git-ignored. A frozen build keeps settings, logs, cache and the materialized `prompts/` + `assets/` in `%LOCALAPPDATA%\Logos` (see `src/app_paths.py`), because the one-file unpack folder is temporary and the install folder may be read-only. Compilation success alone is not proof: launch the built exe and, for installer changes, run install -> launch -> uninstall.

## Dependency manifests — keep in sync (DO NOT FORGET)

There are two dependency manifests, and they are **manually kept in sync**:

1. `pyproject.toml` — canonical source. `setup_windows.ps1` / `setup_ubuntu.sh` install from it via `uv sync`.
2. `requirements.txt` — legacy/manual mirror. Nothing in the setup flow reads it, so it drifts silently.

Whenever a package is added/removed/changed, update **both** files. `requirements.txt`
tracks the same set as `pyproject.toml` (plus a short section header comment).

## Abandoned screen-recognition stack

Screen understanding is done by the LLM (Computer Use / vision providers), not by an
embedded local ML model. The old torch/OCR/YOLO stack was dropped from the manifests
(commit `33babd8`). Do **not** re-add torch/torchvision/ultralytics/supervision/opencv
etc. as dependencies.

## Active project

The app runs inside a selected project directory. Startup resolves the remembered
`active_project` from `settings.json`; if it is missing or gone, the UI opens the
full-screen `HomePage` (`src/ui/navigation.py`) instead of a modal picker. The
installation folder and the application data folder are never projects: an empty
or app-owned remembered path is treated as missing, so a fresh install starts on
the home screen. In the
workspace, projects and sessions are reached through the collapsible left sidebar;
its footer keeps only Home, New session and Settings. The session context menu
(shared by the sidebar and the home screen) carries Rename, Show journal, Show in
Explorer and Delete Session; Show journal opens the raw event journal of that
session, whether or not it is the open one.
The full-mode window frame (`window_geometry`) and the sidebar's collapsed state
(`sidebar_collapsed`) are saved to `settings.json` on close and restored on the
next launch; layout margins are not persisted. The right workspace panel
(`src/ui/right_panel.py`) is visible in every mode. Its Session tab leads with
context statistics (provider, model, context window and usage, token totals,
message/tool-call counts, created and last-activity timestamps) fed from the
same data as the composer indicator, followed by a recap derived from this
session's own journal: the objective plus bullet lines for assistant messages
and skills loaded through `use_skill` (last six, clipped). The rolling handoff
summary is not shown in the panel; it stays model/compaction context. The
Activity tab lists skills loaded through `use_skill`, named tools invoked with
counts (control tools are hidden), and the files or screenshots attached in
this session (clicking one reveals a folder, previews an image or opens the
file in the detected editor). Money is never computed. The Files tab (read-only
tree of the active project; repository and cache folders are hidden; files open
in the detected editor - VS Code, Cursor or Windsurf, else the OS default) is
available in every mode, while the Git tab appears only in Terminal mode. The
Git tab is read-only (`src/git_status.py`, stdlib `git rev-parse`/`git status
--porcelain=v2`/`git diff --numstat` only): it shows the checked-out branch
(or detached HEAD), upstream with ahead/behind counts, and one flat list of
uncommitted files with status letters plus added/deleted line counts
(untracked files are counted; binaries and untracked folders show none). The
tab follows agent-panel conventions (Cursor, Claude Code, OpenCode): working
tree only, no commit history, staging or commit action. Clicking a file row
opens it in the detected editor; deleted files stay disabled. Logos' own
`.logos` data is hidden. The status is read in a background thread and
refreshes when the tab opens, when the project
or mode changes, on the refresh button, and after session events while the tab
is visible. The panel's collapsed state is saved as
`right_panel_collapsed`.
Context fill is visible in three places backed by one snapshot: the composer
pill next to Send (`ContextIndicator`), the right panel header ring with its
percent (in the collapsed rail, under the chevron), and a popover opened by
clicking either. `src/ui/context_popover.py` holds `ContextGauge` (ring),
`SegmentedBar` and `ContextPopover`; the popover is a `Qt.Popup` anchored above
its trigger and clamped to the screen. It shows the percentage,
`used / window tokens · source`, a segmented bar and the rows `System prompt`,
`System tools`, `Skills (N)`, `Messages`, `Other`, plus `Compress context`
(emits `Manager.context_compact_requested`, handled by the same manual
compaction as Compact now) and `Session context…` (opens the existing dialog).
Row shares are estimates from the next request scaled to the provider-reported
total (`context_stats.payload_breakdown`), so they add up to the shown
percentage; Vision, whose request is assembled elsewhere, gets one
status-colored share and a note instead of rows. Skills are counted separately:
`BaseAgentHandler.context_payload` passes `skills_text` and `skills_count`
(`skills.prompt_block_count`), and `model_context.payload_token_estimate`
subtracts that block from `system` so the components stay disjoint. The panel
receives `context_tokens` / `context_source` / `context_status` from
`AppController.session_panel_payload`; a gauge click emits
`RightPanel.context_clicked` with itself as the anchor.
Clicking a recent session opens
its project and that exact session. Per-project data lives in `<project>/.logos/`:

- `project.json` - id, name, timestamps. `name` may be a custom display name set
  from the project context menu; the folder itself is never renamed.
- `sessions/` - one append-only JSONL file per session; the first line is a
  `session` header (`schema_version`, `id`, `mode`, optional `title`, optional
  `objective`) and later
  lines are typed events (`message`, `reasoning`, `progress`, `plan`,
  `tool_call`, `tool_result`, `job_result`, `usage`, `compaction`, `error`,
  `suggestions`). `reasoning` events carry the
  model's stated
  rationale and, for agent steps, the provider's thought summary when the API
  exposes one; `progress` events carry the model's short narration line and
  `plan` events the current plan (`steps`, each with `text` and `status`) from
  the `update_plan` control tool. A plan is live progress, not history: it
  drives the composer's `Step N / M` pill while the turn runs and is never
  redrawn into a restored transcript, while thoughts and notes rebuild inside
  their activity blocks. `tool_call` events carry `call_id`, `name`
  and the model-facing `input`; `tool_result` events carry `call_id`, `name`,
  `status` (`ok`/`error`/`denied`), `output.text` and, when the UI showed more
  than the model needed, `display` (transcript text), `display_title` (the
  live row title) and `log_path`; a background launch also records `job_id`,
  which the later `job_result` (`job_id`, `name`, `status`, `exit_code`, the
  same `display*`/`log_path`) updates in place. `message` events may carry an
  `attachments`
  list (name, kind, path, size, SHA-256, stored flag/status). `usage` events
  carry normalized provider token
  counts
  (input/output/reasoning/cache/total); they are counted in context statistics
  but never replayed as chat text. No prices are stored or computed. Credentials
  are redacted before events are written. A `compaction` event records each
  applied boundary (`trigger` auto/manual, `covered`, `total`,
  `summary_source`, provider, model) and is replayed as an italic notice, so a
  reloaded transcript shows why older history disappeared; the right panel's
  Session tab reads the latest one (falling back to the summary meta for
  sessions compacted before this event existed) and shows a COMPACTION line.
  The newest is
  restored at startup: messages are replayed into the
  chat, and terminal/browser `command_history` plus the whole transcript are
  rebuilt from the same events. Terminal and Browser journals replay into the same activity
  blocks the turn used live (`BaseAgentHandler.replay_event`): `reasoning` /
  `progress` become thought and note rows, `plan` events are skipped (the
  plan only exists live), `tool_call` opens a step, `tool_result` fills its title from
  `display_title`, output from `display` (falling back to the model-facing
  `output.text`) and expands errors and denials, and `job_result` updates the
  launch step. A `write_file` step rebuilds its code box from the recorded
  `content`, and `replace` / `replace_many` steps rebuild the live diff block
  from the recorded strings; since the edit already happened, line numbers
  are anchored on the new text (or omitted when it is no longer in the file).
  Journals written before those fields existed still group with
  the old grammar (step title from the action, output stripped of its
  `**Tool Result:**` markers). Vision and Chat keep the per-event card grammar
  (`AppController._replay_tool_event`). Replayed steps carry no elapsed time,
  so a restored transcript does not show "0s" on every row.
  An activity block is one turn card whose rows are rendered by
  `src/ui/widgets/activity_block_formatter.py`. Consecutive steps of the same
  category collapse into one run row: read-only exploration (`read_files`,
  `list_directory`, `search_file_content`, `web_search`, `web_fetchs`,
  `mcp_list`, `mcp_get`) groups together and every other tool is an `action`
  group. A thought or narration row ends a run, so the model's thinking stays
  visible between groups instead of being hidden inside a folded row; the
  narration before a run's first step stays visible as its commentary.
  A single-step run stays a plain step row. Runs are open while a step is
  running and when one failed or was denied, and fold to
  `Ran N tools · <tool breakdown>` once finished clean; the row toggles via an
  `activity:<message id>:run:<first step id>` anchor, with the override kept
  in `msg['run_state']`. The block header is only the caret and the title
  (`Working` while live, then the outcome): there is no overall turn timer and
  no status-colored card edge, and the outcome never derives from step results:
  replay closes every block as `Done`, because a failed step is not a failed
  turn (the model often retries and succeeds); the red ✗ and expanded output
  stay on the step row alone. When the turn ends with `finish`, the block
  folds to its header (`collapse="auto"` in `ChatWidget.finish_activity`):
  the answer stays open below, the work does not. Auto-fold is skipped when the
  user toggled the block by hand during the turn (`msg['user_toggled']`), when
  a step failed or was denied with no successful step after it
  (`_has_unresolved_failure`), or while a background job is still running;
  `Request failed`, `Stopped` and `Waiting for you` keep their block open.
  Replay always folds (`collapse="always"`), so a restored session shows all
  work collapsed. There is no green `✓ Done` line under the answer.
  The plan lives outside the transcript: a live turn
  shows a compact `Step N / M` pill above the composer
  (`src/ui/widgets/plan_panel.py::PlanPanel`, mounted first in
  `UIManager.bottom_center_column`), hovering it opens a themed checklist
  popover, and the pill disappears when the turn ends
  (`BaseAgentHandler._activity_finalize`). A mid-turn user message leaves it
  alone; a mode switch and `clear_chat` clear it, compact mode only hides it
  (`set_suppressed`). The journal keeps `plan` events for the model and the
  summary, but replay never shows them.
  Terminal and Browser request one `payload.actions` list per response: up to
  eight independent calls that `BaseAgentHandler._start_batch` runs in order
  without asking the model in between, so a run is normally exactly one
  response's batch. A later action never sees an earlier action's output, so
  the prompts limit batching to independent work; `finish` and
  `communicate_to_user` end the batch and drop whatever is queued. An error, a
  denial, the duplicate guard or a duplicate-response hit aborts the rest of
  the batch and hands control back to the model; a background-job launch keeps
  the batch waiting until the job ends (or the wait budget runs out), and a
  mid-task user steer or a pause drops the remaining queued actions. The
  single `action` shape of older caches and models is still accepted by
  `src/llm/llm_parsing.py`, which folds it into `actions`. A user message
  mid-turn seals the live block: `ChatWidget.add_message` with
  `role='user'` calls `seal_live_activity()`, which finishes the block as
  `Done` (`Stopped` when a step was still in flight) and marks that step
  `stopped`, and the handler's next event opens a new block below the message
  (`_activity_alive` rejects a finished block). A
  late result still lands in the sealed block that owns its step
  (`BaseAgentHandler._active_step_block`). Replay splits the same way because
  `_replay_session` closes the replayed block at every `message` event. After
  a manual or automatic compaction only events after the
  recorded boundary are rebuilt; the summary block stands in for the compacted
  prefix. Automatic compaction runs at idle when the context reaches
  `context.auto_compact_tokens` (default 200k, keeps requests cheap on 1M+
  windows) or free space drops to `max(15% of the window, 16k tokens)`,
  whichever comes first; disabled by `context.auto_compact`. The background
  summary refresh fires when uncovered events exceed
  `context.summary_refresh_tokens` (default 30k), when a completion newer than
  the summary crosses `context.summary_threshold_ratio` of the window, or right
  before a session is left (`src/controller.py::_refresh_leaving_summary`). A
  completion the summary already covers describes the pre-compaction context,
  so it never counts toward the window fraction. A capture
  never overwrites a model-written summary with the deterministic fallback.
  Agent `finish` / `communicate_to_user` / `display_message`
  text is persisted as an assistant `message`. `objective` is auto-derived from
  the first request; Browser/Vision resumes get a deterministic session summary
  derived from this journal.
- `sessions/<name>.summary.md` and `<name>.summary.json` - rolling summary of
  one session with fixed sections (`Objective`, `Done`, `Decisions`,
  `Constraints`, `Checks`, `Current`, `Next`, `Unfinished`, `Files`,
  `Blockers`) and provenance (source session, event count, model,
  optional `handoff_from` / `handoff_consumed` and the compaction boundary
  `compacted_through` / `compacted_event_count` / `compacted_at`). Summaries
  never cross session
  boundaries: switching
  mode copies the previous summary once into the new session. The deterministic
  fallback is always available (its `Current` comes from the last assistant
  message, else the latest request - never raw tool output); the LLM refresh
  runs in the worker thread and
  is injected as chat system context, terminal/browser history, or the first
  task of a fresh Vision/CUA chain when continuing from another session/mode.
  The Session context dialog states the summary source (LLM with provider/model,
  fallback, or handoff), its event coverage and whether a refresh is due, running
  or paused.
  Deleting a session removes its summary sidecars. Sidecars are not listed as
  sessions. The legacy project-level `.logos/context/summary.md` is no longer
  read.
- `<project>/AGENTS.md` - optional stable project instructions read from the
  project root only. Capped at 32 KB, redacted and cached by mtime; injected as
  a separate block in every mode before the session handoff. The application
  never generates or rewrites it.
- `attachments/<session-id>/` - copies of pasted images (and other bytes with no
  stable source path), deduplicated by SHA-256. Files with a real path are
  referenced in the journal instead of copied; replay marks referenced files
  `changed` or `missing`. Deleting a session removes its attachment folder.

Attachment contents are not re-sent on every agent step. Terminal and Browser
receive a reference block (name, kind, size, path, status) and read the file on
demand with `read_files` / `list_directory` / `search_file_content` (Browser
includes those read-only filesystem tools too). `read_files` takes an optional
1-based `offset` and a `limit` per file: the range comes back numbered, under a
`--- START OF FILE: <path> (lines a-b of N) ---` header, so large files are read
in slices instead of whole; without them the output is the unchanged full file.
Chat has no tool loop, so text files are inlined once in the current request
and only referenced afterwards.
The Session journal shows the recorded path, size, SHA-256, stored flag and live
status under the message that carried each attachment.

Agent Skills use the open `SKILL.md` format: a folder with a `SKILL.md` file
whose YAML frontmatter carries `name` and `description` (optional `scripts/`,
`references/`, `assets/`). `src/context/skills.py` scans, for the active project
and then the user home: `.logos/skills`, `.agents/skills`, `.cursor/skills`,
`.claude/skills`, `.codex/skills`, `.qoder/skills`, and finally the
application's bundled `skills/` folder (packaged through `logos.spec`). The
first root wins on a name clash, so project skills override user skills and
both override a built-in with the same name. Terminal and Browser list only
name + description in the system prompt (`### Available Skills ###`); the model
calls the `use_skill` tool to load the body, the skill directory and auxiliary
file paths, then reads extras with `read_files`. Chat and Vision have no skills.
The Skills settings page lists installed skills, opens the project
(`<project>/.logos/skills`) and user (`~/.logos/skills`) roots, and opens the
ClawHub catalogue dialog. `src/context/skill_catalog.py` is a stdlib-only,
keyless client (search, popular list, ZIP install); the dialog
`src/ui/skill_catalog_dialog.py` installs into `~/.logos/skills` after showing
the author, license and source URL.

Sessions are mode-locked: `chat`, `terminal`, `browser`, or `vision`. Switching
mode in a non-empty session starts a new session; switching in an empty session
rewrites only the header. Deleting the open session clears the chat and leaves no
journal behind; the next message creates a session in the current mode. Legacy
`session-*.json` files are migrated to JSONL on load and the old file is kept as
`.json.bak`.

`src/project_manager.py` owns this layout. The project root (not the Logos install
directory) is the terminal/browser `working_directory` and the root passed to
`Indexer`; `.logos` is ignored by the indexer. Do not hardcode `Indexer('.')` or
`os.getcwd()` for project-scoped paths.

## Settings

Settings is a full-window page, not a dialog: `src/ui/settings_window.py::SettingsPage`
is built lazily by `UIManager.show_settings_page` on first open (the module is
imported there) and mounted in the main layout in place of the home screen or
workspace; `hide_settings_page` restores the workspace, or the home screen when
no project is active, and compact mode switches to full mode first. The fixed
248 px left rail (`QWidget#SettingsNav`) holds a Back item ("Back to application"),
labelled icon buttons grouped by `NAV_GROUPS` with separator lines, and About
pinned at the bottom. Modes list in the mode-selector order (Terminal, Browser,
Vision last). The rail and the content travel as one block capped at
`BODY_MAX_WIDTH` (1184 px) and centered, so the rail stays next to the settings
instead of sitting in the window corner. Each section sits in a column capped
at `CONTENT_MAX_WIDTH` (880 px), so settings stay compact on wide windows
instead of stretching to the edges. There is no OK/Cancel and no
separate close button: Back (or Esc) is the way out. The Vision section combines
the Vision agent-loop tunables, the compact-window geometry/position and the
allowed Applications list; the About page keeps its natural height instead of
filling the viewport.

Every control is wired in `_wire_autosave` to a 400 ms debounce
(`_save_timer` -> `_apply_changes`). A save compares `_collect_settings_from_ui()`
with the `_saved_state` snapshot taken on load, so nothing is written when the
page is only opened; a real change goes through `settings_manager.set_many`
(save + emit, so the theme, hotkeys and controllers update live) and then
`providers_page.commit()`, which prunes keychain entries of deleted custom
providers. `ProvidersPage` stages its own edits in memory
(`set_many(save=False, emit=False)`) and announces them with `changed`, which
the page also listens to; other sections write their own keys directly. Leaving
the page by any path (Back, Esc, app close, any hide) runs `flush_changes()`
and `mcp_page.commit()` so pending edits and removed MCP secrets are not lost.

## Providers and the Vision model

Settings → Providers (`src/ui/widgets/providers_page.py`) is the single place
where providers are connected, activated and configured. Built-in rows open the
schema-driven `ProviderDialog` (API key + model); custom OpenAI- or
Anthropic-compatible endpoints open `custom_provider_dialog.py`. That dialog's
**API format** selector writes `custom_providers[i]["protocol"]` (`openai`
default, or `anthropic`); `src/llm/custom_providers.py` builds a
`CustomOpenAIProvider` or `CustomAnthropicProvider` subclass accordingly. The
Anthropic variant speaks Messages (`{base}/v1/messages`, `anthropic-version`,
content blocks, forced `tool_choice`) but deliberately omits native Anthropic
extras (Computer Use, web search, strict schemas, adaptive thinking). Custom
providers stay out of Vision either way.

The page's **VISION (COMPUTER USE)** row chooses the default Vision model: a
provider combo (configured OpenAI/Anthropic/Google providers with models where
`ModeCapabilities.supports(pid, model, "vision")`) plus an editable model combo.
The choice is stored as `mode_models.vision`; the page stages it in memory like
its other edits and emits `changed`, so the settings page autosaves it.
Settings → Vision only holds agent-loop tunables and points to this row. Provider tiles use bundled brand SVGs from
`assets/providers/` (MIT, LobeHub icons; `logos.spec` packages `assets/`),
tinted per provider, with initials as the fallback for custom providers.
Lazy discovery (`src/llm/__init__.py`) reads real `get_display_name()` literals
from class staticmethods via `ast.walk`, so names like "OpenAI", "Hugging Face"
and "xAI" appear without importing any SDK.

## Suggested next actions

Terminal and Browser answers can carry up to three clickable next-step
suggestions. The model passes them as the optional `suggestions` list on
`finish` / `communicate_to_user` (`src/computer/tools/common/`); Vision cannot
call those tools, so its final report ends with a `SUGGESTIONS: a | b | c`
line instead (`src/suggestions.py::extract_suggestions`, stripped before
display). `src/handlers/base_agent.py::_publish_suggestions` normalizes them
(dedupe, 80 characters, max 3), renders the chip row through
`UIManager.add_suggestions` and journals a `suggestions` event, which
`AppController._replay_session` renders on restore. Chips are plain anchors
(`suggest:<id>:<index>`); a click marks the row consumed and emits
`ChatWidget.suggestion_selected`, wired to `user_command_submitted`, so the
text is sent exactly like a typed message. Vision done text is not journaled,
so those chips do not survive replay. Chat has no suggestions.

## Terminal access permissions

Every Terminal tool call passes `src/computer/terminal_policy.py` before it
runs (`TerminalHandler.execute_action` calls `AppController.ask_terminal_approval`
first, including for `web_fetchs` / `web_search`). `terminal.access_mode`
(Settings → Terminal, "Access permissions") has three values:

- `ask` (default) - read-only shell commands and edits inside the project run
  without a prompt; state-changing commands, network tools, file deletes,
  sensitive paths and paths outside the project ask.
- `auto` - only risk asks: destructive commands (recursive/forced deletion,
  disk/registry/service/power changes, privilege escalation, destructive git,
  downloaded-code execution), file deletes, sensitive paths and paths outside
  the project.
- `full` - never asks.

Read-only detection is deliberately conservative: every pipe segment must be
an allowlisted command head (`ls`, `git status`, package-manager list/show
commands, `--version` flags, PowerShell `Get-*`/`Format-*` output cmdlets, ...);
redirection, substitution, `rm`/`del`, scripts and unknown executables make a
command state-changing. Sensitive paths include `.env` files (templates such
as `.env.example` excluded), private keys and `.ssh`/`.aws`/`.gnupg`/`.kube`
folders. A denied action is journaled as a `tool_result` with status `denied`,
rendered as an expanded warning card and reported to the model as `Denied: ...`
so it must not retry silently; the dialog defaults to Deny and fails closed.
The mode applies to Terminal only; Browser and Vision keep their own approval
flows.

The current mode is also mirrored in the Terminal composer as a chip
(`AccessModeChip` in `src/ui/manager.py`) with the mode icon and the label from
`ACCESS_MODE_LABELS`; `full` is painted in the error color. Clicking it opens
`src/ui/widgets/terminal_access_popup.py::TerminalAccessPopup`, a `Qt.Popup`
anchored above the chip and clamped to the screen: three clickable rows, each
with icon, title and a one-line explanation, and a check on the active one.
Choosing a row calls `UIManager._set_access_mode`, which writes
`terminal.access_mode` with `set_many` (save + emit), so the chip refreshes
through `on_settings_changed` and Settings → Terminal shows the same value.
The chip is hidden outside Terminal mode and participates in both toolbar
layouts (`_reflow_toolbar`).

## MCP servers

Terminal mode reaches user-configured MCP servers through three meta-tools
(`src/computer/tools/terminal/mcp_tools.py`): `mcp_list` (servers, states and
their tools), `mcp_get` (one tool's definition and input schema) and `mcp_call`
(executes it; `arguments` is a JSON object string because the strict tool schema
cannot express a free-form object). Only `${...}` references are expanded;
known names in single braces such as `{userHome}` are rejected at definition
time (`src/mcp_client/config.py::_interpolate`) because the server would
otherwise receive a literal relative path. The system prompt also carries a live
`{available_mcp_servers}` block built by `servers_prompt_block` and passed from
`TerminalHandler.build_prompts`: one line per configured server with transport,
state, tool count and a one-line description (catalog text when name and
transport match `src/mcp_client/catalog.py`, otherwise the first tool names),
capped at 12 servers, so the model recognizes servers the user names without a
discovery call. Definitions come from `mcp.servers` in
`settings.json` plus the active project's `.mcp.json`; project entries stay
stopped (`needs_trust`) until their names are listed in
`mcp.trusted_projects[<project root>]`. `src/mcp_client/` merges configuration
(`load_servers`) and runs one `MCPManager` with a background asyncio loop and a
persistent client per server. `AppController` creates the manager lazily when
Terminal mode starts or is restored, attaches it to the Terminal `Computer`,
refreshes it on project switch and stops it in `cleanup()`. `mcp_list` /
`mcp_get` are read-only for the access policy; `mcp_call` runs silently only for
a tool the server marks read-only on a trusted server and otherwise asks in both
`ask` and `auto` (`full` never asks). `mcp_call` output is capped at 20k
characters; the full text is spilled to `<project>/.logos/mcp_outputs/` (one
`.txt` per call, 5 MB each, newest 50 kept) and the truncation notice carries
the path so the model can read it with `read_files`. A tool-reported error is
raised as a tool error so it gets the error card and `error` status in the
journal.

`MCPManager` bounds the first connection and tool discovery with
`connect_timeout` (default 30 s) and `timeout` (default 60 s); a call uses the
same tool timeout and fails with a timeout error. Three consecutive transport
failures or timeouts trip the circuit breaker: the supervisor stops, the status
becomes `error` with the cause, and later calls fail fast with a hint to
Restart; `restart()` and a configuration refresh reset the counter. A
tool-reported error result is not a breaker failure. `refresh()` reconciles the
configuration into the running manager: changed servers restart, removed servers
are stopped and dropped from the status map (so `mcp_list` no longer shows
them), unchanged servers keep their clients. The right panel's Activity tab
groups MCP calls as `mcp_call · <server>/<tool>`
(`AppController._session_tool_records`). The end-user guide is `docs/mcp.md`.

Settings → MCP Servers (`src/ui/mcp_settings_page.py`) manages those servers.
The page lists user servers from `settings.json` and project servers from the
active `.mcp.json` in two groups, with transport, live state (stopped,
starting, connected with tool count, error, disabled, needs trust), tooltips
and a 700 ms status poll while visible. User rows carry an enable checkbox
(`enabled` in the definition). A project server with the same name overrides
the user server, marked "Overridden by project". Buttons: Browse Catalog
(one-click catalog, see below), Add Server and Edit
(`src/ui/mcp_server_dialog.py`, stdio command/args/cwd/env or HTTP
url/headers, timeouts, validated without expanding `${...}` references),
Remove, Import (a `.mcp.json` or Claude Desktop `mcpServers` document),
Restart, Test (restart plus connected/failed feedback), Logs (server stderr and
failures from `MCPManager.logs`), Trust Server / Revoke Trust for project rows
behind a warning that trusted servers start with Terminal mode and run code,
Open `.mcp.json`, and Reload (controller refresh plus repopulate).
`src/ui/mcp_catalog_dialog.py` (opened by Browse Catalog) installs a bundled
curated entry from `src/mcp_client/catalog.py` (memory, filesystem, sqlite,
git, fetch, time, sequential-thinking; remote DeepWiki, Microsoft Learn,
Context7, GitHub) through the same save path: the definition template is
filled from folder/file/secret parameter values, `npx`/`uvx` availability is
checked with `shutil.which`, and `~/.logos/mcp` is created for default paths;
Add Server and Import stay for custom definitions. Every
add/edit/remove/trust/enable change is written to `settings.json` immediately
(`save=True, emit=False`) and the live manager is refreshed, so Restart/Test/
Logs act on the saved definition and closing the app keeps it. There is no
Cancel: leaving the settings page (Back or Esc) or closing the app calls
`commit()`, which prunes keychain entries of removed servers and fields from
the snapshot taken when the page first changed. A connection failure stores the
unwrapped task-group cause in
`ServerStatus.error`, and child stderr is decoded as UTF-8 with a Windows
OEM code page (cp866) fallback before it reaches `MCPManager.logs`. The page
is imported by the settings page module directly, which `UIManager` itself
imports on first open; the server dialog module is
imported on first Add/Edit/Import and the catalog dialog on first Browse
Catalog.

MCP env and header values whose names look like credentials (`TOKEN`, `KEY`,
`SECRET`, `PASSWORD`, `AUTH`, `COOKIE`, ...) are kept in the OS keychain
instead of `settings.json`. `src/context/secrets.py` owns the helpers
(`sanitize_mcp_servers`, `hydrate_mcp_servers`, `prune_mcp_secrets`,
placeholders `${keyring:mcp:<server>:<kind>:<name>}` with kind `env` or
`header`, service `Logos-ai-assistant`); `SettingsManager` hydrates placeholders
on load and writes sanitized copies, so the in-memory settings stay usable and
the plaintext never reaches `settings.json`, even when the keychain is
unavailable (the placeholder is written and the failure logged). The page's
`commit()` prunes keychain entries for removed servers or fields. The helpers live in `context.secrets` (already imported at startup)
instead of `src.mcp_client`, whose import costs over a second.
