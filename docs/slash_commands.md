# Slash commands — what exists and what to add

A status and design note for the composer's command registry
(`src/commands.py`). It records what `/init` established, what OpenCode, Claude
Code, Codex CLI and Gemini CLI ship, and which of those are worth porting to
Logos, split by how much machinery each one needs.

**Status (1.1.0).** `/compact`, `/memory` and `/help` are implemented, together
with the user-level memory layer `/memory` writes to; `/init` is unchanged.
Sections 3, 5 and 6 describe what was built. Everything else below is a backlog,
ordered in section 7.

## 1. The registry

It started with `/init` alone; the shape that one command defined still holds:

| Piece | Where | Note |
| --- | --- | --- |
| Registry | `src/commands.py::COMMANDS` | The only place a command exists; palette, routing and journal follow from it |
| Entry | `SlashCommand(name, summary, build, action, capability, takes_arguments, usage, help)` | `build` for a prompt command, `action` (a controller method name) for an app action; `usage`/`help` are what `/help` prints |
| Payload | `CommandRun(prompt, objective, capability, expects_write)` | A prepared prompt, not a dialog |
| Routing | `AppController.handle_user_input` | `commands.find()` → `_run_command_action()` or `_prepare_command()` → ordinary user message |
| Delivery | `BaseAgentHandler.start_task` | Idle: new task. Running: steered into the current turn (OpenCode's `delivery: "steer"`) |
| Guard | `BaseAgentHandler._expected_write_missing` | A command that promises a file is not believed until the file changes |
| Palette | `src/ui/widgets/command_palette.py` | Opens on a leading `/`, filters by prefix, never takes focus |

Consequences worth keeping: a prompt command never interrupts a turn, pressing
it ten times is fine, and nothing is written to disk unless the agent itself
writes it. An app action writes nothing at all.

## 2. What the other tools ship

### OpenCode (the closest reference)

| Command | Kind | Notes |
| --- | --- | --- |
| `/init` | prompt | `initialize.txt`, the template `/init` was ported from |
| `/review` | prompt | `review.txt` — review the working tree, not the history |
| `/compact` | app action | Only in the ACP catalogue (`builtinCommands`), start: `"compaction"` |
| `/<mcp-prompt>` | prompt | Every MCP prompt the servers expose becomes a command |
| `/<user command>` | prompt | Markdown files in `.opencode/command/`, `$ARGUMENTS`, `!shell`, `@file` |

There is no `/memory`: memory *is* `AGENTS.md`, plus an `instructions` config
array of extra globs. Everything else (models, sessions, undo, share) is UI
chrome, not a slash command.

### Claude Code

The broadest set. Notable groups:

- Context: `/compact [instructions]`, `/context [all]`, `/clear` (`/reset`,
  `/new`), `/rewind` (`/undo`, `/checkpoint`), `/recap`, `/export [file]`
- Memory and setup: `/init`, `/memory` (edit `CLAUDE.md`, toggle auto memory),
  `/permissions`, `/model`, `/config` (`/settings`), `/status`, `/usage`
  (`/cost`, `/stats`), `/doctor`, `/skills`, `/mcp`, `/hooks`, `/plugin`
- Work: `/plan`, `/diff`, `/code-review` (`/review`), `/security-review`,
  `/simplify`, `/run`, `/verify`, `/batch`, `/loop`, `/goal`
- Sessions: `/resume`, `/rename`, `/branch`, `/fork`, `/subtask`, `/tasks`,
  `/stop`, `/btw`, `/side`
- Meta: `/help`, `/theme`, `/keybindings`, `/feedback`, `/bug`

Delivery detail that matches what Logos now does: most commands are queued
behind a running response; a few (`/status`, `/tasks`, `/usage`) run
immediately without touching it.

### Codex CLI

Roughly the same surface with different names: `/init`, `/compact`, `/review`,
`/diff`, `/status`, `/usage`, `/model`, `/permissions`, `/memories`,
`/skills`, `/mcp`, `/plan`, `/goal`, `/clear`, `/new`, `/resume`, `/fork`,
`/side` (`/btw`), `/rename`, `/stop` (`/clean`), `/ps`, `/mention`, `/export`,
`/recap`, `/cd`, `/pwd`, `/worktree`, `/presets`, `/copy`, `/quit`.
`/memories` only toggles *whether* memory is injected and generated — it does
not edit a file.

### Gemini CLI

`/memory show|add|refresh` (project `GEMINI.md` + user `~/.gemini/GEMINI.md`),
`/compress` (= `/compact`), `/restore` (pre-tool file checkpoint), `/rewind`
(conversation only), `/chat save|resume|list`, `/tools`, `/mcp`, `/stats`,
`/copy`, `/extensions`, `/theme`, `/auth`, `/help`.

### What the comparison says

1. **Every tool has `/compact`** under some name. Logos already has the
   machinery (`_request_manual_summary_refresh`) and no command for it.
2. **Two of four have a user-level memory file** (Claude Code's
   `~/.claude/CLAUDE.md`, Gemini's `~/.gemini/GEMINI.md`). Logos injects only
   `<project>/AGENTS.md`, so personal preferences have to be repeated per
   project.
3. **The prompt commands are few and stable** (`/init`, `/review`). The rest of
   the long lists are app actions over features the tool already had.
4. **Nothing worth copying needs a dialog.** Both tools that grew dialogs
   (`/theme`, `/config`) are TUI conveniences; Logos has a settings page.

## 3. Adding a second kind of command

`SlashCommand.build` assumes every command becomes a prompt. An app action has
nothing to send to the model, so the registry needed one more field:

```python
@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    build: Callable[[str, str], CommandRun] | None = None   # prompt command
    action: str = ""                                       # AppController method
    capability: str = ""
    takes_arguments: bool = False
```

The field holds the *name* of a controller method, not a callable: `COMMANDS` is
a module-level tuple, so it cannot carry a bound method, and keeping the
behaviour in the controller leaves the registry as data that cannot drift from
the code that runs it. `handle_user_input` grows one branch before
`_prepare_command`:

```python
if command is not None and command.action:
    self._run_command_action(command, user_text)   # no turn, no journal event
    return
```

Three rules for actions:

- **No turn, no journal event.** `/status` or `/memory` must not create a
  session event; they are chrome, like a notice.
- **Never interrupt a running turn.** `/status`, `/context`, `/memory`,
  `/model` are safe mid-turn. `/compact` already refuses with a reason while a
  summary is in flight. `/clear` is the one that stops work, so it asks first.
- **No `QMessageBox`.** Command results go through `ui.add_message`, because a
  modal over the composer for a one-line answer is exactly what the notice
  routing was added to avoid.

The action branch also runs *before* the provider check, so `/memory` works on
a fresh install that has no API key yet. A missing or raising action is caught
in `_run_command_action` and reported as one italic line, never as a crash.

The palette needs no change: it renders `COMMANDS` and already filters by
prefix. `/help` needs none either: it renders each entry's `usage`/`help` plus
`commands.MEMORY_HELP`, so a new command explains itself the moment it is added.

## 4. The candidates, by cost

### 4.1 Prompt commands (one entry each, zero new infrastructure)

| Command | What it sends | Source |
| --- | --- | --- |
| `/review` | Review the working tree diff; report bugs, not style | OpenCode `review.txt` |
| `/simplify` | Cleanup pass over the changed code, no bug hunting | Claude Code |
| `/security-review` | Security pass over the branch diff | Claude Code |
| `/test` | Run the project's tests, fix failures, report what stayed red | Codex `/review` sibling |
| `/fix <text>` | Fix the described bug; no argument → the last error in the transcript | Codex |
| `/explain <target>` | Explain a file, folder or symbol | common |
| `/docs <target>` | Write or refresh README/docstrings for a target | common |
| `/commit` | Stage and commit with a message derived from the diff | Claude Code |
| `/pr` | Draft a PR description from the branch diff | Claude Code |
| `/plan <text>` | Produce a plan with `update_plan`, change nothing | Claude Code, Codex |

`/review` is the obvious first one: the prompt already exists in the reference
source, it fits the `/init` shape exactly (prepare a prompt, let the agent
work), and it needs no capability the session does not already have.

### 4.2 App actions (no model call, existing features behind a name)

| Command | Wires to | Work |
| --- | --- | --- |
| `/compact [focus]` | `_request_manual_summary_refresh(focus)` | **done** — new kind + notices instead of `QMessageBox` |
| `/context` | `_show_context_summary()` / the context popover | Trivial |
| `/status` | `session_panel_payload`: provider, model, capabilities, context %, tokens, steps | Trivial |
| `/clear` (`/new`, `/reset`) | `new_session()`, confirm while busy | Trivial |
| `/model [id]` | `switch_model()` / focus the model button | Trivial |
| `/permissions [ask\|auto\|full]` | `terminal.access_mode` via `_set_access_mode` | Trivial, confirm `full` |
| `/settings [section]` | `show_settings_page()` + an optional section argument | Small |
| `/mcp` | `MCPManager` status as a notice; no argument opens Settings → MCP | Small |
| `/skills` | `skills` scan as a notice; no argument opens Settings → Skills | Small |
| `/rename <name>` | session title, same as the sidebar menu | Trivial |
| `/stop` | `_handle_stop()` | Trivial |
| `/copy` | last answer's `copy_text` | Trivial |
| `/help [command]` | `commands.help_html()`, built from the registry | **done** — one message, no dialog |
| `/export [path]` | transcript → Markdown in the project | Medium: needs a renderer |
| `/goal <text>` | `session_objective` + a steer | Medium: semantics |

### 4.3 Needs new infrastructure

| Command | Why it is expensive |
| --- | --- |
| `/memory` | No user-level file existed; injection, cap, cache and `/init`'s contract all changed — **done**, section 5 |
| `/remember <text>` | Writes a memory file from a prompt; needs a target rule and the write guard |
| `/undo`, `/rewind` | File checkpoints do not exist; the journal is append-only |
| `/diff` | `src/git_status.py` is numstat-only by design; a diff viewer is new UI |
| `/recap` | A deterministic summary exists, a good one-line recap does not |
| `/fork`, `/side` | Two live turns over one session; the handler is single-turn |
| auto-memory | Extraction pass, dedupe, injection budget — a subsystem, not a command |

### 4.4 Not worth porting

`/theme`, `/vim`, `/keymap`, `/title`, `/statusline`, `/tui`, `/pets`,
`/voice`, `/raw`, `/copy`-as-picker, `/logout`, `/feedback`, `/apps`,
`/plugins`, `/hooks`, `/ide`, `/cd` (projects are switched from the sidebar),
`/mention` (`@` file search already exists), `/archive`, `/delete`, `/presets`.
They are terminal-UI affordances or duplicate an existing Logos surface.

## 5. `/memory` — what was built

### Before

- `src/context/project_rules.py` read `<project>/AGENTS.md` only: 32 KB cap,
  cached by `(mtime_ns, size)`, redacted, injected as
  `**Project instructions (AGENTS.md):**`.
- `AppController.project_baseline_prompt()` is the single call site, used by
  Chat (`chat_context_prompt`) and by the agent history refresh
  (`_refresh_agent_context`), so one block reaches every mode.
- The app never wrote that file on its own; the user wrote it or ran `/init`.

### Now

Two layers, Claude Code's shape with Logos' paths:

| Layer | Path | Who writes it | Cap |
| --- | --- | --- | --- |
| User memory | `~/.logos/AGENTS.md` | `/memory <text>`, or the user in an editor | 8 KB |
| Project memory | `<project>/AGENTS.md` | `/init`, or the user in any editor | 32 KB (unchanged) |
| Session | the journal | the app | — |

`~/.logos` is the folder the app already owns for `skills/` and `mcp/`, so no
new convention appeared.

**Injection.** `project_rules.injection_block(root)` returns the user block
first and the project block second, so a project rule overrides a personal
default:

```
**User instructions (~/.logos/AGENTS.md):**
<text>

**Project instructions (AGENTS.md):**
<text>
```

Both blocks go through the same `_read_file(path, cap, label)` helper: HTML
comments stripped, redacted, cached by `(mtime_ns, size)`, and truncated with a
label that names the layer (`[user memory truncated at 8 KB]`). Comment
stripping is what makes a freshly created template inert: the template is one
`<!-- ... -->` block, so it reaches neither the prompt nor `/memory`'s own
output. The two blocks are returned as one string, which is why the history
refresh needs a filter that recognizes *either* prefix:
`project_rules.is_generated_block(text)` replaced the bare
`startswith(BLOCK_PREFIX)` check in `_refresh_agent_context`, so a project
switch still replaces both layers instead of stacking them.

**The command.** One target and two gestures:

| Input | Behaviour |
| --- | --- |
| `/memory` | Shows the file: path, size, contents. Writes nothing and opens nothing |
| `/memory <text>` | Appends each non-empty line as one bullet, verbatim, creating the file from the template when missing. The notice names the path, the size and when it applies |

Deliberate details:

- **Verbatim, no model.** Memory has to be what the user typed, not a
  paraphrase of it, and the write must be instant and work mid-turn, so
  `project_rules.append_memory` writes the file itself.
- **One bullet a line.** A multi-line message becomes several rules, and a typed
  `- ` marker is collapsed instead of doubled into `- - ...`.
- **Duplicates skipped**, with "Already in user memory" instead of a second
  copy.
- **Credentials refused.** `redact_text` changing the text means it holds
  something that looks like a key; memory is plain text on disk and goes into
  every request, so the write is refused rather than stored.
- **8 KB cap refused**, not silently truncated: the message says how full the
  file is.
- **No editor.** Deleting or rewriting a line has no command form; `/memory`
  prints the path and any editor opens it. The command exists because
  `~/.logos/AGENTS.md` is the one memory file with no home in the project tree.

**Why `/memory project` is not a thing.** `<project>/AGENTS.md` lives in the
repository: it is in the Files tab, in git and in every editor the project is
already open in, and `/init` writes its first draft. A second target would add a
grammar to learn for a file the user can already reach in one click, and
`/init`'s prompt already tells the model to improve that file in place rather
than rewrite it, so a hand-written rule survives a re-run.

**`/remember` is deliberately not built.** It is the only path where the model
would write memory, and it would need a target rule plus the write guard
pointed at a path known only at build time — for a result `/memory <text>`
already gives without a request. Section 7 keeps it out.

**Why not auto-memory now.** Claude Code extracts memories from the
conversation on its own. That needs a second model call per turn, a dedupe
policy, an injection budget and a way to show what was stored — a subsystem
with its own failure modes, and one that quietly pollutes the context. Explicit
`/memory` first; auto-memory only if it proves too slow in practice.

## 6. `/compact` — what was built

Everything already existed:

```python
def _request_manual_summary_refresh(self, focus: str = ""):
    focus = (focus or "").strip()[:handoff.FOCUS_MAX_CHARS]
    ...  # no events / busy / already pending / already covered / request
```

It already handled the focus argument, a summary in flight
(`_manual_compact_pending`), an unchanged coverage, a failed boundary write, and
the busy reason. The context popover's **Compress context** calls the same
method.

What the command added:

1. One `SlashCommand` entry with `action="_command_compact"`, whose method
   forwards the tail as the focus.
2. The three `QMessageBox` calls in that method became `ui.add_message`
   notices, because the command path must not open a modal over the composer.
   The dialog path (Session context) wanted the same thing — a notice is enough
   there too.
3. Nothing else. The applied boundary is journaled as a `compaction` event and
   replayed as an italic notice, so the transcript already explains why older
   history disappeared.

Mid-turn `/compact` is refused with one line rather than interrupting: a
compaction while a step is in flight would cut the history the running turn is
about to continue from.

## 7. Order

| Priority | Commands | Reason |
| --- | --- | --- |
| **done** | `/init`, `/compact`, `/memory` (+ user-level injection), `/help` | The registry, its two kinds, and the help that explains them; built in 1.1.0 |
| P0 | `/status`, `/context`, `/clear` | The rest of what makes a growing registry usable |
| P1 | `/review`, `/remember`, `/model`, `/permissions`, `/settings`, `/rename`, `/stop`, `/copy`, `/mcp`, `/skills` | Cheap, and each replaces a trip to a panel or a menu |
| P2 | `/test`, `/fix`, `/explain`, `/commit`, `/pr`, `/simplify`, `/security-review`, `/docs`, `/plan`, `/export` | Prompt commands: valuable but each needs its own prompt quality bar |
| P3 | `/undo`, `/rewind`, `/diff`, `/recap`, `/fork`, auto-memory | New infrastructure; decide after P0/P1 usage |

## 8. Open questions (remaining)

1. **`/clear` while a turn runs.** Refuse (Codex) or confirm and stop (Logos'
   New session)? Recommendation: confirm, since the user asked explicitly.
2. **Should app actions be journaled?** Recommendation: no. They are chrome;
   the compaction they trigger already writes its own event.

Answered while building `/memory`: the user file lives at `~/.logos/AGENTS.md`,
`/memory <text>` appends to it verbatim, and the command does not touch the
project's `AGENTS.md` at all — that file is written by `/init` and edited like
any other project file.
