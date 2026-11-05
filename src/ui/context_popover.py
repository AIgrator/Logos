"""Context-window widgets: the click-through popover and compact gauges.

The popover explains what the reported context fill is made of — system
prompt, tool schema, skills, messages and the unaccounted remainder — and
offers compaction. Shares are estimates derived from the next request;
the total stays provider-reported when the provider reports usage.
"""
from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from context import context_stats

_SEGMENT_COLORS = {
    "dark": {
        "system": "#3fb950",
        "tools": "#58a6ff",
        "skills": "#bc8cff",
        "messages": "#39c5cf",
        "other": "#8b949e",
    },
    "light": {
        "system": "#1a7f37",
        "tools": "#0969da",
        "skills": "#8250df",
        "messages": "#1b7c83",
        "other": "#6e7781",
    },
}

_ROW_KEYS = ("system", "tools", "skills", "messages", "other")

_SOURCE_LABELS = {
    "actual": "provider-reported",
    "payload": "estimated from the request",
    "estimated": "estimated from the journal",
}


def _share_text(share: float) -> str:
    if share <= 0:
        return "0%"
    if share < 0.01:
        return "<1%"
    return f"{share * 100:.0f}%"


class ContextGauge(QWidget):
    """Small ring showing context fill; the caller adds the numeric label."""

    clicked = Signal()

    def __init__(self, size: int = 24, parent=None):
        super().__init__(parent)
        self._size = size
        self._percent = 0.0
        self._status = ""
        self._track = QColor("#30363d")
        self._accent = QColor("#3fb950")
        self._warning = QColor("#d29922")
        self._error = QColor("#f85149")
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.hide()

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def set_state(self, percent, status: str = ""):
        if percent is None:
            self.hide()
            return
        self._percent = max(0.0, min(100.0, float(percent or 0.0)))
        self._status = str(status or "")
        self.show()
        self.update()

    def set_theme(self, colors: dict, accent: str, warning: str, error: str):
        self._track = QColor(colors.get("border") or "#30363d")
        self._accent = QColor(accent)
        self._warning = QColor(warning)
        self._error = QColor(error)
        self.update()

    def _fill(self) -> QColor:
        if self._status == "over" or self._percent >= 95:
            return self._error
        if self._status == "near" or self._percent >= 80:
            return self._warning
        return self._accent

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen_width = 3.0
        inset = pen_width / 2 + 1
        rect = QRectF(self.width() - 2 * inset, self.height() - 2 * inset,
                      self.width() - 2 * inset, self.height() - 2 * inset)
        rect.moveCenter(QRectF(self.rect()).center())
        track = QColor(self._track)
        track.setAlpha(170)
        pen = QPen(track, pen_width)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, 0, 360 * 16)
        if self._percent > 0:
            pen.setColor(self._fill())
            painter.setPen(pen)
            span = -int(round(self._percent / 100.0 * 360 * 16))
            painter.drawArc(rect, 90 * 16, span)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class SegmentedBar(QWidget):
    """One-bar composition of the context window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self._segments = []
        self._track = QColor("#30363d")

    def set_segments(self, segments: list, track: QColor):
        self._segments = [
            (max(0.0, min(1.0, float(share or 0.0))), QColor(color))
            for share, color in segments
        ]
        self._track = QColor(track)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, 4, 4)
        painter.fillPath(path, self._track)
        painter.setClipPath(path)
        width = float(self.width())
        x = 0.0
        gap = 2.0
        for share, color in self._segments:
            segment = width * share
            if segment <= 0:
                continue
            if x >= width:
                break
            painter.fillRect(
                QRectF(x, 0, max(0.0, min(segment - gap, width - x)), self.height()),
                color)
            x += segment
        painter.end()


class ContextPopover(QFrame):
    """Click-through detail card for the context fill indicator."""

    compress_requested = Signal()
    details_requested = Signal()

    WIDTH = 332

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("ContextPopover")
        self.setFixedWidth(self.WIDTH)
        self._dark = True
        self._rows = {}
        self._row_shares = {}
        self._simple_share = 0.0
        self._percent = 0.0
        self._status = ""
        self._build_ui()

    # -- construction ---------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.body = QFrame(self)
        self.body.setObjectName("ContextPopoverBody")
        self.body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self.body)
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("Context window")
        title.setObjectName("ContextPopoverTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.percent_label = QLabel("—")
        self.percent_label.setObjectName("ContextPopoverPercent")
        header.addWidget(self.percent_label)
        layout.addLayout(header)

        self.hint_label = QLabel(
            "How much of this model's context window the next request uses. "
            "Compaction replaces older messages with the session summary.")
        self.hint_label.setObjectName("ContextPopoverHint")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.tokens_label = QLabel("")
        self.tokens_label.setObjectName("ContextPopoverTokens")
        self.tokens_label.setWordWrap(True)
        layout.addWidget(self.tokens_label)

        self.bar = SegmentedBar(self.body)
        layout.addWidget(self.bar)

        self.rows_box = QVBoxLayout()
        self.rows_box.setContentsMargins(0, 2, 0, 0)
        self.rows_box.setSpacing(6)
        for key in _ROW_KEYS:
            row = QWidget(self.body)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            dot = QLabel(row)
            dot.setFixedSize(8, 8)
            row_layout.addWidget(dot)
            name = QLabel("")
            name.setObjectName("ContextPopoverRow")
            row_layout.addWidget(name)
            row_layout.addStretch(1)
            value = QLabel("")
            value.setObjectName("ContextPopoverValue")
            row_layout.addWidget(value)
            row.hide()
            self.rows_box.addWidget(row)
            self._rows[key] = {"row": row, "dot": dot, "name": name, "value": value}
        layout.addLayout(self.rows_box)

        self.empty_label = QLabel(
            "Composition is not available for this panel; the total is "
            "reported by the provider.")
        self.empty_label.setObjectName("ContextPopoverHint")
        self.empty_label.setWordWrap(True)
        self.empty_label.hide()
        layout.addWidget(self.empty_label)

        self.compress_button = QPushButton("Compress context")
        self.compress_button.setObjectName("ContextPopoverAction")
        self.compress_button.setCursor(Qt.PointingHandCursor)
        self.compress_button.clicked.connect(self._on_compress)
        layout.addWidget(self.compress_button)

        self.details_button = QPushButton("Session context…")
        self.details_button.setObjectName("ContextPopoverLink")
        self.details_button.setCursor(Qt.PointingHandCursor)
        self.details_button.clicked.connect(self._on_details)
        layout.addWidget(self.details_button, 0, Qt.AlignRight)

    # -- data -----------------------------------------------------------

    def set_stats(self, stats: dict):
        stats = stats or {}
        window = int(stats.get("window") or 0)
        percent = float(stats.get("percent") or 0.0)
        used = int(stats.get("context_tokens") or 0)
        source = str(stats.get("context_source") or "estimated")
        self.percent_label.setText(f"{percent:.0f}%" if window else "—")
        tokens_text = f"{used:,} / {window:,} tokens" if window else "Window unknown"
        label = _SOURCE_LABELS.get(source, source)
        self.tokens_label.setText(f"{tokens_text} · {label}")

        breakdown = context_stats.payload_breakdown(stats)
        self.empty_label.setVisible(not breakdown)
        self._row_shares = {}
        self._simple_share = 0.0
        self._percent = percent
        self._status = str(stats.get("status") or "")
        for key in _ROW_KEYS:
            row = self._rows[key]
            item = next((entry for entry in breakdown if entry["key"] == key), None)
            if item is None:
                row["row"].hide()
                continue
            share = (item["tokens"] / window) if window else 0.0
            row["name"].setText(item["label"])
            row["value"].setText(_share_text(share) if window else "—")
            row["row"].show()
            self._row_shares[key] = share
        if not breakdown:
            self._simple_share = max(0.0, min(1.0, percent / 100.0))
        self._render_bar()
        self.adjustSize()

    def _fill_color(self) -> str:
        if self._status == "over" or self._percent >= 95:
            return self._error
        if self._status == "near" or self._percent >= 80:
            return self._warning
        return self._accent

    def _render_bar(self):
        palette = _SEGMENT_COLORS["dark" if self._dark else "light"]
        segments = [
            (self._row_shares[key], palette[key])
            for key in _ROW_KEYS if key in self._row_shares
        ]
        if not segments and self._simple_share:
            segments = [(self._simple_share, self._fill_color())]
        self.bar.set_segments(segments, QColor(self._track_color()))

    def set_theme(self, colors: dict, dark: bool, accent: str,
                  warning: str, error: str):
        self._dark = bool(dark)
        self._accent = accent
        self._warning = warning
        self._error = error
        text = colors.get("text") or "#e6edf3"
        muted = colors.get("muted") or "#8b949e"
        border = colors.get("border") or "#30363d"
        panel = colors.get("panel") or "#161b22"
        hover = colors.get("hover") or "rgba(255, 255, 255, 0.07)"
        self.setStyleSheet(f"""
            QFrame#ContextPopoverBody {{
                background: {panel};
                border: 1px solid {border};
                border-radius: 10px;
            }}
            QLabel#ContextPopoverTitle {{
                color: {text}; font-size: 13px; font-weight: 600;
            }}
            QLabel#ContextPopoverPercent {{
                color: {text}; font-size: 13px; font-weight: 600;
            }}
            QLabel#ContextPopoverHint, QLabel#ContextPopoverTokens {{
                color: {muted}; font-size: 11px;
            }}
            QLabel#ContextPopoverRow {{ color: {text}; font-size: 12px; }}
            QLabel#ContextPopoverValue {{
                color: {muted}; font-size: 12px; font-weight: 600;
            }}
            QPushButton#ContextPopoverAction {{
                background: {hover};
                border: 1px solid {border};
                border-radius: 7px;
                color: {text};
                font-size: 12px;
                padding: 6px 10px;
            }}
            QPushButton#ContextPopoverAction:hover {{ border-color: {muted}; }}
            QPushButton#ContextPopoverLink {{
                border: none; background: transparent;
                color: {muted}; font-size: 11px; padding: 2px;
            }}
            QPushButton#ContextPopoverLink:hover {{ color: {text}; }}
        """)
        for key, palette in _SEGMENT_COLORS["dark" if dark else "light"].items():
            dot = self._rows[key]["dot"]
            dot.setStyleSheet(
                f"background: {palette}; border-radius: 4px;")
        self._render_bar()
        self.adjustSize()

    def _track_color(self) -> str:
        return "#30363d" if self._dark else "#d0d7de"

    # -- actions --------------------------------------------------------

    def _on_compress(self):
        self.hide()
        self.compress_requested.emit()

    def _on_details(self):
        self.hide()
        self.details_requested.emit()
