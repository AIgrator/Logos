# AGENTS.md

Notes for AI coding agents working in this repository.

## Commands

- Install / sync dependencies: `uv sync` (uses `pyproject.toml` + `uv.lock`; Python pinned to 3.12).
- Lint: `uv run ruff check <paths>` (config in `pyproject.toml`, line-length 500).
- Entry point: `main.py`. There is no automated test suite (no pytest); changes are verified manually.

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
full-screen `HomePage` (`src/ui/navigation.py`) instead of a modal picker. In the
workspace, projects and sessions are reached through the collapsible left sidebar.
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
Clicking a recent session opens
its project and that exact session. Per-project data lives in `<project>/.logos/`:

- `project.json` - id, name, timestamps. `name` may be a custom display name set
  from the project context menu; the folder itself is never renamed.
- `sessions/` - one append-only JSONL file per session; the first line is a
  `session` header (`schema_version`, `id`, `mode`, optional `title`, optional
  `objective`) and later
  lines are typed events (`message`, `reasoning`, `tool_call`, `tool_result`,
  `job_result`, `usage`, `error`). `reasoning` events carry the model's stated
  rationale and, for agent steps, the provider's thought summary when the API
  exposes one. `message` events may carry an `attachments`
  list (name, kind, path, size, SHA-256, stored flag/status). `usage` events
  carry normalized provider token
  counts
  (input/output/reasoning/cache/total); they are counted in context statistics
  but never replayed as chat text. No prices are stored or computed. Credentials
  are redacted before events are written. The newest is
  restored at startup: messages are replayed into the
  chat, and terminal/browser `command_history` plus tool cards are rebuilt from
  the same events. After a manual or automatic compaction only events after the
  recorded boundary are rebuilt; the summary block stands in for the compacted
  prefix (automatic compaction runs at idle when free context space drops to
  `max(15% of the window, 16k tokens)`; disabled by `context.auto_compact`).
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
  fallback is always available; the LLM refresh runs in the worker thread and
  is injected as chat system context, terminal/browser history, or the first
  task of a fresh Vision/CUA chain when continuing from another session/mode.
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
includes those read-only filesystem tools too). Chat has no tool loop, so text
files are inlined once in the current request and only referenced afterwards.
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
Logs act on the saved definition and closing the window or the app keeps it.
Cancel and Esc restore the snapshot taken when the page first changed; the
window's close event commits instead (secrets of removed servers are pruned
by `commit()`). A connection failure stores the unwrapped task-group cause in
`ServerStatus.error`, and child stderr is decoded as UTF-8 with a Windows
OEM code page (cp866) fallback before it reaches `MCPManager.logs`. The page
is imported by the settings window directly; the server dialog module is
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
unavailable (the placeholder is written and the failure logged). Accepting the
page's `commit()` prunes keychain entries for removed servers or fields. The helpers live in `context.secrets` (already imported at startup)
instead of `src.mcp_client`, whose import costs over a second.
