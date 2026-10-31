# src/ui/widgets/activity_block_formatter.py
"""HTML renderer for the per-turn activity block.

One block groups a turn's plan, model thoughts, narration and tool steps so
the transcript reads as a timeline instead of a wall of separate cards.
Rows keep their own expanded state; the formatter never mutates the message.
"""
from __future__ import annotations

import html
import time

_TEXT_LIMIT = 30000
_ARGS_LIMIT = 12000

_STATUS_ICONS = {
    "running": ("", "COLLAPSIBLE_META"),
    "ok": ("&#10003;", "SUCCESS_COLOR"),
    "error": ("&#10007;", "ERROR_COLOR"),
    "denied": ("!", "WARNING_COLOR"),
    "stopped": ("&#9632;", "COLLAPSIBLE_META"),
}

_PLAN_STATUS = {
    "done": ("&#10003;", "SUCCESS_COLOR"),
    "in_progress": ("&#9679;", "COLLAPSIBLE_TITLE"),
    "pending": ("&#9675;", "COLLAPSIBLE_META"),
}

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


class ActivityBlockFormatter:
    """Renders one activity block message into themed HTML."""

    def __init__(self, theme_manager):
        self.theme_manager = theme_manager

    def _get_style(self, key: str) -> str:
        try:
            return self.theme_manager.get_style(key) or ""
        except Exception:
            return ""

    def format_block(self, msg: dict, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        meta = self._get_style("COLLAPSIBLE_META")
        title_color = self._get_style("COLLAPSIBLE_TITLE")
        status = str(msg.get("status") or "running")
        if status == "error":
            title_color = self._get_style("ERROR_COLOR")
        elif status == "waiting":
            title_color = self._get_style("WARNING_COLOR")

        expanded = bool(msg.get("expanded", True))
        caret = "&#9662;" if expanded else "&#9656;"
        title = html.escape(str(msg.get("title") or "Working"))
        elapsed = self._elapsed(msg, now)
        elapsed_html = (
            f'&nbsp;<span style="color: {meta}; font-weight: normal;">'
            f"&middot; {elapsed}</span>" if elapsed else "")
        # A live, expanded block moves its elapsed time to a footer: the
        # header scrolls away in a long turn, while the footer sits at the
        # live edge the chat follows. Collapsed blocks and finished turns
        # keep the time in the header.
        use_footer = msg.get("finished_at") is None and expanded
        header = (
            f'<div style="font-weight: bold;">'
            f'<a href="activity:{msg.get("id")}:toggle" '
            f'style="text-decoration: none; color: {title_color};">'
            f"{caret}&nbsp;{title}{'' if use_footer else elapsed_html}</a></div>")

        body = ""
        if expanded:
            rows = msg.get("rows") or []
            # The plan is the live progress checklist, so it belongs at the
            # bottom of the block where the chat follows: pinned at the top
            # it scrolled out of view in a long turn (same reason as the
            # elapsed footer).
            visible = [r for r in rows if r.get("kind") != "plan"]
            plans = [r for r in rows if r.get("kind") == "plan"]
            body = self._format_body(msg, visible, meta, now)
            body += "".join(self._format_row(msg, row, meta, now)
                            for row in plans)
            if body:
                # Qt's document height ignores a trailing table's bottom
                # border/cell padding (and block bottom margins), so a last
                # step detail used to be clipped by the card edge. The
                # spacer restores the missing strip plus bottom padding.
                body += '<div style="font-size: 12px; margin: 0;">&nbsp;</div>'
        footer = ""
        if use_footer:
            footer = (
                f'<div style="margin-top: 8px; color: {meta}; font-size: 92%;">'
                f"{title}{elapsed_html}</div>")

        # The card's fill, border and accent are painted by ChatWidget:
        # Qt's rich text does not propagate a div background into nested
        # block rows, which left striped gaps under anchors. Outer spacing
        # must be margins: Qt ignores CSS padding on blocks.
        return f"""
        <div style="margin: 10px 12px 10px 15px;">
            {header}
            {body}
            {footer}
        </div>
        """


    def _elapsed(self, msg: dict, now: float) -> str:
        started = msg.get("started_at")
        if started is None:
            return ""
        finished = msg.get("finished_at")
        end = finished if finished is not None else now
        try:
            seconds = end - float(started)
        except (TypeError, ValueError):
            return ""
        if seconds < 1 and finished is not None:
            return ""
        return format_duration(seconds)

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
        if kind == "plan":
            return self._format_plan(msg, row, meta)
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
        icon, color_key = _STATUS_ICONS.get(status, _STATUS_ICONS["running"])
        color = self._get_style(color_key)
        if status == "running":
            icon = "&#9679;"
            color = self._get_style("COLLAPSIBLE_TITLE")
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
        icon_html = (
            f'&nbsp;<span style="color: {color}; font-weight: 600;">{icon}</span>'
            if icon else "")
        detail = ""
        if expanded:
            inner = "".join(self._format_row(msg, row, meta, now)
                            for row in run_rows)
            detail = f'<div style="margin: 2px 0 0 14px;">{inner}</div>'
        run_id_html = html.escape(run_id, quote=True)
        return (
            f'<div style="margin-top: 6px;">'
            f'<a href="activity:{msg.get("id")}:run:{run_id_html}" '
            f'style="text-decoration: none; color: '
            f'{self._get_style("COLLAPSIBLE_TITLE")};">'
            f"{caret}&nbsp;<b>{title}</b>{suffix}{icon_html}</a>{detail}</div>")

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

    def _format_thought(self, msg: dict, row: dict, meta: str) -> str:
        expanded = bool(row.get("expanded"))
        caret = "&#9662;" if expanded else "&#9656;"
        text = str(row.get("text") or "")
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        excerpt = html.escape(first[:140])
        if len(first) > 140:
            excerpt += "…"
        excerpt_html = (
            f'&nbsp;<span style="color: {meta}; font-weight: normal;">'
            f"{excerpt}</span>" if excerpt else "")
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

    def _format_plan(self, msg: dict, row: dict, meta: str) -> str:
        steps = row.get("steps") or []
        expanded = bool(row.get("expanded", True))
        caret = "&#9662;" if expanded else "&#9656;"
        done = sum(1 for s in steps if s.get("status") == "done")
        counter = (f'&nbsp;<span style="color: {meta}; font-weight: normal;">'
                   f"&middot; {done}/{len(steps)}</span>" if steps else "")
        body = ""
        if expanded and steps:
            lines = []
            for step in steps:
                status = str(step.get("status") or "pending")
                icon, color_key = _PLAN_STATUS.get(status, _PLAN_STATUS["pending"])
                color = self._get_style(color_key)
                strike = "text-decoration: line-through;" if status == "done" else ""
                text = html.escape(str(step.get("text") or ""))
                lines.append(
                    f'<div style="margin-top: 2px; color: {color}; {strike}">'
                    f"{icon}&nbsp; {text}</div>")
            body = f'<div style="margin-top: 4px;">{"".join(lines)}</div>'
        # A one-cell table, not a div: Qt paints a div's background only on
        # its own line and ignores padding there, so the plan came out as a
        # striped band. Cell background and padding render solidly.
        return (
            f'<table width="100%" cellspacing="0" cellpadding="8" '
            f'bgcolor="{self._get_style("CODE_BG")}" '
            f'style="border: 1px solid {self._get_style("COLLAPSIBLE_BORDER")}; '
            f'margin-top: 6px;"><tr><td>'
            f'<a href="{self._row_toggle(msg.get("id"), row)}" '
            f'style="text-decoration: none; color: '
            f'{self._get_style("COLLAPSIBLE_TITLE")}; font-weight: 600;">'
            f"{caret}&nbsp;Plan{counter}</a>{body}</td></tr></table>")

    def _format_step(self, msg: dict, row: dict, meta: str, now: float) -> str:
        expanded = bool(row.get("expanded"))
        caret = "&#9662;" if expanded else "&#9656;"
        status = str(row.get("status") or "running")
        icon, color_key = _STATUS_ICONS.get(status, _STATUS_ICONS["running"])
        color = self._get_style(color_key)
        title = html.escape(str(row.get("title") or "Step"))
        duration = self._step_duration(row, now)
        suffix = ""
        if duration:
            suffix += (f'&nbsp;<span style="color: {meta}; font-weight: normal;">'
                       f"&middot; {duration}</span>")
        if icon:
            suffix += (f'&nbsp;<span style="color: {color}; font-weight: 600;">'
                       f"{icon}</span>")
        detail = ""
        if expanded:
            detail = self._format_step_detail(msg, row, meta)
        # Job actions stay outside the collapsible body: Stop and Open log
        # must be reachable without expanding the step first.
        actions = self._job_actions(msg, row)
        return (
            f'<div style="margin-top: 6px;">'
            f'<a href="{self._row_toggle(msg.get("id"), row)}" '
            f'style="text-decoration: none; color: '
            f'{self._get_style("COLLAPSIBLE_TITLE")};">'
            f"{caret}&nbsp;{title}{suffix}</a>{detail}{actions}</div>")

    def _step_duration(self, row: dict, now: float) -> str:
        started = row.get("started_at")
        if started is None:
            return ""
        finished = row.get("finished_at")
        end = finished if finished is not None else now
        try:
            return format_duration(end - float(started))
        except (TypeError, ValueError):
            return ""

    def _format_step_detail(self, msg: dict, row: dict, meta: str) -> str:
        parts = []
        args_text = str(row.get("args") or "").strip()
        if args_text:
            parts.append(
                f'<div style="color: {meta}; font-size: 90%; '
                f'margin-top: 4px;">Arguments</div>'
                f"{self._pre(_clip(args_text, _ARGS_LIMIT))}")
        output_text = str(row.get("output") or "").strip()
        if output_text:
            parts.append(
                f'<div style="color: {meta}; font-size: 90%; '
                f'margin-top: 4px;">Output</div>'
                f"{self._pre(_clip(output_text, _TEXT_LIMIT))}")
        detail_html = row.get("detail_html")
        if detail_html:
            parts.append(f'<div style="margin-top: 6px;">{detail_html}</div>')
        return (f'<div style="margin: 2px 0 0 14px;">'
                f'{"".join(part for part in parts if part)}</div>')

    def _pre(self, text: str) -> str:
        # Table cell instead of pre styling: Qt ignores a pre's background,
        # border and padding, leaving bare text on the card.
        return (
            f'<table width="100%" cellspacing="0" cellpadding="8" '
            f'bgcolor="{self._get_style("CODE_BG")}" '
            f'style="border: 1px solid {self._get_style("CODE_BORDER")}; '
            f'margin-top: 2px;"><tr><td>'
            f'<pre style="margin: 0; color: '
            f'{self._get_style("TERMINAL_PRE_COLOR")}; '
            f'white-space: pre-wrap; word-wrap: break-word;">'
            f"<code>{html.escape(text)}</code></pre></td></tr></table>")

    def _job_actions(self, msg: dict, row: dict) -> str:
        job_id = row.get("job_id")
        log_path = row.get("log_path")
        if job_id is None and not log_path:
            return ""
        links = []
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
