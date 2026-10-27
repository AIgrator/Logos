# Context Settings

Settings → **Context** controls how much of a session's history Logos sends to the model, and when it writes a rolling summary of the session.

The first thing to know: **none of these settings delete anything.** The full conversation — every user request, command, tool output and error — stays in the session journal (`.logos/sessions/<name>.jsonl`) and in the chat window. The options only shape a separate, derived view that is rebuilt before each model request. If the model loses sight of an old detail, it can re-read a file, re-run a command, or inspect the journal.

This page describes each setting as it appears in the window. For the internal design (journal format, summary schema, provider usage normalization), see the [context management design notes](./context_management.md).

## How context is managed

Logos keeps two layers:

| Layer | Where | What happens to it |
|---|---|---|
| **Session journal** | `.logos/sessions/*.jsonl` | Append-only source of truth. Never rewritten by trimming. |
| **Model-facing view** | Built for each request | Trimmed copy of the journal, plus prompt blocks such as `AGENTS.md` rules and the session summary. |

On top of the journal, each session can have:

- a **rolling summary** — `<session>.summary.md` with fixed sections (`Objective`, `Done`, `Decisions`, `Constraints`, `Checks`, `Current`, `Next`, `Unfinished`, `Files`, `Blockers`), plus a `<session>.summary.json` metadata file. A deterministic fallback is available immediately; an LLM refresh keeps it up to date in the background;
- a **compaction boundary** — a marker set by *Compact now* or by automatic compaction. Events before the boundary are represented by the summary block; only the newer events are replayed to the model.

### Baseline behavior (always on)

Terminal and Browser mode already trim their model-facing history before any setting on this page is considered:

- the most recent ~20,000 characters of history are sent verbatim;
- older **tool results** are shortened to 2,000 characters (head and tail, with a marker in the middle). If the output was saved to a file, its path is kept so the model can read it again;
- entries that are not tool results (requests, commands, reasoning) are not baseline-trimmed;
- the newest entry is never baseline-trimmed, even if it is larger than the budget;
- prompt blocks (`AGENTS.md` rules, handoff seed, session context, compaction summary) are never trimmed or condensed.

The journal always keeps the untrimmed text.

### Where each group applies

| Group | Chat | Terminal | Browser | Vision |
|---|---|---|---|---|
| History trimming (Keep last N, Max chars, Dedup snapshots, Trim tracebacks) | not used | yes | yes | not used |
| Rolling summary settings (switches, size, threshold, model) | yes | yes | yes | handoff / manual compaction |
| Automatic compaction | yes | yes | yes | not used |

Chat has no local tool loop, so there is no tool history to trim. Vision uses the provider's Computer Use chain, which the provider manages itself; it receives summaries through handoffs and manual compaction, but automatic compaction does not run there.

## History trimming

These four options shape the model-facing history in **Terminal** and **Browser** mode. They are applied before the request is sent and do not modify the journal, the chat window or the saved session.

The order of application is: trim tracebacks → cut long entries → deduplicate snapshots → condense old steps → baseline pruning.

### 1. Keep last N steps full

*Settings.json key: `history_keep_last_n` · Default: `0` (off) · Range: 0–200*

When the value is `N > 0`, the model receives the last `N` history entries exactly as recorded. Every older entry is replaced in the model view by a single line that starts with `[older step]` and contains the first line of that entry (up to 160 characters). For a tool result that names a saved full-output path, the path is appended so the model can reopen it.

- "Entries" are individual history records: user requests, agent commands, tool results, model reasoning. The option is not a count of chat turns.
- The order of commands and results is preserved. Nothing is dropped, only condensed.
- Protected blocks are never condensed.
- `0` sends the entire history (subject to the baseline rules above), which is the most expensive option.

**When to change it:** long Terminal or Browser runs with dozens or hundreds of steps, where old outputs repeat in every request. `20`–`50` is a practical range. The trade-off is that the model sees only a one-line reminder of older steps; it has to re-read files or re-run commands if a detail matters.

### 2. Max chars per entry

*Settings.json key: `history_max_result_chars` · Default: `0` (unlimited) · Range: 0–20,000, step 500*

Any single history entry longer than this value is cut to its head and tail (roughly half each) with a `[... N chars trimmed from the middle ...]` marker. Unlike the baseline rule, this cap applies to every non-protected entry — including the newest tool result.

- `0` means no per-entry cap beyond the baseline behavior.
- Protected blocks are excluded.
- The full text remains in the journal, and the model can re-read the source file or re-run the command with narrower output.

**When to change it:** one command — a log dump, `git diff`, a large file read — dominates the prompt. `4,000`–`8,000` is a practical range. Keep it large enough that command output stays actionable; a 500-character cap will make most tool results useless.

### 3. Dedup repeated snapshots

*Settings.json key: `history_dedup_snapshots` · Default: off*

Browser mode observes pages through accessibility snapshots, and the same page is often captured several steps in a row. When this option is on, **consecutive identical snapshots** are replaced with `**Tool Result:** (snapshot unchanged since previous step)`.

- Only page snapshots (tool results containing `Interactive elements:`) are affected.
- The comparison is exact: if anything on the page changed, the new snapshot is kept in full.
- Terminal output is not deduplicated.

**When to change it:** long Browser sessions that click through a form or a list and re-observe an unchanged page many times. This can save a large share of tokens. If the model needs the element list again, it can request a fresh snapshot.

### 4. Trim long tracebacks

*Settings.json key: `history_trim_tracebacks` · Default: off*

Python tracebacks longer than 10 lines are cut to their first 3 and last 5 lines, with a `[... N traceback lines trimmed ...]` marker. The exception type and the final stack frames — the part that tells you (and the model) where the error happened — are kept.

- The rule applies to history entries that contain `Traceback (most recent call last):`, in Terminal and Browser mode.
- Short tracebacks (10 lines or fewer) are left untouched.

**When to change it:** repeated failing commands produce long framework stacks (pandas, web frameworks, ORM layers) that overflow the interesting part. The default is off because, for some bugs, the middle of the stack is exactly what matters.

## Rolling summary

The rolling summary is what lets a session continue without replaying its entire journal: when switching mode, after compaction, and when resuming a Browser or Vision session.

- Before a session is left behind (mode or project switch), Logos writes a deterministic fallback summary immediately, so the handoff does not depend on an LLM call.
- The LLM refresh runs in the background and updates the same file. Failures keep the previous version (or the fallback) and are retried with a growing delay (15–300 seconds).
- Summaries never mix between sessions: each session reads and updates only its own pair of files.
- A summary is not a chat message and is not sent on every request. The model sees it only through a handoff seed or a compaction block.

### 5. Rolling handoff summary

*Settings.json key: `handoff_enabled` · Default: on*

This is the master switch for the rolling summary. It enables:

- background summary refreshes;
- the one-time summary seed when switching mode;
- freezing the summary before switching project or starting a new session;
- automatic compaction (which stands on the summary).

Turning it off stops all of the above. Specifically:

- summary files are no longer refreshed in the background;
- a mode switch no longer copies the summary into the new session;
- auto-compact is disabled even if its own switch is on;
- existing `.summary.md` / `.summary.json` files are not deleted and remain readable;
- **Compact now** still works: it generates or refreshes the summary on demand and moves the compaction boundary.

**When to change it:** rarely. Turn it off if you want to prevent any background summarizer calls for a project, or if you want mode switches to always start from a clean session. With it off, switching modes or projects loses the compact state that would otherwise carry work forward.

### 6. On mode switch

*Settings.json key: `handoff_on_mode_switch` · Default: on · Requires: Rolling handoff summary*

Sessions are mode-locked: changing mode in a non-empty session starts a new session in the new mode. With this option on, Logos copies the previous session's summary once into the new session and shows it to the model as a `Handoff context from earlier work` block. It is delivered once; after that, the new session's own summary takes over.

- Applies to Chat, Terminal, Browser and Vision. Vision receives the block as the first message of its fresh Computer Use chain, which otherwise cannot see the local journal at all.
- With the option off, the new session starts empty. The previous session is still saved and can be reopened from the sidebar.
- This option does nothing if the master switch is off.

**When to change it:** turn it off when a mode change is also a deliberate context reset — for example, when you want a fresh Browser session that is not biased by what Chat was doing. Leave it on for work that continues across modes (investigate in Terminal, then write up in Chat).

### 7. On model switch

*Settings.json key: `handoff_on_model_switch` · Default: on · Requires: Rolling handoff summary*

Switching the active model inside a session keeps the session history — the new model receives it on the next request. This option additionally forces a **background summary refresh** at the moment of the switch, so handoff, compaction and later model switches do not rely on a stale summary.

- It does not resend or duplicate the conversation.
- If the session already has a compaction boundary, the `Compacted session context` block is read from the summary file at request time. Refreshing the summary means the new model sees an up-to-date version of the compacted part.
- It adds a summarizer call only when there are new events since the last summary; if the summary already covers everything, nothing is sent.

**When to change it:** turn it off to avoid the extra summarizer call when you frequently toggle models. The next threshold-driven refresh will still update the summary; manual compaction forces a refresh anyway.

### 8. Summary max tokens

*Settings.json key: `summary_max_tokens` · Default: `1200` · Range: 200–8,000, step 100*

A soft token budget for the LLM summary, written into the summarizer prompt as a "stay under N tokens" instruction. It is **not** the provider's `max_tokens` limit and does not cap the conversation or the model's answers.

- Larger values keep more detail in `Done`, `Decisions`, `Constraints` and `Checks`, but make every summary generation and every compaction/handoff block more expensive.
- Smaller values force terse summaries; very small values can drop useful decisions and constraints.
- The deterministic fallback summary does not use this value.

**When to change it:** raise it (2,000–3,000) for long, decision-heavy projects where the handoff must carry a lot of state; lower it (600–800) for short sessions where a compact reminder is enough.

### 9. Refresh threshold

*Settings.json key: `summary_threshold_ratio` · Default: `0.60` · Range: 0.10–1.00, step 0.05*

The background summary refresh is triggered when the session's context fill reaches this fraction of the active model's context window.

- Fill is taken from the latest provider-reported usage when available; otherwise it is estimated from the journal. In the estimated path, no refresh happens until at least 4 events are recorded.
- The refresh itself does not add a chat message or resend anything. The updated summary is picked up by any handoff or compaction block built after it.
- Manual compaction and the capture before a mode switch force a refresh regardless of this threshold.

Lower values keep the summary fresher (more summarizer calls); higher values make it cheaper but staler. `0.60` is a good default. Use `0.75`–`0.85` if summarizer calls are expensive, and `0.40`–`0.50` if you often switch modes in the middle of a long task and need the handoff to be current.

### 10. Summarizer model

*Settings.json key: `summary_model` · Default: empty (use the active session model)*

An optional model for summary generation, so the expensive working model is not spent on bookkeeping.

- Format: `provider:model`, for example `openai:gpt-5-mini` or `anthropic:claude-haiku-4-5`. The provider must be configured in Logos.
- A bare model id keeps the active provider: `gpt-5-mini` means "the current provider, this model".
- If the model id itself contains a slash (Hugging Face IDs such as `Org/Model`), always use the full `provider:Org/Model` form; otherwise the first slash is read as the provider separator.
- If the summarizer call fails, the previous summary (or the deterministic fallback) stays in place and Logos backs off before retrying.
- The setting is used for every summary refresh: background, model switch, mode-switch capture, and manual or automatic compaction.

**When to change it:** point it at a fast, inexpensive model with decent writing quality. A weak summarizer produces a less useful handoff; check the summary file after a few sessions if you are unsure.

## Automatic compaction

### 11. Auto-compact on low headroom

*Settings.json key: `auto_compact` · Default: on · Requires: Rolling handoff summary*

When the context window is nearly full, Logos compacts the session on its own instead of letting a request hit the model limit.

The trigger requires all of the following:

- free space in the context window is at or below the response buffer — `max(15% of the window, 16,000 tokens)`;
- the session is at rest: no streaming answer, no running tool call, no waiting background job;
- at least 4 new events have accumulated since the last compaction boundary;
- at least 120 seconds have passed since the last compaction;
- the mode is Chat, Terminal or Browser (Vision is excluded — its chain is managed by the provider).

What happens then:

1. Logos refreshes the summary and moves the compaction boundary over the events that summary covers (the newest event at the time the summary was requested).
2. From the next request on, the model receives the recent tail plus a `Compacted session context` block instead of the earlier events.
3. A short notice appears in the chat. If the summary fails, the boundary does not move and the notice reports the failure.

The journal, the chat window and the session files keep the full history. Only the model-facing view is shortened. **Compact now** in the Session context dialog works regardless of this switch.

**When to change it:** leave it on for long autonomous runs so the agent does not stall at the window limit mid-task. Turn it off if you want to decide yourself when the history is compacted, accepting that a very long task can reach the model limit.

## How the options interact

- **Rolling handoff summary** is the master switch for options 5–7 and 11. The four history-trimming options are independent of it, and *Compact now* is not gated by it.
- **Auto-compact** needs both its own switch and the master switch. It uses the summary, so it also depends on the summarizer settings.
- The **refresh threshold** only schedules background updates. Mode switches, model switches, manual compaction and auto-compaction force a refresh when they need a current summary.
- History trimming reduces what is sent but never touches the journal, the summary or the UI. Summary settings never change the history itself; they only change the compact replacement for it.
- `AGENTS.md` rules, handoff seeds, session context and compaction summaries are protected: none of the trimming options shorten them.

## Practical configurations

| Situation | Suggested changes |
|---|---|
| Default use | Keep all defaults. The app trims baseline history, keeps a summary, and compacts automatically. |
| Long Terminal sessions with hundreds of steps | Keep last N steps full: `20`–`50`; Max chars per entry: `6,000`–`8,000`; Trim long tracebacks: on. |
| Long Browser sessions on the same page | Dedup repeated snapshots: on; optionally Keep last N steps full: `20`–`40`. |
| Cost-sensitive setups | Summarizer model: a cheap model; Refresh threshold: `0.75`–`0.85`; auto-compact stays on. |
| Need a clean start on every mode switch | On mode switch: off (and expect no cross-mode context). |
| Prefer manual control of history | Auto-compact: off; use **Compact now** in the Session context dialog. |

## Where to see what happened

- **Context indicator** next to the Send button: percentage of the model window in use. Hover it for the token counts and whether they are provider-reported or estimated.
- **Session context dialog** (click the indicator): current fill, model window, usage of the latest request and session totals, the breakdown of the model payload, the compaction boundary, and the **Compact now** button with an optional compaction focus.
- **Session journal** (from the session menu): the full, untrimmed record of every event.
- On disk: `.logos/sessions/<name>.jsonl`, `<name>.summary.md` and `<name>.summary.json` inside the active project.

If the model seems to "forget" an old step, it is almost always the model-facing view rather than lost data: the journal still has everything, and the model can re-read files or re-run commands. If you want more detail to survive automatically, raise **Keep last N steps full**, raise **Max chars per entry**, or increase **Summary max tokens**.
