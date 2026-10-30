"""Access-mode popup for the Terminal composer chip.

Shows the three permission modes with a one-line explanation each, marks the
active one with a check and paints Full access in the warning color. The
composer keeps the setting in sync with Settings via ``terminal.access_mode``.
"""
from PySide6.QtCore import Qt, QPoint, QSize, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

import qtawesome as qta

OPTIONS = (
    ("ask", "fa5s.shield-alt", "Ask every time",
     "Always ask before commands, external edits, and internet use"),
    ("auto", "fa5s.play-circle", "Auto approve",
     "Only asks when a potential risk is detected."),
    ("full", "fa5s.exclamation-triangle", "Full access",
     "Never ask, with full access to your files, terminal, and the internet"),
)


class _AccessRow(QFrame):
    """Clickable two-line row; QFrame so the stylesheet paints hover."""

    clicked = Signal(str)

    def __init__(self, mode: str, parent=None):
        super().__init__(parent)
        self.setObjectName("AccessModeRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode = mode

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit(self._mode)
        super().mouseReleaseEvent(event)


class TerminalAccessPopup(QFrame):
    """Popup menu above the composer chip; emits the chosen mode."""

    mode_selected = Signal(str)

    WIDTH = 440

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(self.WIDTH)
        self._current = "ask"
        self._text = "#e6edf3"
        self._muted = "#8b949e"
        self._error = "#f85149"
        self._rows = {}
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.body = QFrame(self)
        self.body.setObjectName("AccessPopupBody")
        self.body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self.body)
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(2)

        for mode, icon_name, title, subtitle in OPTIONS:
            button = _AccessRow(mode, self.body)
            button.clicked.connect(self._choose)
            row = QHBoxLayout(button)
            row.setContentsMargins(10, 8, 10, 8)
            row.setSpacing(10)
            icon = QLabel(button)
            icon.setFixedSize(18, 18)
            icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
            text_box = QVBoxLayout()
            text_box.setSpacing(1)
            title_label = QLabel(title, button)
            title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            meta_label = QLabel(subtitle, button)
            meta_label.setWordWrap(True)
            meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            text_box.addWidget(title_label)
            text_box.addWidget(meta_label)
            row.addLayout(text_box, 1)
            check = QLabel(button)
            check.setFixedSize(16, 16)
            check.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            row.addWidget(check, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(button)
            self._rows[mode] = {
                "widget": button,
                "icon": icon,
                "icon_name": icon_name,
                "title": title_label,
                "meta": meta_label,
                "meta_text": subtitle,
                "check": check,
            }

    # -- state ----------------------------------------------------------

    def set_current(self, mode: str):
        self._current = mode if mode in self._rows else "ask"
        self._update_checks()

    def set_theme(self, colors: dict, dark: bool, error: str):
        text = colors.get("text") or "#e6edf3"
        muted = colors.get("muted") or "#8b949e"
        border = colors.get("border") or "#30363d"
        panel = colors.get("panel") or "#161b22"
        hover = colors.get("hover") or "rgba(255, 255, 255, 0.07)"
        self._text = text
        self._muted = muted
        self._error = error or "#f85149"
        self.setStyleSheet(f"""
            QFrame#AccessPopupBody {{
                background: {panel};
                border: 1px solid {border};
                border-radius: 10px;
            }}
            QFrame#AccessModeRow {{
                background: transparent;
                border: none;
                border-radius: 7px;
            }}
            QFrame#AccessModeRow:hover {{ background: {hover}; }}
        """)
        for mode, row in self._rows.items():
            color = self._error if mode == "full" else text
            meta_color = self._error if mode == "full" else muted
            row["title"].setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: 600;"
                " background: transparent;")
            row["meta"].setStyleSheet(
                f"color: {meta_color}; font-size: 11px; background: transparent;")
            row["icon"].setPixmap(
                qta.icon(row["icon_name"], color=color).pixmap(QSize(18, 18)))
        self._fit_rows()
        self._update_checks()
        self.adjustSize()

    def _fit_rows(self):
        """Wrapped labels get explicit heights; layouts underestimate them."""
        available = max(1, self.WIDTH - 96)
        for row in self._rows.values():
            meta = row["meta"]
            needed = meta.heightForWidth(available)
            if needed > 0:
                meta.setFixedHeight(needed)
                row["widget"].setMinimumHeight(15 + 1 + needed + 16)

    def _update_checks(self):
        for mode, row in self._rows.items():
            if mode != self._current:
                row["check"].clear()
                continue
            color = self._error if mode == "full" else self._text
            row["check"].setPixmap(
                qta.icon("fa5s.check", color=color).pixmap(QSize(16, 16)))

    def open_near(self, widget):
        """Shows the popup above the anchor widget (falls back below)."""
        self.layout().activate()
        self.adjustSize()
        anchor = widget.mapToGlobal(QPoint(0, 0))
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        x = anchor.x()
        y = anchor.y() - self.height() - 8
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 6,
                    min(x, area.right() - self.width() - 6))
            if y < area.top() + 6:
                y = anchor.y() + widget.height() + 8
        self.move(x, y)
        self.show()
        self.raise_()

    def _choose(self, mode: str):
        self.hide()
        if mode != self._current:
            self.mode_selected.emit(mode)
