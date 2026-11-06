"""Hover card with the live output of one shell command.

The transcript is a single ``QTextDocument`` and cannot nest a scroll area, so
a step row can show only one grey progress line. A pinned strip above the
composer fixed that, but it also took a permanent slice of the chat for a
viewer nobody was reading. This card appears only while the pointer rests on
the task that produces the output - a background job row above the composer or
a running ``execute_shell`` step in the transcript - and leaves with the
pointer.

It is a ``Qt.Tool`` window so it can float over the transcript without a
taskbar entry, and ``WA_ShowWithoutActivating`` keeps the composer's focus.
Hiding is delayed and cancelled when the pointer enters the card, so the
reader can walk from the task into the output and scroll it.
"""
from __future__ import annotations

import html

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

# Eight lines is enough for a ping's replies or a build's warnings, and the
# card closes on its own as soon as the pointer leaves.
PREVIEW_LINES = 8
MIN_WIDTH = 400
MAX_WIDTH = 620
# Time to cross from the task into the card without the card vanishing first.
HIDE_DELAY_MS = 260
_MONO_FAMILIES = "Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', monospace"


def _one_line(text: str, limit: int = 64) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def format_elapsed(seconds) -> str:
    """Compact clock for the header: 7s, 1m 04s."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    return f"{minutes}m {sec:02d}s"


class OutputPreview(QWidget):
    """Floating, scrollable tail of one shell command's output."""

    stop_requested = Signal()
    job_stop_requested = Signal(int)
    open_log_requested = Signal(str)
    closed = Signal()

    def __init__(self, theme_manager, parent=None, lines: int = PREVIEW_LINES):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("OutputPreview")
        self._theme = theme_manager
        self._line_count = max(3, int(lines))
        self._job_id = None
        self._log_path = ""
        self._state = "running"
        # False while the command is still the running step (Stop ends the
        # turn); True when it is a background job (Stop ends the job, like the
        # row above the composer does).
        self._detached = False
        self._anchor = QRect()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._card = QFrame(self)
        self._card.setObjectName("OutputPreviewCard")
        outer.addWidget(self._card)

        body = QVBoxLayout(self._card)
        body.setContentsMargins(12, 8, 12, 9)
        body.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self._dot = QLabel("\u25cf", self._card)
        self._dot.setFixedWidth(10)
        self._title = QLabel("", self._card)
        self._elapsed = QLabel("", self._card)
        self._actions = QLabel("", self._card)
        self._actions.setTextFormat(Qt.RichText)
        self._actions.linkActivated.connect(self._on_link)
        header.addWidget(self._dot)
        header.addWidget(self._title, 1)
        header.addWidget(self._elapsed)
        header.addWidget(self._actions)
        body.addLayout(header)

        # One quiet line for the job's readiness URL or folder, hidden when the
        # card shows a step that has neither.
        self._hint = QLabel("", self._card)
        self._hint.setVisible(False)
        body.addWidget(self._hint)

        self._view = QPlainTextEdit(self._card)
        self._view.setObjectName("OutputPreviewView")
        self._view.setReadOnly(True)
        self._view.setUndoRedoEnabled(False)
        self._view.setFrameShape(QFrame.NoFrame)
        self._view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setMaximumBlockCount(4000)
        self._view.setFocusPolicy(Qt.ClickFocus)
        font = QFont()
        font.setFamilies(["Consolas", "Cascadia Mono", "DejaVu Sans Mono",
                          "Courier New"])
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(max(8, self.font().pointSize()))
        self._view.setFont(font)
        metrics = QFontMetrics(font)
        margin = int(self._view.document().documentMargin())
        # Exactly N readable lines: a sliver of line N+1 above the scrollbar
        # reads as a rendering bug.
        self._view.setFixedHeight(
            metrics.lineSpacing() * self._line_count + 2 * margin)
        body.addWidget(self._view)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_now)

        try:
            self._theme.theme_changed.connect(self.refresh_theme)
        except Exception:
            pass

        self.setFixedWidth(MIN_WIDTH)
        self.setVisible(False)

    # -- data in -----------------------------------------------------------

    def current_job_id(self):
        return self._job_id

    def is_open(self) -> bool:
        return not self.isHidden()

    def text(self) -> str:
        return self._view.toPlainText()

    def show_for(self, job: dict, anchor: QRect, hint: str = "",
                 detached: bool = False) -> bool:
        """Opens the card for a job dict from ``read_since``; False if invalid."""
        try:
            job_id = int(job.get("job_id"))
        except (TypeError, ValueError):
            return False
        self._job_id = job_id
        self._detached = bool(detached)
        self._log_path = str(job.get("log_path") or "")
        self._state = ("running" if job.get("running", True)
                       else str(job.get("state") or "done"))
        self._view.clear()
        self._title.setText(
            f"#{job_id} \u00b7 "
            f"{html.escape(_one_line(job.get('command') or 'shell command'))}")
        self._hint.setText(_one_line(hint, 96))
        self._hint.setVisible(bool(hint))
        self._elapsed.setText(format_elapsed(job.get("elapsed_seconds")))
        self._sync_actions()
        self._anchor = QRect(anchor)
        self._apply_width()
        self.adjustSize()
        self._place_above()
        self.show()
        self.raise_()
        self._hide_timer.stop()
        return True

    def feed(self, text: str = "", elapsed=None, state: str = "") -> None:
        """Appends newly printed output and refreshes the clock."""
        if text:
            self._append(str(text))
        if elapsed is not None:
            self._elapsed.setText(format_elapsed(elapsed))
        if state:
            self.set_state(state)

    def set_state(self, state: str) -> None:
        if state and state != self._state:
            self._state = str(state)
            self._sync_actions()

    def finish(self, state: str = "done") -> None:
        """Freezes the card on the last output instead of emptying it."""
        self.set_state(state)
        self._sync_actions()

    def set_detached(self, detached: bool = True) -> None:
        """The watched command became a background job of its own."""
        self._detached = bool(detached)
        self._sync_actions()

    def schedule_hide(self) -> None:
        """Hides soon, unless the pointer is already inside the card."""
        if not self.isVisible():
            self.hide_now()
            return
        if self._pointer_inside():
            return
        self._hide_timer.start(HIDE_DELAY_MS)

    def hide_now(self) -> None:
        self._hide_timer.stop()
        if self.isVisible():
            self.setVisible(False)

    def clear(self) -> None:
        self.hide_now()
        self._job_id = None
        self._log_path = ""
        self._state = "running"
        self._detached = False
        self._title.setText("")
        self._hint.setText("")
        self._hint.setVisible(False)
        self._elapsed.setText("")
        self._view.clear()

    def refresh_theme(self) -> None:
        self._apply_theme()

    # -- internals ---------------------------------------------------------

    def _pointer_inside(self) -> bool:
        if not self.isVisible():
            return False
        return self.rect().contains(self.mapFromGlobal(QCursor.pos()))

    def _append(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        bar = self._view.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        cursor = QTextCursor(self._view.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        if follow:
            bar.setValue(bar.maximum())

    def _on_link(self, href: str) -> None:
        if href == "stop":
            if self._detached and self._job_id is not None:
                self.job_stop_requested.emit(int(self._job_id))
            else:
                self.stop_requested.emit()
        elif href == "log" and self._log_path:
            self.open_log_requested.emit(self._log_path)

    def _sync_actions(self) -> None:
        title = self._style("COLLAPSIBLE_TITLE")
        links = []
        if self._state == "running":
            links.append(f'<a href="stop" style="color: {title}; '
                         'text-decoration: none; font-weight: 600;">Stop</a>')
        if self._log_path:
            links.append(f'<a href="log" style="color: {title}; '
                         'text-decoration: none; font-weight: 600;">Open log</a>')
        self._actions.setText(" &middot; ".join(links))
        self._dot.setStyleSheet(
            f"color: {self._state_color()}; font-size: 10px;")

    def _state_color(self) -> str:
        if self._state in ("failed", "error"):
            return self._style("ERROR_COLOR")
        if self._state == "stopped":
            return self._style("WARNING_COLOR")
        if self._state in ("done", "finished", "ready"):
            return self._style("SUCCESS_COLOR")
        return self._style("COLLAPSIBLE_TITLE")

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def _apply_width(self) -> None:
        width = MIN_WIDTH
        if self._anchor.isValid() and self._anchor.width() > 0:
            width = int(self._anchor.width() * 0.8)
            width = max(MIN_WIDTH, min(MAX_WIDTH, width))
        screen = (QGuiApplication.screenAt(self._anchor.center())
                  or QGuiApplication.primaryScreen())
        if screen is not None:
            width = min(width, max(240, screen.availableGeometry().width() - 16))
        self.setFixedWidth(width)

    def _place_above(self) -> None:
        """Centers the card over its task, above it when there is room."""
        if not self._anchor.isValid():
            return
        screen = (QGuiApplication.screenAt(self._anchor.center())
                  or QGuiApplication.primaryScreen())
        if screen is None:
            return
        available = screen.availableGeometry()
        size = self.size()
        x = self._anchor.center().x() - size.width() // 2
        x = max(available.left() + 8,
                min(x, available.right() - size.width() - 8))
        y = self._anchor.top() - size.height() - 8
        if y < available.top() + 8:
            y = self._anchor.bottom() + 8
        y = max(available.top() + 8,
                min(y, available.bottom() - size.height() - 8))
        self.move(QPoint(x, y))

    def _apply_theme(self) -> None:
        meta = self._style("COLLAPSIBLE_META")
        background = self._style("CODE_BG")
        border = self._style("CODE_BORDER")
        view_bg = self._style("TERMINAL_PRE_BG") or background
        view_fg = (self._style("TERMINAL_PRE_COLOR")
                   or self._style("COLLAPSIBLE_TITLE"))
        self._card.setStyleSheet(
            f"QFrame#OutputPreviewCard {{ background-color: {background}; "
            f"border: 1px solid {border}; border-radius: 10px; }}"
            " QLabel { background: transparent; border: none; }"
            f" QPlainTextEdit#OutputPreviewView {{ background-color: {view_bg}; "
            f"color: {view_fg}; border: none; "
            f"font-family: {_MONO_FAMILIES}; }}")
        self._title.setStyleSheet(
            f"color: {self._style('COLLAPSIBLE_TITLE')}; font-size: 12px;")
        self._elapsed.setStyleSheet(
            f"color: {meta}; font-size: 11px; font-family: {_MONO_FAMILIES};")
        self._actions.setStyleSheet(f"font-size: 11px; color: {meta};")
        self._hint.setStyleSheet(
            f"color: {meta}; font-size: 11px; font-family: {_MONO_FAMILIES};")
        self._dot.setStyleSheet(
            f"color: {self._state_color()}; font-size: 10px;")

    # -- window lifecycle --------------------------------------------------

    def enterEvent(self, event):
        # The reader crossed from the task into the card: cancel the pending
        # hide so the output can be scrolled.
        self._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.schedule_hide()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._hide_timer.stop()
        super().hideEvent(event)
        self.closed.emit()
