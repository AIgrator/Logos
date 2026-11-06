"""Live tail of the shell command the running step is producing.

A step row can show one grey progress line, because the transcript is a single
``QTextDocument`` and cannot hold a scroll area inside a card. That left the
user with "Open log" and Notepad to watch a long command actually print -
leaving the app to see what the agent is doing.

This strip sits above the composer (outside the transcript, so it owns a real
scroll area): eight monospace lines of the command's output, its clock, and the
same actions the step row has. It appears with the foreground command, keeps
the tail in view unless the reader scrolled back, and leaves when the turn
ends. It is chrome for the moment of watching, never history: the step row
keeps the recorded output.
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
)

# The strip is meant to be glanced at, not to replace the transcript: eight
# lines is enough for a ping's replies or a build's warnings.
LIVE_OUTPUT_LINES = 8
_MONO_FAMILIES = "Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', monospace"


def _one_line(text: str, limit: int = 72) -> str:
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


class LiveOutputPanel(QFrame):
    """Small scrollable viewer of one running shell command's output."""

    stop_requested = Signal()
    job_stop_requested = Signal(int)
    open_log_requested = Signal(str)

    def __init__(self, theme_manager, parent=None, lines: int = LIVE_OUTPUT_LINES):
        super().__init__(parent)
        self.setObjectName("LiveOutputPanel")
        self._theme = theme_manager
        self._line_count = max(3, int(lines))
        self._job_id = None
        self._log_path = ""
        self._state = "running"
        self._dismissed_job_id = None
        self._suppressed = False
        # False while the command is still the running step (Stop ends the
        # turn); True once it detached into a background job (Stop ends the
        # job, which is what the row above the composer would do as well).
        self._detached = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self._dot = QLabel("\u25cf", self)
        self._dot.setFixedWidth(10)
        self._title = QLabel("", self)
        self._elapsed = QLabel("", self)
        self._actions = QLabel("", self)
        self._actions.setTextFormat(Qt.RichText)
        self._actions.linkActivated.connect(self._on_link)
        header.addWidget(self._dot)
        header.addWidget(self._title, 1)
        header.addWidget(self._elapsed)
        header.addWidget(self._actions)
        outer.addLayout(header)

        self._view = QPlainTextEdit(self)
        self._view.setObjectName("LiveOutputView")
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
        # Exactly N readable lines: the strip must not eat the chat, and a
        # sliver of line N+1 above the scrollbar looks like a rendering bug.
        self._view.setFixedHeight(
            metrics.lineSpacing() * self._line_count + 2 * margin)
        outer.addWidget(self._view)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setVisible(False)
        self._apply_theme()

    # -- data in -----------------------------------------------------------

    @property
    def job_id(self):
        return self._job_id

    def text(self) -> str:
        return self._view.toPlainText()

    def is_suppressed(self) -> bool:
        return self._suppressed

    def start(self, job_id, command: str, log_path: str = "") -> bool:
        """Opens the strip for a job; False when this job was dismissed."""
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            return False
        if job_id == self._dismissed_job_id:
            return False
        if job_id != self._job_id:
            self._view.clear()
            self._job_id = job_id
            self._state = "running"
            self._detached = False
            self._elapsed.setText("")
        self._log_path = str(log_path or self._log_path or "")
        self._title.setText(
            f"#{job_id} \u00b7 {html.escape(_one_line(command or 'shell command'))}")
        self._sync_actions()
        if not self._suppressed:
            self.setVisible(True)
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
        """Freezes the strip on the last output instead of emptying it."""
        self.set_state(state)
        self._sync_actions()

    def set_detached(self, detached: bool = True) -> None:
        """The watched command became a background job of its own."""
        self._detached = bool(detached)
        self._sync_actions()

    def hide_output(self) -> None:
        self.setVisible(False)

    def clear(self) -> None:
        self._job_id = None
        self._log_path = ""
        self._dismissed_job_id = None
        self._state = "running"
        self._detached = False
        self._title.setText("")
        self._elapsed.setText("")
        self._view.clear()
        self.setVisible(False)

    def set_suppressed(self, suppressed: bool) -> None:
        """Compact mode hides the strip without forgetting what it shows."""
        self._suppressed = bool(suppressed)
        if self._suppressed:
            self.setVisible(False)

    # -- internals ---------------------------------------------------------

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
        elif href == "hide":
            self._dismissed_job_id = self._job_id
            self.hide_output()

    def _sync_actions(self) -> None:
        title = self._style("COLLAPSIBLE_TITLE")
        links = []
        if self._state == "running":
            links.append(f'<a href="stop" style="color: {title}; '
                         'text-decoration: none; font-weight: 600;">Stop</a>')
        if self._log_path:
            links.append(f'<a href="log" style="color: {title}; '
                         'text-decoration: none; font-weight: 600;">Open log</a>')
        links.append(f'<a href="hide" style="color: {title}; '
                     'text-decoration: none; font-weight: 600;">Hide</a>')
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

    def _apply_theme(self) -> None:
        meta = self._style("COLLAPSIBLE_META")
        background = self._style("CODE_BG")
        border = self._style("CODE_BORDER")
        view_bg = self._style("TERMINAL_PRE_BG") or background
        view_fg = self._style("TERMINAL_PRE_COLOR") or self._style("COLLAPSIBLE_TITLE")
        self.setStyleSheet(
            f"QFrame#LiveOutputPanel {{ background-color: {background}; "
            f"border: 1px solid {border}; border-radius: 8px; }}"
            " QLabel { background: transparent; border: none; }"
            f" QPlainTextEdit#LiveOutputView {{ background-color: {view_bg}; "
            f"color: {view_fg}; border: none; "
            f"font-family: {_MONO_FAMILIES}; }}")
        self._title.setStyleSheet(
            f"color: {self._style('COLLAPSIBLE_TITLE')}; font-size: 12px;")
        self._elapsed.setStyleSheet(
            f"color: {meta}; font-size: 11px; font-family: {_MONO_FAMILIES};")
        self._actions.setStyleSheet(f"font-size: 11px; color: {meta};")
        self._dot.setStyleSheet(
            f"color: {self._state_color()}; font-size: 10px;")
