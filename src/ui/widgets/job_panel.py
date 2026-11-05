"""Pinned strip of running background shell jobs above the composer.

A background job outlives the step that launched it: the turn moves on, its
activity block scrolls away or folds, and the user loses the only row that can
stop the server. This strip keeps one compact line per live job next to the
composer, with a ticking elapsed time, Stop / Open log, and a hover card that
shows the command, the folder, the readiness URL and the tail of the log.

The strip never survives a finished job: its row is dropped as soon as the
handler applies the final job result.
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

_LOG_TAIL_LINES = 10
_LOG_TAIL_BYTES = 8192


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
        color = self._state_color(state)
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
        tail_html = ""
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
        origin = anchor.mapToGlobal(QPoint(0, 0))
        size = self.size()
        screen = (QGuiApplication.screenAt(origin)
                  or QGuiApplication.primaryScreen())
        available = screen.availableGeometry()
        x = origin.x()
        x = max(available.left() + 8,
                min(x, available.right() - size.width() - 8))
        y = origin.y() - size.height() - 8
        if y < available.top() + 8:
            y = origin.y() + anchor.height() + 8
        self.move(QPoint(x, y))
        self.show()

    def _state_color(self, state: str) -> str:
        if state == "ready":
            return self._style("SUCCESS_COLOR")
        if state in ("failed", "error"):
            return self._style("ERROR_COLOR")
        return self._style("COLLAPSIBLE_TITLE")


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

    def set_elapsed(self, seconds) -> None:
        self._elapsed.setText(format_job_time(seconds))

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def _state_color(self) -> str:
        state = str(self._job.get("state") or "running")
        if state == "ready":
            return self._style("SUCCESS_COLOR")
        if state in ("failed", "error"):
            return self._style("ERROR_COLOR")
        return self._style("COLLAPSIBLE_TITLE")

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
            f"color: {self._state_color()}; font-size: 10px;")
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


class JobPanel(QWidget):
    """Compact, always-visible strip of live background jobs."""

    job_stop_requested = Signal(int)

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self._theme = theme_manager
        self._jobs: dict[int, dict] = {}
        self._rows: dict[int, _JobRow] = {}
        self._suppressed = False
        self._popover: _JobPopover | None = None
        self._popover_job: int | None = None
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

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
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
        self._jobs[job_id] = entry
        self._render()
        if not self._suppressed:
            self.setVisible(True)
        if not self._timer.isActive():
            self._timer.start()

    def update_job_state(self, job_id: int, state: str,
                         status: str | None = None) -> None:
        entry = self._jobs.get(int(job_id)) if job_id is not None else None
        if entry is None:
            return
        entry["state"] = str(state or entry.get("state"))
        if status:
            entry["status"] = status
        row = self._rows.get(int(job_id))
        if row is not None:
            row._job = dict(entry)
            row._apply_theme()

    def finish_job(self, job_id: int) -> None:
        """Drops a job line once its final result was applied."""
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            return
        if self._jobs.pop(job_id, None) is None:
            return
        if self._popover_job == job_id:
            self._hide_popover()
        self._render()
        if not self._jobs:
            self._timer.stop()
            self.setVisible(False)

    def clear(self) -> None:
        self._jobs.clear()
        self._rows.clear()
        self._hide_popover()
        self._render()
        self._timer.stop()
        self.setVisible(False)

    def has_jobs(self) -> bool:
        return bool(self._jobs)

    def set_suppressed(self, suppressed: bool) -> None:
        """Hides the strip without forgetting the jobs (compact mode)."""
        self._suppressed = bool(suppressed)
        if self._suppressed:
            self._hide_popover()
            self.setVisible(False)
        elif self._jobs:
            self.setVisible(True)

    # -- rendering ----------------------------------------------------------

    def _render(self) -> None:
        while self._column_layout.count():
            item = self._column_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = {}
        for job_id, job in list(self._jobs.items()):
            row = _JobRow(job, self._theme, self._column)
            row.stop_requested.connect(self.job_stop_requested.emit)
            row.open_log_requested.connect(self._open_log)
            row.hovered.connect(self._show_popover)
            row.unhovered.connect(self._hide_popover)
            row.set_elapsed(self._elapsed(job))
            self._column_layout.addWidget(row)
            self._rows[job_id] = row
        if not self._jobs:
            self.setVisible(False)

    def _elapsed(self, job: dict) -> float:
        try:
            return max(0.0, time.monotonic() - float(job.get("started_at")))
        except (TypeError, ValueError):
            return 0.0

    def _tick(self) -> None:
        if not self._jobs:
            self._timer.stop()
            return
        for job_id, job in list(self._jobs.items()):
            row = self._rows.get(job_id)
            if row is not None:
                row.set_elapsed(self._elapsed(job))
        if self._popover is not None and self._popover.isVisible() \
                and self._popover_job in self._jobs:
            anchor = self._rows.get(self._popover_job)
            if anchor is not None:
                self._popover.show_job(
                    self._jobs[self._popover_job],
                    format_job_time(self._elapsed(self._jobs[self._popover_job])),
                    anchor)

    def _open_log(self, path: str) -> None:
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_popover(self, row: _JobRow) -> None:
        job = row.job()
        job_id = job.get("job_id")
        if job_id not in self._jobs:
            return
        if self._popover is None:
            self._popover = _JobPopover(self._theme, self)
        self._popover_job = job_id
        self._popover.show_job(
            self._jobs[job_id], format_job_time(self._elapsed(self._jobs[job_id])),
            row)

    def _hide_popover(self) -> None:
        self._popover_job = None
        if self._popover is not None:
            self._popover.hide()

    def _on_theme_changed(self) -> None:
        if self._popover is not None:
            self._popover.refresh_theme()
        for job_id, row in self._rows.items():
            job = self._jobs.get(job_id)
            if job is not None:
                row._job = dict(job)
                row._apply_theme()

    def hideEvent(self, event):
        self._hide_popover()
        super().hideEvent(event)
