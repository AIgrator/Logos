"""Pinned strip of running background shell jobs above the composer.

A background job outlives the step that launched it: the turn moves on, its
activity block scrolls away or folds, and the user loses the only row that can
stop the server. This strip keeps the jobs reachable next to the composer.

One job is one row (dot, command, ticking time, Stop / Open log, hover card
with the folder, the readiness URL and the log tail). Two or more collapse
into a single capsule - `2 jobs · npm run dev, python -m http.server` - whose
click opens a list with one line per job (state dot, command, time, Stop,
Open log). The capsule keeps the composer from growing a row per background
process. A job that ends while the list is open stays in it until the list is
closed, so a Stop's outcome is visible instead of vanishing under the cursor.

The strip never keeps a finished job: unheld rows are dropped as soon as the
handler applies the final job result, and the held ones go with the list.
"""
from __future__ import annotations

import html
import os
import time

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui.widgets.activity_block_formatter import next_second_delay_ms

_LOG_TAIL_LINES = 10
_LOG_TAIL_BYTES = 8192
# The capsule must stay one compact line in the composer lane; the full command
# lives in the list, the tooltip and the transcript step.
_CAPSULE_COMMAND_CHARS = 18
_LIST_COMMAND_CHARS = 40
_CAPSULE_TEXT_CHARS = 46


def format_job_time(seconds) -> str:
    """Compact elapsed text: 42s, 2m 07s, 1h 04m."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _one_line(text: str, limit: int = 64) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _job_count_text(count: int) -> str:
    return f"{count} job" if count == 1 else f"{count} jobs"


def _tail_text(path: str) -> str:
    """Last lines of a job log; '' when it cannot be read."""
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > _LOG_TAIL_BYTES:
                handle.seek(size - _LOG_TAIL_BYTES)
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines[-_LOG_TAIL_LINES:])


def _state_color(style, state: str) -> str:
    if state == "ready":
        return style("SUCCESS_COLOR")
    if state in ("failed", "error"):
        return style("ERROR_COLOR")
    return style("COLLAPSIBLE_TITLE")


class _JobPopover(QFrame):
    """Hover card with the job's details and log tail; never takes focus."""

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._theme = theme_manager
        self.setMinimumWidth(320)
        self.setMaximumWidth(620)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(0)
        self._label = QLabel(self)
        self._label.setTextFormat(Qt.RichText)
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)
        self._apply_theme()

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"QFrame {{ background-color: {self._style('CODE_BG')}; "
            f"border: 1px solid {self._style('CODE_BORDER')}; "
            "border-radius: 8px; }"
            " QLabel { background: transparent; border: none; }")

    def refresh_theme(self) -> None:
        self._apply_theme()

    def show_job(self, job: dict, elapsed_text: str, anchor: QWidget) -> None:
        meta = self._style("COLLAPSIBLE_META")
        title = self._style("COLLAPSIBLE_TITLE")
        state = str(job.get("state") or "running")
        color = _state_color(self._style, state)
        rows = []
        ready = str(job.get("ready_url") or "")
        if ready:
            code = job.get("status_code")
            rows.append(
                f'URL: <span style="color: {title};">{html.escape(ready)}</span>'
                + (f" (HTTP {html.escape(str(code))})" if code is not None else ""))
        if job.get("cwd"):
            rows.append(f"Folder: {html.escape(str(job['cwd']))}")
        if job.get("log_path"):
            rows.append(f"Log: {html.escape(str(job['log_path']))}")
        rows.append(
            f'State: <span style="color: {color};">{html.escape(state)}</span>'
            f" &middot; {html.escape(elapsed_text)}")
        tail = _tail_text(str(job.get("log_path") or ""))
        if tail:
            tail_html = (
                f'<div style="margin-top: 6px; color: {meta}; '
                f'font-family: Consolas, monospace; font-size: 11px;">'
                f"{html.escape(tail).replace(chr(10), '<br>')}</div>")
        else:
            tail_html = (f'<div style="margin-top: 6px; color: {meta}; '
                         f'font-size: 11px;">(no log output yet)</div>')
        command = html.escape(str(job.get("command") or "shell job"))
        self._label.setText(
            f'<div style="color: {title}; font-weight: 600;">'
            f"job #{html.escape(str(job.get('job_id', '?')))}"
            f" &middot; {command}</div>"
            f'<div style="color: {meta}; font-size: 11px; margin-top: 2px;">'
            + "<br>".join(rows)
            + "</div>" + tail_html)
        self.adjustSize()
        _place_above(self, anchor)


def _place_above(widget: QWidget, anchor: QWidget) -> None:
    """Puts a popup just above its anchor, clamped to the screen."""
    origin = anchor.mapToGlobal(QPoint(0, 0))
    size = widget.size()
    screen = QGuiApplication.screenAt(origin) or QGuiApplication.primaryScreen()
    available = screen.availableGeometry()
    x = max(available.left() + 8,
            min(origin.x(), available.right() - size.width() - 8))
    y = origin.y() - size.height() - 8
    if y < available.top() + 8:
        y = origin.y() + anchor.height() + 8
    widget.move(QPoint(x, y))


class _JobRow(QFrame):
    """One job line: dot, command, live time, Stop / Open log."""

    stop_requested = Signal(int)
    open_log_requested = Signal(str)
    hovered = Signal(object)
    unhovered = Signal()

    def __init__(self, job: dict, theme_manager, parent=None):
        super().__init__(parent)
        self._job = dict(job)
        self._theme = theme_manager
        self.setObjectName("JobRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 3, 10, 3)
        row.setSpacing(8)
        self._dot = QLabel("\u25cf", self)
        self._dot.setFixedWidth(10)
        self._summary = QLabel("", self)
        self._summary.setTextFormat(Qt.RichText)
        self._elapsed = QLabel("", self)
        self._elapsed.setStyleSheet("font-family: Consolas, monospace;")
        self._actions = QLabel("", self)
        self._actions.setTextFormat(Qt.RichText)
        self._actions.linkActivated.connect(self._on_link)
        row.addWidget(self._dot)
        row.addWidget(self._summary)
        row.addWidget(self._elapsed)
        row.addWidget(self._actions)
        self._apply_theme()

    def job(self) -> dict:
        return self._job

    def set_job(self, job: dict) -> None:
        self._job = dict(job)
        self._apply_theme()

    def set_elapsed(self, seconds) -> None:
        self._elapsed.setText(format_job_time(seconds))

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def _apply_theme(self) -> None:
        meta = self._style("COLLAPSIBLE_META")
        title = self._style("COLLAPSIBLE_TITLE")
        background = self._style("CODE_BG")
        border = self._style("CODE_BORDER")
        self.setStyleSheet(
            f"QFrame#JobRow {{ background-color: {background}; "
            f"border: 1px solid {border}; border-radius: 8px; }}"
            " QLabel { background: transparent; border: none; }")
        self._dot.setStyleSheet(
            f"color: {_state_color(self._style, str(self._job.get('state') or 'running'))}; "
            "font-size: 10px;")
        self._summary.setStyleSheet(f"color: {title}; font-size: 12px;")
        self._elapsed.setStyleSheet(
            f"color: {meta}; font-size: 11px; font-family: Consolas, monospace;")
        self._actions.setStyleSheet(f"font-size: 11px; color: {meta};")
        self._summary.setText(
            f'job #{self._job.get("job_id", "?")} &middot; '
            f'{html.escape(_one_line(self._job.get("command") or "shell job"))}')
        links = []
        if self._job.get("status") != "stopping":
            links.append(f'<a href="stop" style="color: {title}; '
                         "text-decoration: none; font-weight: 600;\">Stop</a>")
        if self._job.get("log_path"):
            links.append(f'<a href="log" style="color: {title}; '
                         "text-decoration: none; font-weight: 600;\">Open log</a>")
        self._actions.setText(" &middot; ".join(links))

    def _on_link(self, href: str) -> None:
        if href == "stop":
            try:
                self.stop_requested.emit(int(self._job.get("job_id")))
            except (TypeError, ValueError):
                pass
        elif href == "log" and self._job.get("log_path"):
            self.open_log_requested.emit(str(self._job["log_path"]))

    def enterEvent(self, event):
        self.hovered.emit(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.unhovered.emit()
        super().leaveEvent(event)


class _JobCapsule(QFrame):
    """Several jobs as one line: `2 jobs · npm run dev, http.server ▾`."""

    clicked = Signal()

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self.setObjectName("JobCapsule")
        self.setCursor(Qt.PointingHandCursor)
        self._theme = theme_manager
        self._jobs: list[dict] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 3, 10, 3)
        row.setSpacing(8)
        self._dot = QLabel("\u25cf", self)
        self._dot.setFixedWidth(10)
        self._summary = QLabel("", self)
        self._caret = QLabel("\u25be", self)
        for label in (self._dot, self._summary, self._caret):
            # Clicks belong to the capsule frame, not its labels.
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(self._dot)
        row.addWidget(self._summary)
        row.addWidget(self._caret)
        self._apply_theme()

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def _apply_theme(self) -> None:
        meta = self._style("COLLAPSIBLE_META")
        title = self._style("COLLAPSIBLE_TITLE")
        background = self._style("CODE_BG")
        border = self._style("CODE_BORDER")
        self.setStyleSheet(
            f"QFrame#JobCapsule {{ background-color: {background}; "
            f"border: 1px solid {border}; border-radius: 8px; }}"
            f"QFrame#JobCapsule:hover {{ border: 1px solid {title}; }}"
            " QLabel { background: transparent; border: none; }")
        self._summary.setStyleSheet(f"color: {title}; font-size: 12px;")
        self._caret.setStyleSheet(f"color: {meta}; font-size: 10px;")
        self._dot.setStyleSheet(
            f"color: {self._state_color()}; font-size: 10px;")

    def _state_color(self) -> str:
        states = [str(job.get("state") or "running") for job in self._jobs]
        if any(state in ("failed", "error") for state in states):
            return self._style("ERROR_COLOR")
        if any(state == "ready" for state in states):
            return self._style("SUCCESS_COLOR")
        return self._style("COLLAPSIBLE_TITLE")

    def set_jobs(self, jobs, count: int, opened: bool = False) -> None:
        self._jobs = list(jobs)
        names = [_one_line(job.get("command") or "shell job",
                           _CAPSULE_COMMAND_CHARS) for job in jobs]
        text = _job_count_text(count)
        if names:
            text += " \u00b7 " + ", ".join(names)
        self._summary.setText(_one_line(text, _CAPSULE_TEXT_CHARS))
        self._caret.setText("\u25b4" if opened else "\u25be")
        self._dot.setStyleSheet(
            f"color: {self._state_color()}; font-size: 10px;")
        self.setToolTip("\n".join(
            f"job #{job.get('job_id', '?')} \u00b7 {job.get('command') or 'shell job'}"
            for job in jobs))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _JobListRow(QWidget):
    """One job inside the capsule's list: dot, command, time, Stop / Open log."""

    stop_requested = Signal(int)
    open_log_requested = Signal(str)

    def __init__(self, entry: dict, theme_manager, parent=None):
        super().__init__(parent)
        self._entry = dict(entry)
        self._theme = theme_manager
        self.setObjectName("JobListRow")
        # A plain QWidget paints a stylesheet background only when asked to.
        self.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 3, 6, 3)
        row.setSpacing(8)
        self._dot = QLabel("\u25cf", self)
        self._dot.setFixedWidth(10)
        self._name = QLabel("", self)
        self._name.setMaximumWidth(300)
        self._elapsed = QLabel("", self)
        self._actions = QLabel("", self)
        self._actions.setTextFormat(Qt.RichText)
        self._actions.linkActivated.connect(self._on_link)
        row.addWidget(self._dot)
        row.addWidget(self._name, 1)
        row.addWidget(self._elapsed)
        row.addWidget(self._actions)
        self._apply()

    def job_id(self):
        return self._entry.get("job_id")

    def set_entry(self, entry: dict) -> None:
        self._entry = dict(entry)
        self._apply()

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def _apply(self) -> None:
        meta = self._style("COLLAPSIBLE_META")
        title = self._style("COLLAPSIBLE_TITLE")
        live = bool(self._entry.get("live", True))
        state = str(self._entry.get("state") or "running")
        color = _state_color(self._style, state) if live else meta
        self._dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        self._name.setStyleSheet(
            f"color: {title if live else meta}; font-size: 12px;")
        self._elapsed.setStyleSheet(
            f"color: {meta}; font-size: 11px; font-family: Consolas, monospace;")
        self._actions.setStyleSheet(f"font-size: 11px; color: {meta};")
        command = _one_line(self._entry.get("command") or "shell job",
                            _LIST_COMMAND_CHARS)
        self._name.setText(
            f"job #{self._entry.get('job_id', '?')} \u00b7 {command}")
        self._elapsed.setText(str(self._entry.get("elapsed_text") or ""))
        links = []
        if live and self._entry.get("status") != "stopping":
            links.append(f'<a href="stop" style="color: {title}; '
                         "text-decoration: none; font-weight: 600;\">Stop</a>")
        if self._entry.get("log_path"):
            links.append(f'<a href="log" style="color: {title}; '
                         "text-decoration: none; font-weight: 600;\">Open log</a>")
        self._actions.setText(" &middot; ".join(links))
        self.setToolTip(self._tooltip())

    def _tooltip(self) -> str:
        lines = ["job #" + str(self._entry.get("job_id", "?"))]
        if self._entry.get("command"):
            lines.append(str(self._entry["command"]))
        if not self._entry.get("live", True):
            lines.append("finished")
        ready = str(self._entry.get("ready_url") or "")
        if ready:
            code = self._entry.get("status_code")
            lines.append(ready + (f" (HTTP {code})" if code is not None else ""))
        if self._entry.get("cwd"):
            lines.append(str(self._entry["cwd"]))
        return "\n".join(lines)

    def _on_link(self, href: str) -> None:
        if href == "stop":
            try:
                self.stop_requested.emit(int(self._entry.get("job_id")))
            except (TypeError, ValueError):
                pass
        elif href == "log" and self._entry.get("log_path"):
            self.open_log_requested.emit(str(self._entry["log_path"]))


class _JobListPopover(QFrame):
    """Popup list of every pinned job, opened by the capsule."""

    stop_requested = Signal(int)
    open_log_requested = Signal(str)
    closed = Signal()

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("JobListPopover")
        self._theme = theme_manager
        self._anchor: QWidget | None = None
        self._rows: dict[int, _JobListRow] = {}
        self._order: list[int] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(2)
        self.setMinimumWidth(380)
        self.setMaximumWidth(680)
        self._apply_theme()

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"QFrame#JobListPopover {{ background-color: {self._style('CODE_BG')}; "
            f"border: 1px solid {self._style('CODE_BORDER')}; "
            "border-radius: 8px; }"
            " QWidget#JobListRow { background: transparent; border-radius: 5px; }"
            " QWidget#JobListRow:hover { background-color: "
            f"{self._style('CODE_BORDER')}; }}"
            " QLabel { background: transparent; border: none; }")

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def refresh_theme(self) -> None:
        self._apply_theme()
        for row in self._rows.values():
            row._apply()

    def show_for(self, entries: list[dict], anchor: QWidget) -> None:
        self._anchor = anchor
        self.update_entries(entries)
        self.adjustSize()
        _place_above(self, anchor)
        self.show()
        self.raise_()

    def set_anchor(self, anchor: QWidget) -> None:
        self._anchor = anchor

    def update_entries(self, entries: list[dict]) -> None:
        ids = [entry.get("job_id") for entry in entries]
        if ids != self._order:
            self._rebuild(entries)
        else:
            for entry in entries:
                row = self._rows.get(entry.get("job_id"))
                if row is not None:
                    row.set_entry(entry)
        self.adjustSize()
        if self.isVisible() and self._anchor is not None:
            _place_above(self, self._anchor)

    def _rebuild(self, entries: list[dict]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = {}
        self._order = []
        for entry in entries:
            row = _JobListRow(entry, self._theme, self)
            row.stop_requested.connect(self.stop_requested.emit)
            row.open_log_requested.connect(self.open_log_requested.emit)
            self._layout.addWidget(row)
            self._rows[entry.get("job_id")] = row
            self._order.append(entry.get("job_id"))

    def hideEvent(self, event):
        super().hideEvent(event)
        self.closed.emit()


class JobPanel(QWidget):
    """Compact, always-visible strip of live background jobs."""

    job_stop_requested = Signal(int)

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self._theme = theme_manager
        self._jobs: dict[int, dict] = {}
        # Jobs that ended while the capsule's list was open stay listed until
        # it closes, so the Stop's outcome does not vanish under the cursor.
        self._held: dict[int, dict] = {}
        self._suppressed = False
        self._mode = "none"
        self._row: _JobRow | None = None
        self._capsule: _JobCapsule | None = None
        self._hover: _JobPopover | None = None
        self._hover_job: int | None = None
        self._list: _JobListPopover | None = None
        self._list_open = False
        self._syncing = False
        self._sync_pending = False
        self._in_hide = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.setVisible(False)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 3, 0, 1)
        outer.setSpacing(0)
        self._column = QWidget(self)
        self._column_layout = QVBoxLayout(self._column)
        self._column_layout.setContentsMargins(0, 0, 0, 0)
        self._column_layout.setSpacing(3)
        outer.addStretch(1)
        outer.addWidget(self._column)
        outer.addStretch(1)

        # One clock for the strip: single shot and re-armed on the wall-clock
        # second so it agrees with the transcript's step clock instead of
        # drifting a second away from it.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

        try:
            self._theme.theme_changed.connect(self._on_theme_changed)
        except Exception:
            pass

    # -- public API ---------------------------------------------------------

    def track_job(self, job: dict) -> None:
        """Adds or refreshes one live job; visible until ``finish_job``."""
        try:
            job_id = int(job.get("job_id"))
        except (TypeError, ValueError):
            return
        entry = dict(job)
        entry["job_id"] = job_id
        entry.setdefault("started_at", time.monotonic())
        entry.setdefault("state", "running")
        entry.setdefault("status", "running")
        entry.pop("finished_at", None)
        self._jobs[job_id] = entry
        self._held.pop(job_id, None)
        self._sync()

    def update_job_state(self, job_id: int, state: str,
                         status: str | None = None) -> None:
        entry = None
        if job_id is not None:
            try:
                job_id = int(job_id)
            except (TypeError, ValueError):
                return
            entry = self._jobs.get(job_id) or self._held.get(job_id)
        if entry is None:
            return
        entry["state"] = str(state or entry.get("state"))
        if status:
            entry["status"] = status
        self._refresh()

    def finish_job(self, job_id: int) -> None:
        """Drops a job once its final result was applied."""
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            return
        entry = self._jobs.pop(job_id, None)
        if entry is None:
            held = self._held.get(job_id)
            if held is None:
                return
            held.setdefault("finished_at", time.monotonic())
            self._refresh()
            return
        entry["state"] = "finished"
        entry["status"] = "finished"
        entry["finished_at"] = time.monotonic()
        if self._list_open:
            self._held[job_id] = entry
        self._sync()

    def clear(self) -> None:
        self._jobs.clear()
        self._held.clear()
        self._close_list()
        self._hide_hover()
        self._sync()

    def has_jobs(self) -> bool:
        return bool(self._jobs)

    def set_suppressed(self, suppressed: bool) -> None:
        """Hides the strip without forgetting the jobs (compact mode)."""
        self._suppressed = bool(suppressed)
        if self._suppressed:
            self._close_list()
            self._hide_hover()
        self._sync()

    # -- rendering ----------------------------------------------------------

    def _job_count(self) -> int:
        return len(self._jobs) + len(self._held)

    def _mode_for(self) -> str:
        total = self._job_count()
        if total == 0:
            return "none"
        if total == 1 and not self._held:
            return "row"
        return "capsule"

    def _sync(self) -> None:
        # Hiding the panel or closing the list re-enters here (Qt signals);
        # a flat re-entrancy guard plus one more pass keeps that from nesting
        # half-applied state.
        if self._syncing:
            self._sync_pending = True
            return
        self._syncing = True
        try:
            self._sync_pending = True
            while self._sync_pending:
                self._sync_pending = False
                self._sync_once()
        finally:
            self._syncing = False

    def _sync_once(self) -> None:
        mode = self._mode_for()
        if mode == "none" and self._list_open:
            self._close_list()
            mode = self._mode_for()
        if mode != self._mode:
            self._drop_widgets()
            self._mode = mode
            self._build()
        self._refresh()
        self._sync_timer()
        self.setVisible(mode != "none" and not self._suppressed)

    def _drop_widgets(self) -> None:
        for widget in (self._row, self._capsule):
            if widget is not None:
                try:
                    self._column_layout.removeWidget(widget)
                except Exception:
                    pass
                widget.deleteLater()
        self._row = None
        self._capsule = None

    def _build(self) -> None:
        if self._mode == "row":
            job = next(iter(self._jobs.values()), None)
            if job is None:
                return
            row = _JobRow(job, self._theme, self._column)
            row.stop_requested.connect(self.job_stop_requested.emit)
            row.open_log_requested.connect(self._open_log)
            row.hovered.connect(self._show_hover)
            row.unhovered.connect(self._hide_hover)
            self._column_layout.addWidget(row)
            self._row = row
        elif self._mode == "capsule":
            capsule = _JobCapsule(self._theme, self._column)
            capsule.clicked.connect(self._toggle_list)
            self._column_layout.addWidget(capsule)
            self._capsule = capsule

    def _refresh(self) -> None:
        if self._mode == "row" and self._row is not None:
            job = next(iter(self._jobs.values()), None)
            if job is not None:
                self._row.set_job(job)
                self._row.set_elapsed(self._elapsed(job))
        elif self._mode == "capsule" and self._capsule is not None:
            self._capsule.set_jobs(list(self._jobs.values()),
                                   self._job_count(), self._list_open)
        if self._list_open and self._list is not None:
            # The capsule is rebuilt when the job set changes; the list must
            # follow the new anchor instead of a deleted widget.
            if self._capsule is not None:
                self._list.set_anchor(self._capsule)
            self._list.update_entries(self._entries())

    def _entries(self) -> list[dict]:
        entries = []
        for job in self._jobs.values():
            entry = dict(job)
            entry["live"] = True
            entry["elapsed_text"] = format_job_time(self._elapsed(job))
            entries.append(entry)
        for job in self._held.values():
            entry = dict(job)
            entry["live"] = False
            entry["elapsed_text"] = format_job_time(self._elapsed(job))
            entries.append(entry)
        return entries

    def _elapsed(self, job: dict) -> float:
        try:
            started = float(job.get("started_at"))
        except (TypeError, ValueError):
            return 0.0
        finished = job.get("finished_at")
        try:
            end = time.monotonic() if finished is None else float(finished)
        except (TypeError, ValueError):
            end = time.monotonic()
        return max(0.0, end - started)

    def _sync_timer(self) -> None:
        want = bool(self._jobs) and (self._mode == "row" or self._list_open)
        if want and not self._timer.isActive():
            self._timer.start(next_second_delay_ms())
        elif not want and self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        if self._mode == "row" and self._row is not None:
            job = next(iter(self._jobs.values()), None)
            if job is not None:
                self._row.set_elapsed(self._elapsed(job))
        if self._list_open and self._list is not None:
            self._list.update_entries(self._entries())
        self._sync_timer()

    # -- the capsule's list -------------------------------------------------

    def _toggle_list(self) -> None:
        if self._list_open:
            self._close_list()
            return
        if self._capsule is None:
            return
        if self._list is None:
            self._list = _JobListPopover(self._theme, self)
            self._list.stop_requested.connect(self.job_stop_requested.emit)
            self._list.open_log_requested.connect(self._open_log)
            self._list.closed.connect(self._on_list_closed)
        self._list_open = True
        self._list.show_for(self._entries(), self._capsule)
        self._refresh()
        self._sync_timer()

    def _close_list(self) -> None:
        self._list_open = False
        if self._list is not None:
            self._list.hide()

    def _on_list_closed(self) -> None:
        self._list_open = False
        self._held.clear()
        # Hiding the strip closes its list too; re-syncing there would show the
        # strip again inside its own hideEvent. The next show re-syncs anyway.
        if not self._in_hide:
            self._sync()

    def _open_log(self, path: str) -> None:
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_hover(self, row: _JobRow) -> None:
        job = row.job()
        job_id = job.get("job_id")
        if job_id not in self._jobs:
            return
        if self._hover is None:
            self._hover = _JobPopover(self._theme, self)
        self._hover_job = job_id
        self._hover.show_job(
            self._jobs[job_id], format_job_time(self._elapsed(self._jobs[job_id])),
            row)

    def _hide_hover(self) -> None:
        self._hover_job = None
        if self._hover is not None:
            self._hover.hide()

    def _on_theme_changed(self) -> None:
        if self._hover is not None:
            self._hover.refresh_theme()
        if self._list is not None:
            self._list.refresh_theme()
        if self._row is not None:
            self._row._apply_theme()
        if self._capsule is not None:
            self._capsule._apply_theme()
            self._capsule.set_jobs(list(self._jobs.values()),
                                   self._job_count(), self._list_open)

    def hideEvent(self, event):
        self._hide_hover()
        self._in_hide = True
        try:
            if self._list_open:
                self._close_list()
        finally:
            self._in_hide = False
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # A hidden strip (compact mode, minimized window) missed the refreshes;
        # rebuild its state before it is seen again.
        self._sync()
