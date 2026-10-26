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
next launch; layout margins are not persisted. Clicking a recent session opens
its project and that exact session. Per-project data lives in `<project>/.logos/`:

- `project.json` - id, name, timestamps. `name` may be a custom display name set
  from the project context menu; the folder itself is never renamed.
- `sessions/` - one append-only JSONL file per session; the first line is a
  `session` header (`schema_version`, `id`, `mode`, optional `title`, optional
  `objective`) and later
  lines are typed events (`message`, `reasoning`, `tool_call`, `tool_result`,
  `job_result`, `usage`, `error`). `message` events may carry an `attachments`
  list (name, kind, path, size, SHA-256, stored flag/status). `usage` events
  carry normalized provider token
  counts
  (input/output/reasoning/cache/total); they are counted in context statistics
  but never replayed as chat text. No prices are stored or computed. Credentials
  are redacted before events are written. The newest is
  restored at startup: messages are replayed into the
  chat, and terminal/browser `command_history` plus tool cards are rebuilt from
  the same events. Agent `finish` / `communicate_to_user` / `display_message`
  text is persisted as an assistant `message`. `objective` is auto-derived from
  the first request; Browser/Vision resumes get a deterministic session summary
  derived from this journal.
- `context/summary.md` - rolling project handoff summary with fixed sections
  (`Objective`, `Done`, `Current`, `Next`, `Files`, `Blockers`),
  `context/summary.json` - its source session/event count/model metadata.
  The deterministic fallback is always available; the LLM refresh runs in the
  worker thread and is injected as chat system context, the first
  terminal/browser history entry, or the first task of a fresh Vision/CUA chain
  when continuing in a different session/mode.
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

Sessions are mode-locked: `chat`, `terminal`, `browser`, or `vision`. Switching
mode in a non-empty session starts a new session; switching in an empty session
rewrites only the header. Legacy `session-*.json` files are migrated to JSONL on
load and the old file is kept as `.json.bak`.

`src/project_manager.py` owns this layout. The project root (not the Logos install
directory) is the terminal/browser `working_directory` and the root passed to
`Indexer`; `.logos` is ignored by the indexer. Do not hardcode `Indexer('.')` or
`os.getcwd()` for project-scoped paths.
