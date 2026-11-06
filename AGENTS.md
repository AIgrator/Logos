# AGENTS.md

Notes for AI coding agents working in this repository.

## Commands

- Install / sync dependencies: `uv sync` (uses `pyproject.toml` + `uv.lock`; Python pinned to 3.12).
- Lint: `uv run ruff check <paths>` (config in `pyproject.toml`, line-length 500).
- Entry point: `main.py`. There is no automated test suite (no pytest); changes are verified manually.

## Packaging

Both builders are ephemeral tools, never dependencies: do not add PyInstaller or Inno Setup to `pyproject.toml` / `requirements.txt`.

- One-file executable: `uv run --with pyinstaller pyinstaller logos.spec --noconfirm --clean` -> `dist/Logos.exe` (about 6 minutes, ~204 MB).
- Installer: `"$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe" installer/logos.iss` -> `dist/LogosSetup.exe`. Per-user only (`PrivilegesRequired=lowest`, default `%LOCALAPPDATA%\Programs\Logos`), user picks the folder, Start Menu shortcut, HKCU uninstall entry for Settings > Installed apps, and the uninstaller deletes the whole data folder below. It does not touch the Chromium engine in `%LOCALAPPDATA%\ms-playwright` (about 700 MB, shared with other Playwright programs) unless the user ticks the uninstaller's checkbox, which starts empty and is skipped on silent uninstalls. Its optional Tasks (all checked by default) carry the Windows integration the source setup script does: `logos.cmd` in `{app}` plus an append to the user PATH (`ChangesEnvironment`, removed on uninstall), the HKCU `Directory\shell\Logos` "Open with Logos" folder menu, and the desktop shortcut. Because the installer owns these, a frozen build skips step 2 of `src/initial_setup.py`; source setups keep it because `setup_windows.ps1` reads `setup_choices.json`.

`build/` and `dist/` are packaging artifacts and git-ignored. A frozen build keeps settings, logs, cache and the materialized `prompts/` + `assets/` in `%LOCALAPPDATA%\Logos` (see `src/app_paths.py`), because the one-file unpack folder is temporary and the install folder may be read-only. Compilation success alone is not proof: launch the built exe and, for installer changes, run install -> launch -> uninstall.

## Version

`[project] version` in `pyproject.toml` is the only place a release number
exists. `src/build_info.py` reads it at runtime (source runs) or from the copy
`logos.spec` bundles next to the executable, so the window title
(`… [v0.2.0]`), the startup log line and Settings → About
(`Version 0.2.0 · build <commit>` for source runs) all follow a one-line bump.
`logos.spec` also writes an EXE version resource from that number, and
`installer/logos.iss` reads it back (`GetStringFileInfo(…, "ProductVersion")`),
so the installer needs no hand-edited `AppVersion`. The commit hash is still
logged and shown on the About page when the app runs from a checkout; an
installed build has no repository and shows the version alone — never
"unknown".

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
the home screen. A folder argument (`Logos.exe <folder>`, used by the "Open with
Logos" context menu) wins over the remembered project and registers the folder as
a project. When another instance already runs, the new process sends
`open_project:<folder>` over the `Logos-Assistant-Lock` local socket and exits,
and the running window switches to that project as if it had been picked in the
app (`prevent_multiple_instances` controls the single-instance check). In the
workspace, projects and sessions are reached through the collapsible left sidebar;
its footer keeps only Home, New session and Settings. The session context menu
(shared by the sidebar and the home screen) carries Rename, Show journal, Show in
Explorer and Delete Session; Show journal opens the raw event journal of that
session, whether or not it is the open one.
The full-mode window frame (`window_geometry`) and the sidebar's collapsed state
(`sidebar_collapsed`) are saved to `settings.json` on close and restored on the
next launch; layout margins are not persisted. The right workspace panel
(`src/ui/right_panel.py`) is visible in every mode. Its Session tab leads with
context statistics (provider, model, the session's attached capabilities,
context window and usage, token totals,
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
  `session` header (`schema_version`, `id`, `mode`, `capabilities`, optional
  `title`, optional `objective`) and later
  lines are typed events (`message`, `reasoning`, `progress`, `plan`,
  `tool_call`, `tool_result`, `job_result`, `usage`, `compaction`, `error`,
  `suggestions`, `model`). `reasoning` events carry the
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
  live row title) and `log_path`; a screenshot also records `image_path`, so
  the picture is drawn again on replay. A background launch also records `job_id`,
  which the later `job_result` (`job_id`, `name`, `status`, `exit_code`, the
  same `display*`/`log_path`) updates in place. `message` events may carry an
  `attachments`
  list (name, kind, path, size, SHA-256, stored flag/status). `usage` events
  carry normalized provider token
  counts
  (input/output/reasoning/cache/total); they are counted in context statistics
  but never replayed as chat text. No prices are stored or computed. Credentials
  are redacted before events are written. A `model` event (`provider`, `model`)
  is written on every model switch inside a session (writes are skipped when no
  session is open). Opening a session silently applies its latest `model` event
  (`AppController._restore_session_model`, after the replay): no chat notice, no
  summary handoff, no new journal event. A saved provider that is no longer
  configured, a model missing from the merged catalogue or one that cannot run
  the session's capabilities keeps the current model and adds one italic warning.
  `model` events are never rendered, and they do not make an empty session
  non-empty: a panel switch there still rewrites only the header and keeps them.
  A new session inherits the current model,
  and Settings → Providers holds the default; a switch stays in memory until
  the next settings save (window state on close), so the last used model
  deliberately becomes the starting model and is persisted as such. A `compaction` event records each
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
  A thought row is always exactly one line: its first non-empty line is
  clipped to the text column and ends with `…`, and the full text stays behind
  the expand click. Qt's rich text has no `text-overflow`, so the clip is
  measured (`ActivityBlockFormatter._thought_excerpt`, fed by
  `ChatWidget._measure_activity_text` and `_activity_column_width`) and the
  excerpt is `white-space: nowrap`, so a narrow window never wraps it. A
  window resize re-renders the blocks that have thoughts once it settles
  (`_excerpt_timer` → `_rerender_excerpts`, skipped under 24 px), so the
  ellipsis follows the new width instead of leaving a half-empty line.
  A single-step run stays a plain step row. Runs are open while a step is
  running and when one failed or was denied, and fold to
  `Ran N tools · <tool breakdown>` once finished clean; the row toggles via an
  `activity:<message id>:run:<first step id>` anchor, with the override kept
  in `msg['run_state']`. The block header is only the caret and the title:
  `Working` while live, a clean finish turns into what the turn did
  (`6 steps · read_files, write_file +1`, from `block_summary`), and a real
  outcome keeps its word (`Stopped`, `Request failed`, `Waiting for you`).
  There is no overall turn timer and no status-colored card edge, and the
  outcome never derives from step results:
  replay closes every block as `Done`, which the header renders as that same
  summary, because a failed step is not a failed
  turn (the model often retries and succeeds); the red badge and expanded output
  stay on the step row alone. Every step row (and every run row) opens with a
  small circled status badge instead of a trailing tick: a thin green
  circle-check for `ok`, a red circle-cross for `error`, a hollow accent ring
  for `running`, plus amber `!` and grey dash for `denied` / `stopped`
  (`src/ui/widgets/status_marks.py`). They are drawn with `QPainter`, not
  typed glyphs, so they stay crisp and follow the theme colors
  (`STATUS_OK_COLOR`, `STATUS_ERROR_COLOR`, ...); `ChatWidget._new_document`
  registers them as document resources under `logos-status:<state>`, because
  the transcript is one `QTextDocument` per block and a bundled image file
  would need a variant per state and theme. The caret stays a quiet grey so
  the badge is what the eye lands on. When the turn ends with `finish`, the block
  folds to its header (`collapse="auto"` in `ChatWidget.finish_activity`):
  the answer stays open below, the work does not. Auto-fold is skipped when the
  user toggled the block by hand during the turn (`msg['user_toggled']`), when
  a step failed or was denied with no successful step after it
  (`_has_unresolved_failure`), or while a background job is still running;
  `Request failed`, `Stopped` and `Waiting for you` keep their block open.
  Replay always folds (`collapse="always"`), so a restored session shows all
  work collapsed. There is no green `✓ Done` line under the answer.
  A background shell job outlives the step that launched it, so it also gets a
  pinned strip above the composer (`src/ui/widgets/job_panel.py::JobPanel`,
  mounted first in `UIManager.bottom_center_column`, opposite the live plan):
  no job hides it, one job is a full row (status dot, the command, a 1 s clock
  and `Stop` / `Open log`, plus a hover card `_JobPopover` that reads the last
  lines of the job log and shows the folder, state and readiness URL), and two
  or more fold into one capsule (`2 jobs · npm run dev, http.server 8000 ▾`)
  in the same chrome as the plan pill, so the composer never grows a line per
  background process. Clicking the capsule opens `_JobListPopover` (a
  `Qt.Popup` above it, clamped to the screen) with one `_JobListRow` per job:
  state dot, `job #N · command`, the time, `Stop` and `Open log`; a job that
  ends while that list is open is held in `JobPanel._held` until the list
  closes, so the outcome of a Stop does not vanish under the cursor, and the
  held rows show grey with no Stop link and a frozen time. The dock runs its
  clock only for a single row or an open list, because the capsule is static
  text; `_sync` is re-entrancy-guarded (`_syncing` / `_sync_pending`) and
  `_in_hide` keeps `hideEvent` from re-showing the panel through its own
  `_on_list_closed`, which is the trap a `Qt.Popup` closing on `hide()` sets.
  The handler pins the job in `_on_tool_finished` (`_track_job_panel`, with the
  clock backdated through `shell_jobs.status(...).elapsed_seconds`, because a
  readiness probe can spend half a minute before the launch returns) and drops
  it in `_apply_job_ui`, which every finish path (event, wait tick, stop)
  reaches; a poll updates the state text. The dock reuses the launch step's
  `job_started_at` as its own start, so the two clocks show the same number.
  The launch step row keeps the same
  clock: `_show_activity_result` records `job_started_at` / `job_finished_at`
  and `ActivityBlockFormatter._step_duration` measures the job, not the
  half-second step, so the transcript does not freeze at `0s`. A duration
  under a second is omitted entirely (`_duration_text`) instead of printing
  `· 0s`, so a quick call carries no clock at all. Only a
  launching `execute_shell` gets that handle (`live_job_id`): a poll or stop
  snapshot reports `status: running` for the job it describes, but its row is
  a static `ok` row - treating it as the handle left a poll row ticking for
  minutes after the server was stopped and kept the finished turn from
  auto-folding. `ChatWidget._needs_activity_tick` keeps the 1 s render alive
  for a block whose turn already finished while a job row still runs
  (`_has_live_job`), and a replayed row (no `job_started_at`) never ticks.
  A Stop click on a job that already ended drops its pinned row instead of
  leaving it in `stopping` (`TerminalHandler.request_stop_job`).
  A command still waiting in the foreground can be handed to the background by
  the user instead of the tail of the turn: a running `execute_shell` step row
  offers `Move to background` (anchor
  `activity:<msg id>:detach:<row id>` → `ChatWidget.job_detach_requested` →
  `AppController._handle_job_detach`), and Ctrl+B does the same through a
  window-scoped `QShortcut` in `UIManager` (so it also works while the composer
  has focus). `TerminalHandler.request_detach_job` takes
  `ShellJobManager.foreground_job_id()` - the one job a `launch_and_wait` is
  still waiting on - and `request_detach` sets that job's `detach_event`; the
  wait loop then returns the live handle at once with `detached_by_user`, so
  the row becomes a job row, the strip pins it, and `_on_tool_finished`
  continues the loop instead of `_arm_job_wait`. The model is told the user
  moved it and to keep working; it polls or stops the job as usual. The
  auto-detach after `terminal.shell_detach_seconds` (default 25 s, capped at
  the shell timeout) still covers the case where nobody clicks. Its result
  carries `detached_by_time` and tells the model the command was moved
  automatically and is not a background launch it asked for, because the old
  wording ("Background shell job #N started") made a model obeying "do not
  run anything in the background" stop the job and relaunch it twice.
  A session reset (`UIManager.clear_chat`) clears the strip; compact mode
  hides it (`set_suppressed`) without forgetting the jobs.
  Every message carries a footer strip under it: a user bubble the local send
  time, an answer the model that wrote it, the time and - when the model
  finished its turn with a conclusion - a copy button
  (`ChatWidget._footer_html`, rendered by `_render_message`). The strip sits
  outside the bubble table because `ChatWidget._user_bubble_rect` paints the
  background under the message's own text blocks only. It stays hidden until
  the pointer is over that message (`_message_index_at` + `_apply_hover` from
  `mouseMoveEvent`, cleared by `leaveEvent`); the line itself is always rendered
  (an empty `&nbsp;` paragraph), so revealing a strip never moves the transcript
  under the cursor. `Pending` is the exception and shows without hover, because
  the user has to see that the text has not reached the model yet.
  `ts` is the journal event's UTC stamp (live messages hand over
  `time.time()`), converted to the reader's zone by `format_message_time` -
  `12:19` today, `3 Nov, 12:19` this year, `3 Nov 2025, 12:19` older. An
  assistant strip names `msg['actor']`: the model id captured when the message
  was created (`current_assistant_name` live, the session's model on replay,
  moved forward by each `model` journal event). The button is a painted icon
  (`src/ui/widgets/transcript_icons.py`, `logos-icon:copy` / `:copied`, the
  same QPainter-as-document-resource approach as the status badges) and copies
  `msg['copy_text']`, the plain prompt or answer rather than the rendered HTML;
  the icon flips to a check for 1.6 s. Only a completed final answer gets the
  button (`copyable=True` from `finish` / `communicate_to_user`, from the final
  `finalize_stream`, and on replayed assistant `message` events), so a stopped
  turn and an interim `display_message` note never grow one. The same strip
  dates a `queued` message, which keeps its own dimmed HTML in
  `msg['queued_content']`.
  The strip is chrome, not message text: `_render_message` stores where it
  starts (`msg['meta_start']`) and `_selectable_end` keeps selection endpoints
  out of it, so dragging across a message never highlights or copies the
  status, the model name, the time or the icon, and a click inside the strip
  anchors at the end of the body. Copy All takes a message's body with a cursor
  that stops at `meta_start`, and `_selected_plain_text` strips Qt's U+2029 /
  U+FDD0 / U+FDD1 layout markers (every bubble is a table) from anything
  copied.
  A bubble whose text is accepted but has not reached the model yet leads the
  strip with `Pending` (accent color): a mid-task steer folded into the next
  planning turn (`BaseAgentHandler._steer_mid_task`, which no longer writes the
  old "Noted - I'll take it into account after the current step finishes."
  line), a note recorded while the task is paused, and a chat message waiting
  behind a streaming answer (which keeps its italic `queued` label inside the
  bubble as well). The controller flags these in
  `_mark_pending_user_message` and drops the status in
  `_clear_pending_user_messages`, called from `_send_llm_request` (the single
  funnel every request goes through, so `Pending` disappears exactly when the
  text is sent), from `handle_cancel_request` (Stop: the text is abandoned and
  the usual `Request cancelled.` line explains it) and from
  `BaseAgentHandler.stop` (a steer that never made it into a request dies with
  the task). `activate_queued_message` clears it for the chat queue. `Pending`
  is live UI state and is never journaled, so a restored session shows none.
  Inside an expanded step the raw call is a disclosure, not content: the
  `Arguments` label toggles through an
  `activity:<message id>:args:<row id>` anchor (`row['args_expanded']`) and the
  one-line monospace peek of the call sits outside that link, so the label is
  chrome while the call itself can be selected, and
  it is not rendered at all when the call is already its title or its detail -
  `BaseAgentHandler._step_args_text` drops null/empty leaves and hands the
  trimming to the handler (`TerminalHandler` replaces file bodies with
  `<N chars>`, removes the `execute_shell` command and the browser
  `description` that became the title), so a live step and its replay show
  the same call; `_format_args` also skips a step whose `detail_html` already
  renders the body. A long result is a preview of 25 lines / 3 500 characters
  plus a `Show all · N more lines` link (`row['output_expanded']`, anchor
  `...:output:<row id>`, `Show less` to fold back), because the transcript is
  one `QTextDocument` and cannot nest a scroll area inside a card. A
  screenshot step draws the picture itself
  (`ActivityBlockFormatter._format_image`): the tool result carries
  `image_path`, the row keeps it (the journal event does too), and a
  thumbnail sits under the step title, outside the collapsible body, so it is
  visible without expanding the step. Its width follows the reading column
  (`available_width`, capped at 520 px), and the picture is an
  `attachment-preview:` link, so a click opens the existing full-size preview
  dialog rather than an editor. A pruned file (the newest 200 are kept)
  renders nothing instead of a broken image. A running
  call reports itself on one grey line inside its own step row
  (`row['progress']`, drawn by `ActivityBlockFormatter._format_progress`,
  clipped to 140 characters): `TerminalHandler._on_tool_progress` writes the
  clock, the percent and the last output line there and skips a text identical
  to the previous one, so a progress callback every 0.2 s cannot turn into a
  5 fps re-render. The old separate live card (`ChatWidget.show_tool_progress`)
  drew the same clock and the same tail as a second row and is now only the
  fallback for a call without a step row. The line never outlives the call:
  `activity_update_step` drops it as soon as `status` leaves `running`, and
  `finish_activity` / `_show_activity_result` clear it explicitly. A waiting
  counter (`waiting… 12/30s`) already carries the time, so `_progress_line`
  does not append the step clock next to it. Both one-second clocks of the
  transcript and of the job dock are single-shot `QTimer`s re-armed through
  `next_second_delay_ms()`, so they wake on the same wall-clock second and read
  `time.monotonic()` together instead of drifting a second apart. Both the
  output box and the code box normalize `\r\n` / `\r` to `\n` before escaping
  (Qt's `pre-wrap` draws a lone `\r` as its own break, so Windows command
  output used to render double spaced) and use a 95 % line-height. A long turn
  - a written file, a big listing, a live run still piling up steps - is taller
  than the screen, so its own header (the caret and the title) leaves the top
  of the transcript. A sticky bar (`ChatWidget.sticky_bar_button`) pins it and
  folds what it names on click, so the reader never scrolls back to the header
  first. `_sticky_target_at_scroll` picks the level: inside a step the reader
  opened it reads `▾ Writing file · catalog.html` and folds that step alone
  (`_step_marker` is armed by the click that opened it and
  `_remeasure_step_growth` measures the height that toggle added), while over
  the block as a whole it repeats the block summary
  (`▾ 6 steps · read_files, write_file +1`) and folds the entire block.
  The block level is read from the rendered document heights
  (`_block_at_scroll`), so a block that fits on screen never gets a bar, and
  folding or scrolling out of either level hides it.
  Selection is scoped to content, not to chrome: `ChatWidget._select_ranges`
  returns the character ranges a reader may select and copy from each
  document, and both the highlight (`paintEvent` paints one
  `QAbstractTextDocumentLayout.Selection` per intersection) and `copy_selection`
  / `copy_all_text` use only those ranges. Card headers, run rows, step titles,
  carets, status badges and the `Arguments` / `Show all` links are links, so the
  fragment's `anchorHref()` marks them as chrome; bare labels (`Output`,
  `Command`, `System note`, `Changed N replacement(s)`) are matched by their
  text outside a table. Content stays selectable: narration, an expanded
  thought, the arguments peek and body, command output, a written file, an
  edit diff and `detail_html`. A user bubble is content up to its status line
  and a chat answer stays fully selectable (its markdown links are content),
  while a suggestion row is not selectable at all. The computed ranges are
  cached per message on `(id(doc), character count, block count, meta_start,
  consumed, type)`, because a drag repaints on every mouse move.
  The plan lives outside the transcript: a live turn
  shows a compact `Step N / M` pill above the composer
  (`src/ui/widgets/plan_panel.py::PlanPanel`, mounted first in
  `UIManager.bottom_center_column`), hovering it opens a themed checklist
  popover, and the pill disappears when the turn ends
  (`BaseAgentHandler._activity_finalize`). A mid-turn user message leaves it
  alone; a panel switch and `clear_chat` clear it, compact mode only hides it
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
  `src/llm/llm_parsing.py`, which folds it into `actions`. The same module also
  rebuilds a call whose envelope is missing or wrong: a tool call that arrives
  without the `payload` wrapper (the payload's own keys at the top level), with
  the action name as `type` (`{"type": "execute_shell", "payload": {...}}`), or
  with `actions` as a JSON string, is wrapped/decoded before validation, so a
  weak model's shape never turns into a silent empty turn. Stringified scalar
  arguments (`"false"`, `"null"`) are matched to the tool's declared parameter
  types in `src/handlers/tool_exec_worker.py` before the call; a list/object
  parameter sent as one JSON string (`paths="[\"a.html\"]"`) is decoded, and a
  bare value for a list parameter becomes a one-item list, because otherwise
  `read_files` opens the string's characters. `update_plan` steps get the same
  treatment (`_plan_steps` in `base_agent.py`): a stringified plan is unwrapped
  instead of turning every character into a step of the live pill. A tool's
  declared `arguments` must match its real signature: `find_files` advertised
  `path` while the function accepted `directory`, so a live run died with
  `find_files() got an unexpected keyword argument 'path'`. It now takes `path`
  and keeps `directory` as a compatibility alias, the same way
  `search_file_content` does. A user
  message mid-turn seals the live block: `ChatWidget.add_message` with
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
  `compacted_through` / `compacted_event_count` / `compacted_at`). A summary
  belongs to its own session and is never copied into another one: attaching a
  capability in place keeps the same journal (so there is no boundary to
  bridge), while `handoff_prompt` still feeds a fresh Computer Use chain - which
  cannot replay local events - from the summary of the session it continues. The
  deterministic fallback is always available (its `Current` comes from the last
  assistant message, else the latest request - never raw tool output); the LLM
  refresh runs in the worker thread and
  is injected as chat system context, terminal/browser history, or the first
  task of a fresh Vision/CUA chain when continuing from another session.
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

Sessions are no longer mode-locked. Switching the active panel to `terminal`,
`browser` or `vision` attaches that capability to the **open** session: the
journal, the transcript and the events stay, `capabilities` in the header grows,
and `_prepare_session_for_mode` neither starts a new file nor copies a handoff
summary (`_seed_session_summary`, which did that, is gone). Switching in an
empty session still only rewrites the header. Deleting the open session clears
the chat and leaves no journal behind; the next message creates a session in the
current mode. Legacy `session-*.json` files are migrated to JSONL on load and the
old file is kept as `.json.bak`.

The composer no longer carries a mode selector. `src/ui/widgets/capability_bar.py`
(`CapabilityBar`) holds four controls — Chat, Terminal, Browser, Vision: a check
marks a capability the session holds, the checked control is the panel the next
turn presents, and a click asks the controller to open that panel
(`presentation_requested` → `UIManager._on_capability_opened` → the matching
`_activate_*`), which attaches the capability on the way. Chat is the base-only
presentation until Phase 4, so it never carries a check. Revoking is deliberate
(P8): right-clicking an attached Terminal/Browser/Vision control offers
"Detach … from session" (`capability_detach_requested` →
`AppController._detach_capability_clicked` → `detach_capability`), which rewrites
the header, keeps the journal, the transcript and the handler, leaves the shared
browser and the MCP manager alive, and falls back to the nearest presentation the
session still holds (Terminal, else Browser, else Chat). `UIManager.set_capabilities`
mirrors `session_capabilities`; `_sync_capability_ui` refreshes it after a panel
switch, an attach/detach and whenever a journal is opened. The bar takes the
selector's place in both toolbar layouts, and `set_vision_mode_loading` disables
only the Vision control. Switcher notices speak of panels and capabilities
("Terminal panel opened…", "Browser capability detached…") instead of modes.

`src/computer/capabilities.py` is the single authority on which tools a session
may use. A capability is what a session is allowed to do (`base`, `terminal`,
`browser`, `vision`, plus `chat` until it becomes base-only); a mode is how the
current build delivers it, so `MODE_GRANTS` maps `terminal` and `browser` to
`base + <capability>` and `vision`/`chat` to their own whole sets;
`merge_grants` unions them when a capability is attached in place and
`remove_grants` drops one on an explicit detach (`base` is never dropped). The
journal header records `capabilities`, and `grants_from_header` derives them from
the legacy `mode` for journals written before it existed. `ToolRegistry` builds
every cache file from those grants and drops (with a warning in the log) a tool
that a mode module declares but no capability grants, so the two can no longer
drift apart silently; `cache/capabilities/tools.json` is the generated
catalogue. `AppController.session_capabilities` mirrors the open session's
grants.

`CATALOG_TOOLS` splits a grant into two exposures, and that split is a prompt
budget, not a permission: `native_tools_for_grants` keeps the common
step-by-step tools, `catalog_tools_for_grants` the rest (the three MCP
meta-tools and the browser edge cases — eval, console, network, frames, dialog,
select, hover, find, back), which the prompt only *names* under a closing
"Tools on demand" section. `search_tools` (a `base` control tool intercepted by
`BaseAgentHandler._dispatch_action` like `update_plan`) matches a name or a
description, returns the loaded schema and promotes the tool into
`_promoted_tools`; the next request declares it and the strict grammar accepts
it, and `start_task` clears the set. Per-mode cache files follow the same split
(`ToolRegistry._declared_metadata`), so the byte-for-byte equality with a pure
grant set still holds. Native counts: terminal 29, browser 20, both 39.

`TerminalHandler` is the app's only agent loop: `AppController.browser_handler`
is the same object, so Terminal and Browser are two *presentations* of one
handler (`presentation`, `schema_name`, `tool_view`; `BaseAgentHandler` defaults
them to `MODE_NAME`/`SCHEMA_NAME`). The presentation comes from
`controller.session_mode` and decides the system prompt (`prompts/terminal` or
`prompts/browser`), the response envelope (`terminal_tool_code` /
`browser_tool_code`) and the `cache/session` view, never which tools are
allowed — that is the grants' job. Shell/file calls go to the exec thread and
`browser_*` calls to the shared browser service, so a browser turn can run shell
tools and a terminal turn can run browser ones. Browser-only habits follow the
presentation: the step/time budgets (`browser.max_steps`,
`browser.max_task_seconds`), the persistent (peeked) screenshot, dropping a
cancelled task's history, and secret/URL redaction of browser arguments. A mode
switch passes its mode explicitly to `_cleanup_and_switch_handler`, or relies on
the target state naming it (`CHATTING`/`TERMINAL`/`BROWSER`/`VISION`), because
handler identity no longer tells Terminal from Browser.

The tool block and the response format follow the session's grants, not its
mode (`src/computer/session_tools.py`, `src/llm/tool_catalog.py`).
`ToolRegistry.generate_session_profile(grants, handlers)` writes
`cache/session/<handler>/{tools.txt,response_formats.json,profile.json}`; the
signature in `profile.json` (grants + tool names + on-demand names + schema
generation) is checked
while the prompt is built, so attaching a capability or promoting an on-demand
tool regenerates the view on the
next turn and a pure `base+terminal` / `base+browser` grant set produces exactly
the per-mode block and envelope, byte for byte. `HANDLER_CAPABILITIES` narrows
that to what one handler can execute: Terminal and Browser are one loop
(`TerminalHandler` presents as either), and that loop routes `browser_*` calls
into the shared browser service and everything else onto its exec thread, so
both entries list `base+terminal+browser`. The provider layer (`tool_catalog`)
merges `cache/session/*` over `cache/<mode>/*`, so the granted envelope wins
under the same name (`terminal_tool_code` / `browser_tool_code`), and each
provider keys its formatted-tool cache on `catalogue_stamp()` (file mtimes) so a
grant change cannot be answered with yesterday's contract. The two agent
`ToolCall` models (`schemas/terminal.py`, `schemas/browser.py`) list
`AGENT_TOOL_NAMES` — the union of the three agent capabilities — because the
envelope must accept a tool of a capability attached beside it; permission is
decided at execution time. Startup clears the session views, since no session is
open yet.

`src/project_manager.py` owns this layout. The project root (not the Logos install
directory) is the terminal/browser `working_directory` and the root passed to
`Indexer`; `.logos` is ignored by the indexer. Do not hardcode `Indexer('.')` or
`os.getcwd()` for project-scoped paths.

`BrowserService` (`src/handlers/browser_service.py`) owns the app's single
`BrowserManager` and the plain thread it runs on (`BrowserExecWorker`, name
`browser-exec`), so a panel switch or a hard Terminal Stop never starts a second
Chromium. One loop dispatches through it and passes its own GUI-thread
`ToolExecRelay`, so a browser step renders in the turn that asked for it:
`TerminalHandler.execute_action` routes every `browser_*` call there (before the
terminal access policy, since the browser policy classifies it).
`AppController.browser_service` is created before the handlers and shut down in
`cleanup()`. Do not build a
`Computer(mode='browser')` or a `BrowserManager` anywhere else — one profile and
one login state are the point. `browser_screenshot` saves into
`<project>/.logos/screenshots` (newest 200 kept; the configured app folder when
no project is open) through `BrowserManager.screenshot_dir`, and the PNG is
attached to the next request: persistently in the browser presentation, and in a
terminal turn while its last step was a browser one
(`TerminalHandler._request_attachments`).

`open_browser(url)` is the control tool a session uses to attach the browser
capability from inside an agent turn (Phase 2 of
[the vision plan](docs/future_vision.md)). It is a `base` tool, declared in both
agent manifests, and intercepted before the terminal access policy like
`browser_*`: `TerminalHandler._open_browser_action` calls
`AppController.attach_capability("browser")` (grants merge, nothing is
summarized, the panel does not switch) and then dispatches `browser_goto` on the
shared service, so the step card and the journal record `open_browser` while the
browser runs the navigation. `AppController.attach_capability` updates
`session_capabilities` and the header's `capabilities`, so the next prompt's
session profile already lists the browser tools and the envelope accepts them.
When the browser capability is already attached the same action is just a
`browser_goto`.

`open_terminal()` is its mirror and the other half of Phase 2: also a `base`
tool and a control tool, handled in `BaseAgentHandler._attach_terminal_capability`
before any action runs (`_dispatch_action`), because it executes nothing itself.
It attaches the terminal capability, starts the MCP manager (a session that
gained terminal tools mid-turn must see its servers on the next step) and hands
the model a tool result saying the shell/file/MCP tools are available from the
next step, plus a chat notice for the user. Since both panels are one loop,
either call grants tools the running handler can execute immediately.

`search_tools` is the third `base` control tool and the way an on-demand tool
becomes usable: the catalogue is matched by name or description
(`BaseAgentHandler._match_catalog_tools`), the loaded schema is returned as the
tool result, and the match is added to `_promoted_tools` so the *next* request
declares it. That action aborts the rest of the batch (`abort=True`) because the
queued calls were planned without the tool. Only tools the grants already hold
are reachable — a catalog entry is never a permission.

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
Anthropic-compatible endpoints open `custom_provider_dialog.py`. The Google
schema offers no native-search toggle on purpose: Gemini's Google Search
grounding is billed and unavailable on free keys, so chat web search goes
through the app's own `web_search` / `web_fetchs` tools, and the form only
notes that Vision needs a paid key. That dialog's
**API format** selector writes `custom_providers[i]["protocol"]` (`openai`
default, or `anthropic`); `src/llm/custom_providers.py` builds a
`CustomOpenAIProvider` or `CustomAnthropicProvider` subclass accordingly. The
Anthropic variant speaks Messages (`{base}/v1/messages`, `anthropic-version`,
content blocks, forced `tool_choice`) but deliberately omits native Anthropic
extras (Computer Use, web search, strict schemas, adaptive thinking). Custom
providers stay out of Vision either way.

The composer's model button leads with the active provider's brand glyph
(`src/ui/provider_icons.py`, the same bundled SVGs the provider tiles use,
re-tinted on every theme change), so the toolbar says who answers; a custom
endpoint has no bundled glyph and simply gets no icon instead of the old
generic bolt.

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

Vision blocks are hard (`src/computer/vision_policy.py`): the action never runs,
so repeating it cannot help. The chat therefore follows the block reason with a
`policy_hint_text` line that names the exact settings to change - Settings →
Vision and «Allow Windows Search / Start menu», «Allow terminal commands during
Vision tasks», or the `Applications` list with «Scan for Applications» when the
app is not there yet - plus the blocked app or shortcut the check carried
(`app` / `attempt`). `VisionHandler._policy_hint_codes` keeps it to one hint per
code per task (reset in `start_task`) so a retrying model cannot flood the
transcript, and the restarted CUA chain is seeded with "Do not retry it" and the
same instruction.

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

## Screenshots (Terminal mode)

`screenshot` (`src/computer/tools/terminal/system/screenshot.py`, capture in
`src/computer/screen_capture.py`) lets the Terminal agent look at the desktop
instead of guessing. With no arguments it captures the primary display;
`window_title` (case-insensitive part of the title) or `process_id` captures one
window; `left`/`top`/`right`/`bottom` capture a rectangle given by its top-left
and bottom-right corners. Window capture is Windows-only: `PrintWindow` with
`PW_RENDERFULLCONTENT` first (the only way to read a window that other windows
cover), then a `BitBlt` of the window DC, then bringing the window forward and
grabbing its rectangle off the screen; a single-color frame counts as a failed
render. Screen and region grabs go through mss, on every platform, always on
the primary monitor for the full-screen case. All coordinates are physical
pixels - `main.py` asks for Per-Monitor V2 DPI awareness before `QApplication`
exists, so the tool, mss and the Qt window share one space. A partial region (1
to 3 coordinates) is an error, not a guess, and so is `right <= left` or
`bottom <= top`.

The PNG is saved to `<project>/.logos/screenshots/screenshot_<timestamp>.png`
(the app's `APP_SCREENSHOTS_DIR` when no project is open) and the result carries
`path`, `relative_path`, `width`, `height`, `bytes`, `target`, `method`
(`printwindow` / `screen` / `region`) and `attached`. The newest 200 files are
kept, older ones are pruned like `mcp_outputs`. For screen and region captures
the tool hides Logos' own `QMainWindow`s through
`QMetaObject.invokeMethod(..., BlockingQueuedConnection)`, waits for the
compositor, then restores them, so the app never covers what is being captured;
`hide_app: false` keeps it in the picture, and capturing Logos' own window with
the default `hide_app` is rejected with a hint to pass `false`.

The image itself reaches the model once: `screenshot` calls
`Computer.set_pending_screenshot`, and `TerminalHandler._request_attachments`
(consumed by `BaseAgentHandler._plan_next_action`) reads up to the last four
pending files into the next request and forgets them, unlike the browser
presentation's own shot, which lives until the next navigation. A fresh task
drops leftovers
(`TerminalHandler.start_task`). Attachment is gated only by a catalogue entry
that explicitly lists modalities without `image`; an uncatalogued model (older
or newer than `models.json`, or a hand-typed custom one) still gets the image,
like Browser mode, so a text-only model fails the request loudly instead of the
agent silently working blind. The result's `attached: false` plus `note` is
what the model sees when the catalogue said no. `screenshot` is read-only for
`terminal_policy.py`, so it never asks for approval.

## Browser engine

Browser mode drives a real Chromium through Playwright, and that binary is not
packaged (~320 MB download, ~700 MB on disk). `src/browser/runtime.py` owns the runtime decisions:
`PLAYWRIGHT_BROWSERS_PATH` is pinned to a stable per-user folder
(`%LOCALAPPDATA%\ms-playwright`; `~/.cache/ms-playwright` or
`~/Library/Caches/ms-playwright` elsewhere; an explicit environment value wins),
because the Python package otherwise starts its driver with
`PLAYWRIGHT_BROWSERS_PATH=0`, meaning "inside the driver package" — the
temporary PyInstaller `_MEI` folder of a frozen build, which changes on every
launch and disappears on exit. `pin_environment()` runs before
`sync_playwright().start()` and before any install, so lookup and download
agree. `is_chromium_installed()` matches the exact revision from the bundled
`browsers.json`; when it is missing, switching to Browser mode opens
`src/ui/browser_engine_dialog.py`, which explains the download and shows
progress while `install_chromium()` runs the bundled driver
(`node cli.js install chromium`) on a thread. The driver fetches four archives
in turn (Chromium, its headless shell, FFmpeg, winldd) and counts every one
from 0 to 100 %, so the bar follows the current archive only: it restarts when
the next file begins, and the status line above it names that file and its size
(`Chromium · 197.5 MiB`), both taken from the driver's own `20% of 197.5 MiB`
lines. `runtime.describe_progress` strips the driver's bar art and the
`https://…` link from an archive title (that link used to swallow the name, so
the label read `Downloading · 197.5 MiB`). The `Download: ~320 MB` line in the
body is the only place the whole total is stated. Settings → Browser shows the same
status and button. `BrowserManager.launch()` refuses with a readable message
instead of Playwright's "Executable doesn't exist at …_MEI…" error.

Every navigation, redirect included, passes `src/browser/policy.py::validate_url`.
`browser.network_access` decides how far it may reach and is set in
Settings → Browser: `any` (default) is a normal browser — localhost, LAN
addresses, `file://` paths all open; `local` keeps localhost but refuses private
LAN addresses; `public` refuses both. The guard exists only for pages the user
does not trust (a page's text can try to talk the agent into visiting a router
or a local file), so the default is unrestricted. `file://` accepts any
existing file with an empty or `localhost` host — Browser mode can already read
files with `read_files`, so a project-root restriction protected nothing.
An IPv6 address that only wraps an IPv4 one is judged by the wrapped address:
IPv4-mapped (`::ffff:0:0/96`) and NAT64 (`64:ff9b::/96`) are unwrapped first.
Without that, a NAT64 resolver's answer for an IPv4-only site (`ukr.net` →
`64:ff9b::23ba:da43`, i.e. 35.186.218.67) fell into `::/8`, which Python reports
as IETF-reserved, and a public page looked local.

Grounding is refs first, but not refs only (`src/browser/manager.py`).
`BrowserManager.snapshot()` walks every frame of the page, so a control inside a
cross-origin iframe (widgets, captchas, checkout forms) gets a ref like any other
element and is marked `(frame N: host)` in the `browser_snapshot` output; clicks,
fills and hovers then address it by that ref. `browser_read` appends the text of
each frame as a `Frame N (host)` line, because a cross-origin frame never appears
in the top document text. `browser_click` also takes `x`/`y` for the cases where
no ref can exist — canvas, image maps, widgets that react only to raw mouse
events, a frame the snapshot capped out on: CSS pixels from the top-left of the
visible page (the same pixels as a `browser_screenshot`, which is never scaled),
validated against the viewport so a stray point cannot click outside the page,
with an optional `button` (left/right/middle). `browser_frames` lists the frames
with their index, and `browser_eval` runs one JavaScript expression in the page
or in a frame by index; its result is capped at 6 000 characters and wrapped as
untrusted page content. `browser_eval` is the only browser tool whose every call
asks for approval (`classify_action` in `src/browser/policy.py` returns a
`page_javascript` check showing the script), because page JavaScript can read
cookies and storage and fetch with the page's session. This is the reason the
webscraper.io bot-check "CDP detection with a click within cross domain iframe"
test can now pass at all: before frame-aware snapshots the checkbox had no ref
and no tool could reach it. The remaining failure on that page, "Popular
Browser", comes from the `navigator.userAgentData` brand list of a plain
Chromium build, not from automation signals, and closing it is stealth work
(`patchright`/`rebrowser-patches`), deliberately not done yet.

Console and network traffic are captured passively (`src/browser/logs.py`), so
the model can see why a page broke instead of guessing. `BrowserManager` hooks
`console`, `pageerror`, `response`, `requestfailed` and `framenavigated` on
every page it attaches (popups included, through `_attach_page`); listeners
never drive the page and only append to two bounded rings (1 000 console
entries, 3 000 responses) and to one file per browser launch under
`<project>/.logos/browser_logs/browser_<YYYYmmdd_HHMMSS>.log` (8 MB cap, newest
50 files kept, `resolve_log_dir` returns nothing when no project is open, so
the rings still work in the standalone harness). `browser_console` (arguments
`limit`, `level` = all/error/warning) and `browser_network` (`limit`,
`only_problems`) return the newest entries — newest last, clipped to 7 000
characters with the tail kept — plus the log path, and the model reads the rest
with `read_files` / `search_file_content`, which is why a noisy SPA never floods
the transcript. Both are read-only for `classify_action` and both results are
wrapped as untrusted page content, because console text and URLs are authored by
the page. Network entries are recorded on the response (method, redacted URL,
status, resource type, `request.timing` duration) and on `requestfailed`, so a
blocked ad request shows up as `FAILED` rather than silently disappearing.
Console text is stored as the page wrote it, so a page that prints a token puts
it in that local log; URLs are always redacted with `redact_url`.

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
