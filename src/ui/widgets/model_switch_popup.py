"""OpenCode-style popup for in-session LLM model switching.

Shows provider groups (only providers with an API key configured), a
search field, and the current model marked with a check. Emits
model_selected(provider_id, model_id) with the stored model id
(e.g. "gemini-3.8-flash" or "anthropic/claude-sonnet-5.5" for OpenRouter).

Hovering a model shows a floating info panel (provider / input / reasoning /
context) to the right of the list, mirroring opencode's model picker.

The footer carries the per-model reasoning effort control. It follows the
highlighted model and emits reasoning_effort_changed for that exact model.
"""
import logging

from PySide6.QtCore import Qt, Signal, QPoint, QPointF, QEvent
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from context.tokens import ModelMetadata
from llm.reasoning_effort import label as effort_label


class _EffortCombo(QComboBox):
    """Effort dropdown with a painted chevron.

    A styled QComboBox loses the native arrow, which made the footer row
    read as a static label instead of a control.
    """

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#b8b8b8") if self.isEnabled() else QColor("#666666"))
        cx = float(self.width() - 12)
        cy = self.height() / 2.0
        painter.drawPolygon(QPolygonF([
            QPointF(cx - 4.0, cy - 2.0),
            QPointF(cx + 4.0, cy - 2.0),
            QPointF(cx, cy + 3.0),
        ]))


class ModelSwitchPopup(QDialog):
    model_selected = Signal(str, str)  # provider_id, stored model id
    reasoning_effort_changed = Signal(str, str, str)  # provider_id, model id, level

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(300)
        self.setMinimumHeight(120)
        self.setMaximumHeight(380)

        self.setStyleSheet(
            "QDialog { background-color: #1e1e1e; border: 1px solid #3a3a3a; }"
            "QLineEdit { background-color: #252526; color: #d4d4d4; border: 1px solid #3a3a3a;"
            " padding: 6px; font-size: 13px; }"
            "QListWidget { background-color: #1e1e1e; color: #d4d4d4; border: none;"
            " font-size: 13px; outline: none; }"
            "QListWidget::item { padding: 5px 8px; }"
            "QListWidget::item:selected { background-color: #094771; color: #ffffff; }"
            "QListWidget::item:hover:!selected { background-color: #2a2d2e; }"
            "QComboBox { background-color: #252526; color: #d4d4d4; border: 1px solid #3a3a3a;"
            " padding: 3px 22px 3px 6px; font-size: 12px; }"
            "QComboBox:hover { border-color: #5c5c5c; background-color: #2a2a2b; }"
            "QComboBox:disabled { color: #777777; }"
            "QComboBox QAbstractItemView { background-color: #252526; color: #d4d4d4;"
            " selection-background-color: #094771; }"
            "QComboBox::drop-down { border: none; width: 18px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search models")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.model_list = QListWidget(self)
        self.model_list.setMouseTracking(True)
        self.model_list.itemClicked.connect(self._on_item_clicked)
        self.model_list.itemEntered.connect(self._update_details_from_item)
        self.model_list.currentItemChanged.connect(
            lambda cur, _prev: self._update_details_from_item(cur))
        self.model_list.installEventFilter(self)
        layout.addWidget(self.model_list, 1)

        # Footer: reasoning effort of the highlighted model. The row stays
        # visible for models without effort control (disabled, explained)
        # so the dialog height never jumps while hovering.
        self._effort_data = None
        self._active_model_data = None
        self.effort_separator = QFrame(self)
        self.effort_separator.setFixedHeight(1)
        self.effort_separator.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.effort_separator.setStyleSheet("background-color: #333333;")
        layout.addWidget(self.effort_separator)
        self.effort_row = QWidget(self)
        effort_layout = QHBoxLayout(self.effort_row)
        effort_layout.setContentsMargins(4, 2, 4, 3)
        effort_layout.setSpacing(6)
        self.effort_label = QLabel("Effort", self.effort_row)
        self.effort_label.setStyleSheet("QLabel { color: #8a8a8a; font-size: 12px; }")
        self.effort_combo = _EffortCombo(self.effort_row)
        self.effort_combo.setToolTip(
            "Reasoning effort for the highlighted model. "
            "Saved separately for each model.")
        self.effort_combo.currentIndexChanged.connect(self._on_effort_changed)
        effort_layout.addWidget(self.effort_label)
        effort_layout.addWidget(self.effort_combo, 1)
        layout.addWidget(self.effort_row)
        self._show_effort_for(None)

        # Floating info panel (not a permanent widget), shown to the right of
        # the highlighted model and hidden when the pointer leaves the list.
        self._details = QLabel(None, Qt.WindowType.ToolTip)
        self._details.setTextFormat(Qt.TextFormat.RichText)
        self._details.setStyleSheet(
            "QLabel { background-color: #252526; color: #d4d4d4;"
            " border: 1px solid #3a3a3a; padding: 6px; font-size: 11px; }"
        )
        self._details.hide()

    def eventFilter(self, obj, event):
        if obj is self.model_list and event.type() == QEvent.Type.Leave:
            self._details.hide()
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        self._details.hide()
        super().hideEvent(event)

    def set_sections(self, sections: list):
        """Populates the list.

        sections: list of dicts with kind 'header'/'subheader' (label) or
        'model' (label, provider_id, model_id, provider_display, is_current,
        effort_levels, effort_current).
        """
        self._details.hide()
        self._active_model_data = None
        self.model_list.clear()
        self.search.clear()
        for entry in sections:
            kind = entry.get("kind")
            if kind in ("header", "subheader"):
                item = QListWidgetItem(entry.get("label", ""))
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                if kind == "header":
                    item.setForeground(Qt.GlobalColor.darkGray)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                else:
                    item.setForeground(Qt.GlobalColor.gray)
                item.setData(Qt.ItemDataRole.UserRole, {"kind": kind, "label": entry.get("label", "")})
                self.model_list.addItem(item)
            else:
                text = entry.get("label", "")
                if entry.get("is_current"):
                    text = f"\u2713 {text}"
                item = QListWidgetItem(text)
                data = {
                    "kind": "model",
                    "label": entry.get("label", ""),
                    "provider_id": entry.get("provider_id", ""),
                    "model_id": entry.get("model_id", ""),
                    "provider_display": entry.get("provider_display", ""),
                    "effort_levels": list(entry.get("effort_levels") or []),
                    "effort_current": entry.get("effort_current") or "",
                }
                item.setData(Qt.ItemDataRole.UserRole, data)
                if entry.get("is_current"):
                    self._active_model_data = data
                self.model_list.addItem(item)
        self._apply_filter("")

    def open_near(self, widget):
        """Shows the popup above the anchor widget (falls back below)."""
        self.show()
        anchor = widget.mapToGlobal(QPoint(0, 0))
        screen = QApplication.primaryScreen().availableGeometry()
        x = max(screen.left(), min(anchor.x(), screen.right() - self.width()))
        y = anchor.y() - self.height()
        if y < screen.top():
            y = anchor.y() + widget.height()
        self.move(x, y)
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def _details_html(self, data: dict) -> str:
        """Two-column label/value table, compact like opencode's panel."""
        pid = data.get("provider_id", "")
        mid = data.get("model_id", "")
        meta = ModelMetadata.get(pid, mid)
        window = meta.get("context_window") or 0
        context = f"{int(window):,}".replace(",", " ") if window else "unknown"
        if not meta:
            reasoning = "—"
        else:
            reasoning = "Supported" if meta.get("reasoning") else "No"
        modalities = meta.get("input_modalities") or []
        inputs = ", ".join(modalities) if modalities else "—"
        provider = data.get("provider_display") or pid
        rows = [
            ("Model", data.get("label", "")),
            ("Provider", provider),
            ("Input", inputs),
            ("Reasoning", reasoning),
        ]
        if data.get("effort_levels"):
            rows.append(("Effort", effort_label(data.get("effort_current", ""))))
        rows.append(("Context", context))
        label_style = "color:#8a8a8a; padding:0 12px 2px 0;"
        value_style = "color:#e6e6e6; padding:0 0 2px 0;"
        body = "".join(
            f'<tr><td style="{label_style}">{k}</td><td style="{value_style}">{v}</td></tr>'
            for k, v in rows
        )
        return f'<table cellspacing="0" cellpadding="0">{body}</table>'

    def _show_effort_for(self, data):
        """Points the footer control at a model entry (None = no model)."""
        levels = list((data or {}).get("effort_levels") or [])
        self.effort_combo.blockSignals(True)
        try:
            self.effort_combo.clear()
            if levels:
                self._effort_data = data
                for level in levels:
                    self.effort_combo.addItem(effort_label(level), level)
                index = self.effort_combo.findData(data.get("effort_current", ""))
                self.effort_combo.setCurrentIndex(index if index >= 0 else 0)
                self.effort_combo.setEnabled(True)
                self.effort_label.setEnabled(True)
            else:
                self._effort_data = None
                self.effort_combo.addItem("Not supported", "")
                self.effort_combo.setCurrentIndex(0)
                self.effort_combo.setEnabled(False)
                self.effort_label.setEnabled(False)
        finally:
            self.effort_combo.blockSignals(False)

    def _on_effort_changed(self, index: int):
        data = self._effort_data
        if not data or index < 0:
            return
        level = self.effort_combo.itemData(index) or ""
        if not level:
            return
        pid = data.get("provider_id", "")
        mid = data.get("model_id", "")
        data["effort_current"] = level
        active = self._active_model_data
        if (active and active.get("provider_id") == pid
                and active.get("model_id") == mid):
            active["effort_current"] = level
        # Keep every list copy of this model in sync (and the open panel).
        for i in range(self.model_list.count()):
            other = self.model_list.item(i)
            odata = other.data(Qt.ItemDataRole.UserRole) or {}
            if (odata.get("kind") == "model" and odata.get("provider_id") == pid
                    and odata.get("model_id") == mid):
                odata["effort_current"] = level
                other.setData(Qt.ItemDataRole.UserRole, odata)
                if self._details.isVisible():
                    self._details.setText(self._details_html(odata))
                    self._details.adjustSize()
        logging.info(f"Reasoning effort changed: {pid} / {mid} -> {level}")
        self.reasoning_effort_changed.emit(pid, mid, level)

    def _update_details_from_item(self, item):
        """Shows the floating info panel next to the highlighted model."""
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") != "model":
            return
        # Footer follows the hovered model and keeps pointing at it when the
        # pointer moves down to the combo (no revert on list leave).
        self._show_effort_for(data)
        self._details.setText(self._details_html(data))
        self._details.adjustSize()
        rect = self.model_list.visualItemRect(item)
        viewport = self.model_list.viewport()
        screen = QApplication.primaryScreen().availableGeometry()
        x = self.mapToGlobal(QPoint(0, 0)).x() - self._details.width() - 2
        x = max(screen.left(), x)
        y = viewport.mapToGlobal(QPoint(0, rect.top())).y()
        if y + self._details.height() > screen.bottom():
            y = screen.bottom() - self._details.height()
        self._details.move(x, max(screen.top(), y))
        self._details.show()
        self._details.raise_()

    def _apply_filter(self, query: str):
        self._details.hide()
        # Typing/clearing the search invalidates the hovered target: fall back
        # to the active model until the pointer highlights another one.
        self._show_effort_for(self._active_model_data)
        query = (query or "").strip().lower()
        # First pass: model visibility.
        for i in range(self.model_list.count()):
            item = self.model_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if data.get("kind") != "model":
                continue
            if not query:
                item.setHidden(False)
                continue
            haystack = f"{data.get('label', '')} {data.get('provider_id', '')}".lower()
            item.setHidden(query not in haystack)
        # Second pass: headers visible only if they own a visible model.
        # A top header owns everything down to the next top header;
        # a subheader owns models down to the next header of any kind.
        for i in range(self.model_list.count()):
            item = self.model_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            kind = data.get("kind")
            if kind not in ("header", "subheader"):
                continue
            stop_kinds = ("header",) if kind == "header" else ("header", "subheader")
            visible_child = False
            for j in range(i + 1, self.model_list.count()):
                nxt = self.model_list.item(j)
                ndata = nxt.data(Qt.ItemDataRole.UserRole) or {}
                if ndata.get("kind") in stop_kinds:
                    break
                if not nxt.isHidden():
                    visible_child = True
                    break
            item.setHidden(query != "" and not visible_child)

    def _on_item_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") != "model":
            return
        logging.info(
            f"ModelSwitchPopup selected: {data.get('provider_id')} / {data.get('model_id')}"
        )
        self.model_selected.emit(data.get("provider_id", ""), data.get("model_id", ""))
        self.close()
