# src/ui/widgets/activity_block_formatter.py
"""HTML renderer for the per-turn activity block.

One block groups a turn's model thoughts, narration and tool steps so the
transcript reads as a timeline instead of a wall of separate cards. The plan
is not part of it: it lives in the composer's live strip and disappears with
the turn. Rows keep their own expanded state; the formatter never mutates the
message.
"""
from __future__ import annotations

import html
import time

from src.ui.widgets.status_marks import MARK_SIZE, STATUS_STATES

_TEXT_LIMIT = 30000
_ARGS_LIMIT = 12000
# A result longer than this is a preview: the transcript should stay a
# timeline, not a terminal buffer. The rest stays one click away.
_OUTPUT_PREVIEW_LINES = 25
_OUTPUT_PREVIEW_CHARS = 3500
# Width of the one-line argument peek next to the collapsed Arguments link.
_ARGS_PREVIEW_CHARS = 110
# The step's live progress line (waiting clock, percent, last output line).
_PROGRESS_PREVIEW_CHARS = 140

# Status badges lead a row instead of trailing it. A tick after the title reads
# like a read receipt; a quiet mark in front of it is a status column, where the
# shape carries the meaning and the color only supports it. The images are
# registered per document by ``status_marks.register_status_images``.

# Read-only lookups. A turn usually starts with a long survey of the project;
# folding those steps keeps the transcript readable while edits and commands
# stay individual rows where their titles matter.
_EXPLORATION_TOOLS = {
    "read_files",
    "list_directory",
    "search_file_content",
    "web_search",
    "web_fetchs",
    "mcp_list",
    "mcp_get",
}


def step_category(row: dict) -> str:
    """Coarse step kind used to decide where a tool run ends."""
    name = str(row.get("name") or "")
    return "explore" if name in _EXPLORATION_TOOLS else "action"


def run_extent(rows: list, start: int) -> int:
    """Last row index of the tool run beginning at ``start``.

    Only consecutive steps of the same category form a run. A thought or a
    narration line ends it: those rows belong to the response that produced
    the following steps, so folding them into the previous group would hide
    the model's thinking inside a collapsed "Ran N tools" line.
    """
    if not 0 <= start < len(rows):
        return start
    category = step_category(rows[start])
    last = start
    index = start + 1
    while index < len(rows):
        if rows[index].get("kind") != "step":
            break
        if step_category(rows[index]) != category:
            break
        last = index
        index += 1
    return last


def steps_in_run(rows: list, start: int) -> list:
    end = run_extent(rows, start)
    return [row for row in rows[start:end + 1] if row.get("kind") == "step"]


def run_default_expanded(steps) -> bool:
    """Live or failed runs stay open; finished clean runs fold into one line."""
    statuses = [str(row.get("status") or "") for row in steps]
    return any(status in ("running", "error", "denied") for status in statuses)


def block_tool_summary(msg: dict) -> str:
    """Tool names used by a block, at most two plus a ``+N`` count."""
    names = []
    for row in msg.get("rows") or []:
        if row.get("kind") != "step":
            continue
        name = str(row.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    if len(names) > 2:
        return ", ".join(names[:2]) + f" +{len(names) - 2}"
    return ", ".join(names)


def block_summary(msg: dict) -> str:
    """What a block did, as its header reads it: ``6 steps · read_files, write_file``.

    A finished turn says that instead of an outcome word, because "what ran"
    tells the reader more than "Done" does.
    """
    steps = [row for row in msg.get("rows") or [] if row.get("kind") == "step"]
    if not steps:
        return ""
    label = f"{len(steps)} step" if len(steps) == 1 else f"{len(steps)} steps"
    tools = block_tool_summary(msg)
    return f"{label} · {tools}" if tools else label


def next_second_delay_ms(now: float | None = None) -> int:
    """Delay (1..1000 ms) to the next whole wall-clock second.

    Both one-second clocks of the app - the transcript's step timers and the
    background-job dock - read `time.monotonic()` when they render. Free-running
    `QTimer`s started from different moments tick out of phase, so the same job
    could read 10s in its step row and 11s in the dock in the same instant. Both
    timers are single-shot and re-arm through this helper, so they wake together
    just after each second and read the clock at the same moment.
    """
    current = time.time() if now is None else now
    delay = 1000 - int((current % 1.0) * 1000)
    return max(1, min(1000, delay))


def format_duration(seconds: float | None) -> str:
    """Compact duration: 8s, 1m 05s, 1h 02m."""
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + (
        f"\n… [truncated for display, {len(text) - limit} chars hidden]")


def _one_line(text: str, limit: int) -> str:
    """Flattens JSON onto one line, keeping both ends when it must shrink."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    tail = 24
    head = max(20, limit - tail - 1)
    return flat[:head] + "…" + flat[-tail:]


class ActivityBlockFormatter:
    """Renders one activity block message into themed HTML."""

    def __init__(self, theme_manager):
        self.theme_manager = theme_manager
        # The transcript is one QTextDocument, and Qt's rich text has no
        # text-overflow, so a row that must stay on one line has to be clipped
        # by measurement. ChatWidget supplies both callbacks (its column width
        # and a font-metric measure); without them the excerpt falls back to a
        # plain character limit.
        self.available_width = None
        self.measure_text = None
        self.last_width = 0

    def _get_style(self, key: str) -> str:
        try:
            return self.theme_manager.get_style(key) or ""
        except Exception:
            return ""

    def format_block(self, msg: dict, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        self.last_width = self._column_width()
        meta = self._get_style("COLLAPSIBLE_META")
        title_color = self._get_style("COLLAPSIBLE_TITLE")
        status = str(msg.get("status") or "running")
        if status == "error":
            title_color = self._get_style("ERROR_COLOR")
        elif status == "waiting":
            title_color = self._get_style("WARNING_COLOR")

        expanded = bool(msg.get("expanded", True))
        caret = "&#9662;" if expanded else "&#9656;"
        # A cleanly finished turn speaks through what it did, not through the
        # word "Done": the step and tool count says more than an outcome does.
        # Real outcomes (stopped, failed, waiting for you) keep their label.
        title = str(msg.get("title") or "").strip()
        if title in ("", "Done"):
            title = block_summary(msg) or title or "Working"
        title = html.escape(title)
        header = (
            f'<div style="font-weight: bold;">'
            f'<a href="activity:{msg.get("id")}:toggle" '
            f'style="text-decoration: none; color: {title_color};">'
            f'<span style="color: {meta};">{caret}</span>&nbsp;{title}</a></div>')

        body = ""
        if expanded:
            rows = msg.get("rows") or []
            body = self._format_body(msg, rows, meta, now)
            if body:
                # Qt's document height ignores a trailing table's bottom
                # border/cell padding (and block bottom margins), so a last
                # step detail used to be clipped by the card edge. The
                # spacer restores the missing strip plus bottom padding.
                body += '<div style="font-size: 12px; margin: 0;">&nbsp;</div>'

        # The card's fill and border are painted by ChatWidget:
        # Qt's rich text does not propagate a div background into nested
        # block rows, which left striped gaps under anchors. Outer spacing
        # must be margins: Qt ignores CSS padding on blocks.
        return f"""
        <div style="margin: 10px 12px 10px 15px;">
            {header}
            {body}
        </div>
        """

    def _format_body(self, msg: dict, rows: list, meta: str, now: float) -> str:
        """Rows in order, with each run of consecutive tool steps folded.

        A run is exactly the set of steps one response issued: a thought or a
        narration line between steps belongs to the next response and stays
        visible, so the model's reasoning is never swallowed by a folded
        "Ran N tools" row.
        """
        parts = []
        index = 0
        while index < len(rows):
            if rows[index].get("kind") != "step":
                parts.append(self._format_row(msg, rows[index], meta, now))
                index += 1
                continue
            steps = steps_in_run(rows, index)
            if len(steps) <= 1:
                parts.append(self._format_row(msg, rows[index], meta, now))
                index += 1
                continue
            end = run_extent(rows, index)
            parts.append(self._format_run(msg, rows[index:end + 1], meta, now))
            index = end + 1
        return "".join(parts)

    def _format_row(self, msg: dict, row: dict, meta: str, now: float) -> str:
        kind = row.get("kind")
        if kind == "thought":
            return self._format_thought(msg, row, meta)
        if kind == "note":
            return self._format_note(row)
        if kind == "step":
            return self._format_step(msg, row, meta, now)
        return ""

    def _format_run(self, msg: dict, run_rows: list, meta: str,
                    now: float) -> str:
        steps = [row for row in run_rows if row.get("kind") == "step"]
        run_id = str(steps[0].get("id") or "") if steps else ""
        expanded = self._run_expanded(msg, run_id, steps)
        caret = "&#9662;" if expanded else "&#9656;"
        status = self._run_status(steps)
        mark = self._status_mark(status)
        label = ("Running" if status == "running" else "Ran")
        title = f"{label} {len(steps)} tools"
        suffix_bits = []
        breakdown = self._tool_breakdown(steps)
        if breakdown:
            suffix_bits.append(breakdown)
        failed = sum(1 for row in steps if row.get("status") == "error")
        denied = sum(1 for row in steps if row.get("status") == "denied")
        if failed:
            suffix_bits.append(f"{failed} failed")
        elif denied:
            suffix_bits.append(f"{denied} denied")
        suffix = ""
        if suffix_bits:
            suffix = (
                f'&nbsp;<span style="color: {meta}; font-weight: normal;">'
                f"&middot; {' &middot; '.join(suffix_bits)}</span>")
        detail = ""
        if expanded:
            inner = "".join(self._format_row(msg, row, meta, now)
                            for row in run_rows)
            detail = f'<div style="margin: 2px 0 0 14px;">{inner}</div>'
        run_id_html = html.escape(run_id, quote=True)
        caret_html = f'<span style="color: {meta};">{caret}</span>'
        return (
            f'<div style="margin-top: 6px;">'
            f'<a href="activity:{msg.get("id")}:run:{run_id_html}" '
            f'style="text-decoration: none; color: '
            f'{self._get_style("COLLAPSIBLE_TITLE")};">'
            f"{caret_html}&nbsp;{mark}<b>{title}</b>{suffix}</a>{detail}</div>")

    @staticmethod
    def _run_status(steps) -> str:
        statuses = [str(row.get("status") or "") for row in steps]
        if "running" in statuses:
            return "running"
        if "error" in statuses:
            return "error"
        if "denied" in statuses:
            return "denied"
        if statuses and all(status == "stopped" for status in statuses):
            return "stopped"
        return "ok"

    @staticmethod
    def _tool_breakdown(steps) -> str:
        """Counts per tool name: "3 read_files, 2 list_directory"."""
        counts: dict[str, int] = {}
        for row in steps:
            name = str(row.get("name") or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        if not counts:
            return ""
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        parts = [name if count == 1 else f"{count} {name}"
                 for name, count in ordered[:4]]
        if len(ordered) > 4:
            parts.append("…")
        return ", ".join(parts)

    def _run_expanded(self, msg: dict, run_id: str, steps) -> bool:
        state = msg.get("run_state") or {}
        if run_id and isinstance(state, dict) and run_id in state:
            return bool(state[run_id])
        return run_default_expanded(steps)

    def _row_toggle(self, msg_id: str, row: dict) -> str:
        row_id = html.escape(str(row.get("id") or ""), quote=True)
        return f"activity:{msg_id}:row:{row_id}"

    def _thought_excerpt(self, text: str) -> str:
        """One line for the row: as much of the thought as fits, then "…".

        A thought is the model's reasoning, so the row shows its opening and
        keeps the rest behind the expand click. It must never wrap: a second
        line would tuck the reasoning under the row and double the height of
        every turn.
        """
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first:
            return ""
        budget = self._excerpt_budget()
        if budget <= 0 or not callable(self.measure_text):
            legend = first[:140]
            return legend + "…" if len(first) > 140 else legend
        measure = self.measure_text
        if measure(first) <= budget:
            return first
        low, high = 0, len(first)
        while low < high:
            middle = (low + high + 1) // 2
            if measure(first[:middle].rstrip() + "…") <= budget:
                low = middle
            else:
                high = middle - 1
        clipped = first[:low].rstrip()
        return clipped + "…" if clipped else "…"

    def _column_width(self) -> int:
        """Width of the text column at the last render, or 0 when unknown."""
        width_fn = self.available_width
        if not callable(width_fn):
            return 0
        try:
            return max(0, int(width_fn() or 0))
        except Exception:
            return 0

    def _excerpt_budget(self) -> int:
        """Pixels left for the excerpt on a thought row, or 0 when unknown."""
        width = self._column_width()
        if width <= 0 or not callable(self.measure_text):
            return 0
        # The block is inset by its own margins (15 px left, 12 px right) and
        # the row starts with the caret and the bold "Thought" label.
        inset = 15 + 12 + 14
        prefix = self.measure_text("▾\u00a0Thought ", True)
        return max(0, width - inset - prefix)

    def _format_thought(self, msg: dict, row: dict, meta: str) -> str:
        expanded = bool(row.get("expanded"))
        caret = "&#9662;" if expanded else "&#9656;"
        text = str(row.get("text") or "")
        excerpt = html.escape(self._thought_excerpt(text))
        excerpt_html = (
            f'&nbsp;<span style="color: {meta}; font-weight: normal; '
            f'white-space: nowrap;">{excerpt}</span>' if excerpt else "")
        body = ""
        if expanded:
            body = (
                f'<div style="color: {meta}; white-space: pre-wrap; '
                f'font-size: 94%; margin: 4px 0 0 14px;">'
                f"{html.escape(_clip(text, _TEXT_LIMIT))}</div>")
        return (
            f'<div style="margin-top: 6px;">'
            f'<a href="{self._row_toggle(msg.get("id"), row)}" '
            f'style="text-decoration: none; color: {meta};">'
            f'{caret}&nbsp;<b>Thought</b>{excerpt_html}</a>{body}</div>')

    def _format_note(self, row: dict) -> str:
        text = html.escape(str(row.get("text") or "")).replace("\n", "<br>")
        return (
            f'<div style="margin-top: 6px; white-space: pre-wrap;">'
            f"{text}</div>")

    def _status_mark(self, status: str) -> str:
        """Circled badge that opens a row, drawn by ``status_marks``."""
        status = status if status in STATUS_STATES else "running"
        return (f'<img src="logos-status:{status}" '
                f'width="{MARK_SIZE}" height="{MARK_SIZE}">&nbsp;')

    def _format_step(self, msg: dict, row: dict, meta: str, now: float) -> str:
        expanded = bool(row.get("expanded"))
        caret = "&#9662;" if expanded else "&#9656;"
        status = str(row.get("status") or "running")
        mark = self._status_mark(status)
        title = html.escape(str(row.get("title") or "Step"))
        duration = self._step_duration(row, now)
        suffix = ""
        if duration:
            suffix += (f'&nbsp;<span style="color: {meta}; font-weight: normal;">'
                       f"&middot; {duration}</span>")
        detail = ""
        if expanded:
            detail = self._format_step_detail(msg, row, meta)
        progress = self._format_progress(row, meta)
        # Job actions stay outside the collapsible body: Stop and Open log
        # must be reachable without expanding the step first.
        actions = self._job_actions(msg, row)
        caret_html = (f'<span style="color: {meta};">{caret}</span>'
                      if caret else "")
        return (
            f'<div style="margin-top: 6px;">'
            f'<a href="{self._row_toggle(msg.get("id"), row)}" '
            f'style="text-decoration: none; color: '
            f'{self._get_style("COLLAPSIBLE_TITLE")};">'
            f"{caret_html}&nbsp;{mark}{title}{suffix}</a>{progress}{detail}{actions}</div>")

    def _format_progress(self, row: dict, meta: str) -> str:
        """The running tool's own progress, one grey line under its title.

        A waiting command used to draw a second live card with the same clock
        and the same tail, so the transcript showed two competing rows for one
        call. The line belongs to the step it describes; it disappears with the
        step's result (``status`` leaves ``running``).
        """
        text = str(row.get("progress") or "").strip()
        if not text or str(row.get("status") or "") != "running":
            return ""
        return (
            f'<div style="margin: 1px 0 0 14px; color: {meta}; '
            f'font-size: 90%; font-family: Consolas, monospace; '
            f'white-space: nowrap; overflow: hidden;">'
            f"{html.escape(_one_line(text, _PROGRESS_PREVIEW_CHARS))}</div>")

    def _step_duration(self, row: dict, now: float) -> str:
        # A job row's clock measures the job, not the step: the launch call
        # returned in half a second, while the server keeps running for
        # minutes. The start is backdated to the job's own start so the time
        # matches `poll_shell_job`.
        if row.get("job_id") is not None and row.get("job_started_at") is not None:
            try:
                started = float(row["job_started_at"])
                finished = row.get("job_finished_at")
                end = now if finished is None else float(finished)
                return self._duration_text(end - started)
            except (TypeError, ValueError):
                pass
        started = row.get("started_at")
        if started is None:
            return ""
        finished = row.get("finished_at")
        end = finished if finished is not None else now
        try:
            return self._duration_text(end - float(started))
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _duration_text(seconds: float) -> str:
        """Hides a sub-second duration instead of printing `0s`.

        A quick call would otherwise wear `· 0s` in its title row, which reads
        like noise next to a real clock and says nothing about the step.
        """
        elapsed = max(0.0, seconds)
        return format_duration(elapsed) if elapsed >= 1.0 else ""

    def _format_step_detail(self, msg: dict, row: dict, meta: str) -> str:
        parts = [
            self._format_args(msg, row, meta),
            self._format_output(msg, row, meta),
        ]
        detail_html = row.get("detail_html")
        if detail_html:
            parts.append(f'<div style="margin-top: 6px;">{detail_html}</div>')
        return (f'<div style="margin: 2px 0 0 14px;">'
                f'{"".join(part for part in parts if part)}</div>')

    def _row_action(self, msg_id, row: dict, action: str) -> str:
        row_id = html.escape(str(row.get("id") or ""), quote=True)
        return f"activity:{msg_id}:{action}:{row_id}"

    def _format_args(self, msg: dict, row: dict, meta: str) -> str:
        """Collapsed argument peek, never open on its own.

        The step title already names the action, and a written file or an edit
        diff is rendered below as its own block, so the raw call is a
        disclosure for the reader who asks for it - not part of the answer.
        An empty payload (a call whose whole input is its title) is not
        rendered at all.
        """
        args_text = str(row.get("args") or "").strip()
        if not args_text or row.get("detail_html"):
            return ""
        expanded = bool(row.get("args_expanded"))
        caret = "&#9662;" if expanded else "&#9656;"
        peek = ""
        if not expanded:
            peek = (
                f'&nbsp;<span style="color: {meta}; font-weight: normal; '
                f'font-family: Consolas, monospace; font-size: 90%;">'
                f"{html.escape(_one_line(args_text, _ARGS_PREVIEW_CHARS))}</span>")
        body = self._pre(_clip(args_text, _ARGS_LIMIT)) if expanded else ""
        # The peek is the call itself, so it stays outside the toggle link:
        # the link handles the click, the payload stays selectable text.
        return (
            f'<div style="margin-top: 4px;">'
            f'<a href="{self._row_action(msg.get("id"), row, "args")}" '
            f'style="text-decoration: none; color: {meta}; font-size: 90%;">'
            f"{caret}&nbsp;Arguments</a>{peek}{body}</div>")

    @staticmethod
    def _is_long_output(text: str) -> bool:
        return (len(text.splitlines()) > _OUTPUT_PREVIEW_LINES
                or len(text) > _OUTPUT_PREVIEW_CHARS)

    @staticmethod
    def _output_preview(text: str) -> str:
        head = "\n".join(text.splitlines()[:_OUTPUT_PREVIEW_LINES])
        return head[:_OUTPUT_PREVIEW_CHARS]

    def _format_output(self, msg: dict, row: dict, meta: str) -> str:
        """Step result, clamped to a readable preview with a way to see it all."""
        text = _clip(str(row.get("output") or "").strip(), _TEXT_LIMIT)
        if not text:
            return ""
        long_output = self._is_long_output(text)
        opened = bool(row.get("output_expanded"))
        shown = text if (opened or not long_output) else self._output_preview(text)
        footer = ""
        if long_output:
            if opened:
                label = "Show less"
            else:
                hidden_chars = len(text) - len(shown)
                hidden_lines = len(text.splitlines()) - len(shown.splitlines())
                unit = (f"{hidden_lines} more lines" if hidden_lines > 1
                        else f"{hidden_chars} more characters")
                label = f"Show all · {unit}"
            footer = (
                f'<div style="margin-top: 2px;">'
                f'<a href="{self._row_action(msg.get("id"), row, "output")}" '
                f'style="text-decoration: none; color: '
                f'{self._get_style("COLLAPSIBLE_TITLE")}; font-size: 90%;">'
                f"{html.escape(label)}</a></div>")
        return (
            f'<div style="margin-top: 4px;">'
            f'<div style="color: {meta}; font-size: 90%;">Output</div>'
            f"{self._pre(shown)}{footer}</div>")

    def _pre(self, text: str) -> str:
        # Table cell instead of pre styling: Qt ignores a pre's background,
        # border and padding, leaving bare text on the card.
        #
        # Command output arrives with CRLF line endings, and Qt's pre-wrap
        # renders a lone \r as its own break on top of the \n, so every line
        # of shell output was drawn with a blank line under it. Normalizing to
        # \n (and a slightly tighter line-height) gives a compact block
        # instead of one that spends half its height on nothing.
        text = str(text).replace("\r\n", "\n").replace("\r", "\n")
        return (
            f'<table width="100%" cellspacing="0" cellpadding="8" '
            f'bgcolor="{self._get_style("CODE_BG")}" '
            f'style="border: 1px solid {self._get_style("CODE_BORDER")}; '
            f'margin-top: 2px;"><tr><td>'
            f'<pre style="margin: 0; color: '
            f'{self._get_style("TERMINAL_PRE_COLOR")}; '
            f'white-space: pre-wrap; word-wrap: break-word; '
            f'line-height: 95%;">'
            f"<code>{html.escape(text)}</code></pre></td></tr></table>")

    def _job_actions(self, msg: dict, row: dict) -> str:
        job_id = row.get("job_id")
        log_path = row.get("log_path")
        links = []
        # A command still waiting in the foreground can be handed to the
        # background by the user (Ctrl+B does the same): the shell returns a
        # job handle at once, so the agent stops waiting for it.
        if (job_id is None and row.get("status") == "running"
                and row.get("name") == "execute_shell"):
            links.append(
                f'<a href="activity:{msg.get("id")}:detach:{row.get("id")}" '
                f'style="color: {self._get_style("COLLAPSIBLE_TITLE")}; '
                'text-decoration: none; font-weight: 600;">'
                "Move to background</a>")
        # Stop is offered only while the launch step is still live; once the
        # job ends the row is marked ok/error and the link disappears.
        if job_id is not None and row.get("status") == "running":
            try:
                job_id = int(job_id)
            except (TypeError, ValueError):
                job_id = None
            if job_id is not None:
                links.append(
                    f'<a href="activity:{msg.get("id")}:job:{job_id}" '
                    f'style="color: {self._get_style("COLLAPSIBLE_TITLE")}; '
                    'text-decoration: none; font-weight: 600;">Stop job</a>')
        if log_path:
            from urllib.parse import quote
            safe_path = quote(str(log_path), safe="")
            links.append(
                f'<a href="activity:{msg.get("id")}:log:{safe_path}" '
                f'style="color: {self._get_style("COLLAPSIBLE_TITLE")}; '
                'text-decoration: none; font-weight: 600;">Open log</a>')
        if not links:
            return ""
        return ('<div style="margin: 4px 0 0 14px; font-size: 90%;">'
                + "&nbsp;&nbsp;&middot;&nbsp;&nbsp;".join(links)
                + "</div>")
