"""Live plan pill above the composer.

A plan is progress, not history: a compact "Step 2 / 5" pill is shown while
the turn runs, hovering it reveals the checklist, and it disappears when the
turn ends, so finished turns never leave a stale plan in the transcript.
"""
from __future__ import annotations

import html

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_PLAN_STATUS = {
    "done": ("\u2713", "SUCCESS_COLOR"),
    "in_progress": ("\u25cf", "COLLAPSIBLE_TITLE"),
    "pending": ("\u25cb", "COLLAPSIBLE_META"),
}


def normalize_plan_steps(steps) -> list[dict]:
    """Validated plan rows ready for display."""
    normalized = []
    for step in steps or []:
        if isinstance(step, str):
            text, status = step.strip(), "pending"
        elif isinstance(step, dict):
            text = str(step.get("text") or "").strip()
            status = str(step.get("status") or "pending").strip().lower()
        else:
            continue
        if status not in _PLAN_STATUS:
            status = "pending"
        if text:
            normalized.append({"text": text, "status": status})
    return normalized


def current_step_index(steps) -> int:
    """1-based number the pill shows: the step being worked on."""
    for index, step in enumerate(steps):
        if step.get("status") == "in_progress":
            return index + 1
    for index, step in enumerate(steps):
        if step.get("status") == "pending":
            return index + 1
    return len(steps)


class _PlanPopover(QFrame):
    """Hover card with the full checklist; never takes focus or clicks."""

    def __init__(self, theme_manager):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._theme = theme_manager
        self._steps: list[dict] = []
        # Without a floor the word-wrapped rows shrink-wrap into a narrow,
        # hard-to-read column.
        self.setMinimumWidth(300)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(3)
        self._apply_theme()

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def _step_html(self, step: dict) -> str:
        status = str(step.get("status") or "pending")
        icon, color_key = _PLAN_STATUS.get(status, _PLAN_STATUS["pending"])
        color = self._style(color_key)
        strike = "text-decoration: line-through;" if status == "done" else ""
        text = html.escape(str(step.get("text") or ""))
        return (f'<span style="color: {color}; {strike}">'
                f"{icon}&nbsp; {text}</span>")

    def show_steps(self, steps: list[dict], anchor: QWidget) -> None:
        self._steps = steps
        self._render()
        self.adjustSize()

        origin = anchor.mapToGlobal(QPoint(0, 0))
        size = self.size()
        screen = (QGuiApplication.screenAt(origin)
                  or QGuiApplication.primaryScreen())
        available = screen.availableGeometry()
        x = origin.x() + (anchor.width() - size.width()) // 2
        x = max(available.left() + 8,
                min(x, available.right() - size.width() - 8))
        y = origin.y() - size.height() - 8
        if y < available.top() + 8:
            y = origin.y() + anchor.height() + 8
        self.move(QPoint(x, y))
        self.show()

    def refresh_theme(self) -> None:
        self._apply_theme()
        if self.isVisible():
            self._render()
            self.adjustSize()

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"QFrame {{ background-color: {self._style('CODE_BG')}; "
            f"border: 1px solid {self._style('CODE_BORDER')}; "
            "border-radius: 8px; }"
            " QLabel { background: transparent; border: none; }")

    def _render(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for step in self._steps:
            label = QLabel(self)
            label.setTextFormat(Qt.RichText)
            label.setWordWrap(True)
            label.setMaximumWidth(420)
            label.setText(self._step_html(step))
            self._layout.addWidget(label)


class _PlanPill(QFrame):
    """The compact pill; hovering it opens the checklist popover."""

    def __init__(self, on_enter, on_leave, parent=None):
        super().__init__(parent)
        self._on_enter = on_enter
        self._on_leave = on_leave

    def enterEvent(self, event):
        self._on_enter()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._on_leave()
        super().leaveEvent(event)


class PlanPanel(QWidget):
    """Compact "Step N / M" pill above the input for the duration of a turn."""

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self._theme = theme_manager
        self._steps: list[dict] = []
        self._suppressed = False
        self._popover: _PlanPopover | None = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.setVisible(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 2)
        row.setSpacing(0)
        row.addStretch(1)
        self._pill = _PlanPill(self._show_popover, self._hide_popover, self)
        pill_layout = QHBoxLayout(self._pill)
        pill_layout.setContentsMargins(10, 2, 10, 2)
        pill_layout.setSpacing(6)
        self._icon = QLabel("\u25d4", self._pill)
        self._label = QLabel("", self._pill)
        pill_layout.addWidget(self._icon)
        pill_layout.addWidget(self._label)
        row.addWidget(self._pill)
        row.addStretch(1)

        try:
            self._theme.theme_changed.connect(self._on_theme_changed)
        except Exception:
            pass
        self._apply_theme()

    # -- public API ---------------------------------------------------------

    def set_plan(self, steps) -> None:
        normalized = normalize_plan_steps(steps)
        if not normalized:
            self.clear_plan()
            return
        self._steps = normalized
        self._update_label()
        if self._popover is not None and self._popover.isVisible():
            self._popover.show_steps(self._steps, self._pill)
        if not self._suppressed:
            self.setVisible(True)

    def clear_plan(self) -> None:
        self._steps = []
        self._hide_popover()
        self._update_label()
        self.setVisible(False)

    def has_plan(self) -> bool:
        return bool(self._steps)

    def set_suppressed(self, suppressed: bool) -> None:
        """Hides the pill without forgetting the plan (compact mode)."""
        self._suppressed = bool(suppressed)
        if self._suppressed:
            self._hide_popover()
            self.setVisible(False)
        elif self._steps:
            self.setVisible(True)

    # -- rendering ----------------------------------------------------------

    def _style(self, key: str) -> str:
        try:
            return self._theme.get_style(key) or ""
        except Exception:
            return ""

    def _apply_theme(self) -> None:
        meta = self._style("COLLAPSIBLE_META")
        border = self._style("CODE_BORDER")
        background = self._style("CODE_BG")
        self._pill.setStyleSheet(
            f"QFrame {{ background-color: {background}; "
            f"border: 1px solid {border}; border-radius: 10px; }}"
            " QLabel { background: transparent; border: none; }")
        self._icon.setStyleSheet(f"color: {meta}; font-size: 12px;")
        self._label.setStyleSheet(f"color: {meta}; font-size: 12px;")

    def _on_theme_changed(self) -> None:
        self._apply_theme()
        if self._popover is not None:
            self._popover.refresh_theme()

    def _update_label(self) -> None:
        if not self._steps:
            self._label.setText("")
            return
        self._label.setText(
            f"Step {current_step_index(self._steps)} / {len(self._steps)}")

    def _show_popover(self) -> None:
        if not self._steps or self.isHidden():
            return
        if self._popover is None:
            self._popover = _PlanPopover(self._theme)
        self._popover.show_steps(self._steps, self._pill)

    def _hide_popover(self) -> None:
        if self._popover is not None:
            self._popover.hide()

    def hideEvent(self, event):
        self._hide_popover()
        super().hideEvent(event)
