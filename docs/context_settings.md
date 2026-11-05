# Context Settings

Settings → **Context** controls how much of a session's history Logos sends to the model, and when it writes a rolling summary of the session.

The first thing to know: **none of these settings delete anything.** The full conversation — every user request, command, tool output and error — stays in the session journal (`.logos/sessions/<name>.jsonl`) and in the chat window. The options only shape a separate, derived view that is rebuilt before each model request. If the model loses sight of an old detail, it can re-read a file, re-run a command, or inspect the journal.

The page is split into three groups: **History trimming** (how much of the Terminal/Browser history is sent), **Rolling summary** (how and when the session summary is written) and **Automatic compaction** (when older events are replaced by that summary).

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

The Terminal and Browser panels already trim their model-facing history before any setting on this page is considered:

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
| Rolling summary settings (switches, size, threshold, budgets, model) | yes | yes | yes | handoff / manual compaction |
| Automatic compaction | yes | yes | yes | not used |

Chat has no local tool loop, so there is no tool history to trim. Vision uses the provider's Computer Use chain, which the provider manages itself; it receives summaries through handoffs and manual compaction, but automatic compaction does not run there.

## History trimming

These four options shape the model-facing history in the **Terminal** and **Browser** panels. They are applied before the request is sent and do not modify the journal, the chat window or the saved session.

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

The Browser panel observes pages through accessibility snapshots, and the same page is often captured several steps in a row. When this option is on, **consecutive identical snapshots** are replaced with `**Tool Result:** (snapshot unchanged since previous step)`.

- Only page snapshots (tool results containing `Interactive elements:`) are affected.
- The comparison is exact: if anything on the page changed, the new snapshot is kept in full.
- Terminal output is not deduplicated.

**When to change it:** long Browser sessions that click through a form or a list and re-observe an unchanged page many times. This can save a large share of tokens. If the model needs the element list again, it can request a fresh snapshot.

### 4. Trim long tracebacks

*Settings.json key: `history_trim_tracebacks` · Default: off*

Python tracebacks longer than 10 lines are cut to their first 3 and last 5 lines, with a `[... N traceback lines trimmed ...]` marker. The exception type and the final stack frames — the part that tells you (and the model) where the error happened — are kept.

- The rule applies to history entries that contain `Traceback (most recent call last):`, in the Terminal and Browser panels.
- Short tracebacks (10 lines or fewer) are left untouched.

**When to change it:** repeated failing commands produce long framework stacks (pandas, web frameworks, ORM layers) that overflow the interesting part. The default is off because, for some bugs, the middle of the stack is exactly what matters.

## Rolling summary

The rolling summary is what lets a session continue without replaying its entire journal: after compaction, when resuming a Browser or Vision session, and when a fresh Computer Use chain continues earlier work.

- Before a session is left behind (project switch or closing the app), Logos writes a deterministic fallback summary immediately, so the handoff does not depend on an LLM call.
- The LLM refresh runs in the background and updates the same file. Failures keep the previous version (or the fallback) and are retried with a growing delay (15, 30, 60, 120, then 240 seconds).
- Summaries never mix between sessions: each session reads and updates only its own pair of files.
- A summary is not a chat message and is not sent on every request. The model sees it only through a handoff seed or a compaction block.

### 5. Rolling handoff summary

*Settings.json key: `handoff_enabled` · Default: on*

This is the master switch for the rolling summary. It enables:

- background summary refreshes;
- the one-time summary seed when a fresh Vision Computer Use chain continues another session's work;
- freezing the summary before switching project or starting a new session;
- automatic compaction (which stands on the summary).

Turning it off stops all of the above. Specifically:

- summary files are no longer refreshed in the background;
- auto-compact is disabled even if its own switch is on;
- existing `.summary.md` / `.summary.json` files are not deleted and remain readable;
- **Compact now** still works: it generates or refreshes the summary on demand and moves the compaction boundary.

**When to change it:** rarely. Turn it off if you want to prevent any background summarizer calls for a project, at the cost of compaction and handoffs relying on the deterministic fallback.

### 6. On model switch

*Settings.json key: `handoff_on_model_switch` · Default: on · Requires: Rolling handoff summary*

Switching the active model inside a session keeps the session history — the new model receives it on the next request. This option additionally forces a **background summary refresh** at the moment of the switch, so handoff, compaction and later model switches do not rely on a stale summary.

- It does not resend or duplicate the conversation.
- If the session already has a compaction boundary, the `Compacted session context` block is read from the summary file at request time. Refreshing the summary means the new model sees an up-to-date version of the compacted part.
- It adds a summarizer call only when there are new events since the last summary; if the summary already covers everything, nothing is sent.

**When to change it:** turn it off to avoid the extra summarizer call when you frequently toggle models. The next threshold-driven refresh will still update the summary; manual compaction forces a refresh anyway.

### 7. Summary max tokens

*Settings.json key: `summary_max_tokens` · Default: `1200` · Range: 200–8,000, step 100*

A soft token budget for the LLM summary, written into the summarizer prompt as a "stay under N tokens" instruction. It is **not** the provider's `max_tokens` limit and does not cap the conversation or the model's answers.

- Larger values keep more detail in `Done`, `Decisions`, `Constraints` and `Checks`, but make every summary generation and every compaction/handoff block more expensive.
- Smaller values force terse summaries; very small values can drop useful decisions and constraints.
- The deterministic fallback summary does not use this value.

**When to change it:** raise it (2,000–3,000) for long, decision-heavy projects where the handoff must carry a lot of state; lower it (600–800) for short sessions where a compact reminder is enough.

### 8. Refresh threshold

*Settings.json key: `summary_threshold_ratio` · Default: `0.90` · Range: 0.10–1.00, step 0.05*

A near-full safety net: the background summary refresh is triggered when a completion not yet covered by the summary reaches this fraction of the active model's context window.

- Fill is taken from the latest provider-reported usage. The part of the journal the summary already covers is ignored: that completion describes the pre-compaction context, not the next request.
- With defaults this trigger rarely fires: routine refreshes come from the token budget below (about every 30,000 uncovered tokens), and automatic compaction takes over at 200,000 tokens, which is below 90% of a 262k+ window. It matters when auto-compaction is off, the compaction cap is raised, or the budget is `0`.
- The refresh itself does not add a chat message or resend anything. The updated summary is picked up by any handoff or compaction block built after it.
- Manual compaction, the capture before leaving a session, and a model switch force a refresh regardless of this threshold.

Lower values keep the summary fresher on small windows (more summarizer calls); higher values are cheaper but let it lag when nothing else triggers a refresh. Use `0.60`–`0.75` if you set the refresh budget to `0`, or if you raised the auto-compact cap and want a fresh summary well before the window fills.

**Summary refresh budget** — *Settings.json key: `summary_refresh_tokens` · Default: `30000` · Range: 0–1,000,000, step 5,000*

The primary trigger: the summary is refreshed once this many tokens of new journal events are not covered by the current summary. It is independent of the model window, so long sessions stay summarized even though 90% of a 1M-token window is unreachable before compaction. `0` disables the budget and leaves only the window threshold.

The budget is a cost dial: a lower value keeps the handoff closer to the journal (more summarizer calls), a higher value lets the summary lag behind. At the default, one refresh happens per ~30,000 tokens of new history — a handful of calls in a long session, not one per step.

**When to change it:** lower it (10,000–15,000) when the summary must follow a long, detail-heavy task closely; raise it (60,000–100,000) when summarizer calls are expensive and the summary is mostly needed at handoff points.

### 9. Summarizer model

*Settings.json key: `summary_model` · Default: empty (use the active session model)*

An optional model for summary generation, so the expensive working model is not spent on bookkeeping.

- Format: `provider:model`, for example `openai:gpt-5-mini` or `anthropic:claude-haiku-4-5`. The provider must be configured in Logos.
- A bare model id keeps the active provider: `gpt-5-mini` means "the current provider, this model".
- If the model id itself contains a slash (Hugging Face IDs such as `Org/Model`), always use the full `provider:Org/Model` form; otherwise the first slash is read as the provider separator.
- If the summarizer call fails, the previous summary (or the deterministic fallback) stays in place and Logos backs off before retrying.
- The setting is used for every summary refresh: background, model switch, the capture before leaving a session, and manual or automatic compaction.

**When to change it:** point it at a fast, inexpensive model with decent writing quality. A weak summarizer produces a less useful handoff; check the summary file after a few sessions if you are unsure.

## Automatic compaction

### 10. Auto-compact on low headroom

*Settings.json key: `auto_compact` · Default: on · Requires: Rolling handoff summary*

When the context window is nearly full, Logos compacts the session on its own instead of letting a request hit the model limit.

The trigger requires all of the following:

- the context reached `auto_compact_tokens` (default 200,000), or free space in the context window is at or below the response buffer — `max(15% of the window, 16,000 tokens)`;
- the session is at rest: no streaming answer, no running tool call, no waiting background job;
- at least 4 new events have accumulated since the last compaction boundary;
- at least 120 seconds have passed since the last compaction;
- the active panel is Chat, Terminal or Browser (Vision is excluded — its chain is managed by the provider).

What happens then:

1. Logos refreshes the summary and moves the compaction boundary over the events that summary covers (the newest event at the time the summary was requested).
2. From the next request on, the model receives the recent tail plus a `Compacted session context` block instead of the earlier events.
3. A short notice appears in the chat. If the summary fails, the boundary does not move and the notice reports the failure.

The journal, the chat window and the session files keep the full history. Only the model-facing view is shortened. **Compact now** in the Session context dialog works regardless of this switch.

**When to change it:** leave it on for long autonomous runs so the agent does not stall at the window limit mid-task. Turn it off if you want to decide yourself when the history is compacted, accepting that a very long task can reach the model limit.

**Auto-compact at** — *Settings.json key: `auto_compact_tokens` · Default: `200000` · Range: 0–1,000,000, step 10,000*

Automatic compaction also fires when the context reaches this size, even though a 1M+ token window is far from full: smaller requests are cheaper and faster, and the summary keeps the state that matters. `0` disables the cap and leaves only the free-space rule.

**When to change it:** on 1M+ windows keep 200,000; cost-sensitive setups can use 100,000–150,000 for more frequent, smaller requests; set 0 if you want the model to see as much of the raw history as possible.

## How the options interact

- **Rolling handoff summary** is the master switch for options 5, 6 and 10. The four history-trimming options are independent of it, and *Compact now* is not gated by it.
- **Auto-compact** needs both its own switch and the master switch. It uses the summary, so it also depends on the summarizer settings.
- The **refresh threshold and budget** only schedule background updates. Model switches, leaving a session, manual compaction and auto-compaction force a refresh when they need a current summary.
- History trimming reduces what is sent but never touches the journal, the summary or the UI. Summary settings never change the history itself; they only change the compact replacement for it.
- `AGENTS.md` rules, handoff seeds, session context and compaction summaries are protected: none of the trimming options shorten them.

## Practical configurations

| Situation | Suggested changes |
|---|---|
| Default use | Keep all defaults. The app trims baseline history, keeps a summary, and compacts automatically. |
| Long Terminal sessions with hundreds of steps | Keep last N steps full: `20`–`50`; Max chars per entry: `6,000`–`8,000`; Trim long tracebacks: on. |
| Long Browser sessions on the same page | Dedup repeated snapshots: on; optionally Keep last N steps full: `20`–`40`. |
| Cost-sensitive setups | Summarizer model: a cheap model; Auto-compact at: `100,000`–`150,000`; Summary refresh at: `60,000`; auto-compact stays on. |
| Prefer manual control of history | Auto-compact: off; use **Compact now** in the Session context dialog. |

## Where to see what happened

- **Context indicator** next to the Send button: percentage of the model window in use, colored at 80% (warning) and 95% (error). Hover it for the token counts, whether they are provider-reported or estimated, and — when the last answer reported a cache — the read/write split. Click it to open the **Context window** popover.
- **Context window popover**: the percentage, `used / window tokens · source`, a segmented bar over the next request's estimated composition, and the rows `System prompt`, `System tools`, `Skills (N)`, `Messages`, `Other`. **Compress context** runs the same manual compaction as **Compact now**; **Session context…** opens the full dialog. Shares are estimates scaled to the provider-reported total, so they add up to the shown percentage. Vision, whose request is assembled elsewhere, shows a single share and a note instead of rows.
- **Right panel ring**: the same fill percentage in the panel header, under the chevron when the panel is collapsed. Clicking it opens the same popover.
- **Session context dialog**: current fill, model window, usage of the latest request and session totals, the breakdown of the model payload, the compaction boundary, and the **Compact now** button with an optional compaction focus. A summary line above the text states whether the rolling summary came from the model or the deterministic fallback (with provider/model and update time), how many journal events it covers, and whether a refresh is due, running or paused.
- **Session journal** (from the session menu): the full, untrimmed record of every event.
- On disk: `.logos/sessions/<name>.jsonl`, `<name>.summary.md` and `<name>.summary.json` inside the active project.

If the model seems to "forget" an old step, it is almost always the model-facing view rather than lost data: the journal still has everything, and the model can re-read files or re-run commands. If you want more detail to survive automatically, raise **Keep last N steps full**, raise **Max chars per entry**, or increase **Summary max tokens**.
