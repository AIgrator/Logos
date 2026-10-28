"""OpenCode-style popup for in-session LLM model switching.

Shows provider groups (only providers with an API key configured), a
search field, and the current model marked with a check. Emits
model_selected(provider_id, model_id) with the stored model id
(e.g. "gemini-3.8-flash" or "anthropic/claude-sonnet-5.5" for OpenRouter).

Hovering a model shows a floating info panel (provider / input / reasoning /
context) to the right of the list, mirroring opencode's model picker.

Models that accept a reasoning effort show the current level as a chip on the
right of the row. Clicking the chip (or right-clicking the row) opens the level
menu for that exact model without switching to it, so the target is always the
row the user is pointing at.
"""
import logging

from PySide6.QtCore import Qt, Signal, QPoint, QEvent, QRect
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
)

from context.tokens import ModelMetadata
from llm.reasoning_effort import label as effort_label


def build_effort_menu(parent, levels: list, current: str, on_pick) -> QMenu:
    """Level menu shared by the picker rows and the composer effort badge."""
    menu = QMenu(parent)
    menu.setStyleSheet(
        "QMenu { background-color: #252526; color: #d4d4d4;"
        " border: 1px solid #3a3a3a; padding: 4px; }"
        "QMenu::item { padding: 4px 18px 4px 22px; }"
        "QMenu::item:selected { background-color: #094771; }"
    )
    for level in levels:
        action = menu.addAction(effort_label(level))
        action.setCheckable(True)
        action.setChecked(level == current)
        action.triggered.connect(lambda _checked=False, lvl=level: on_pick(lvl))
    return menu


class _ModelItemDelegate(QStyledItemDelegate):
    """Paints the model name on the left and its effort level as a chip."""

    _RIGHT_MARGIN = 8
    _CHIP_PAD_X = 7
    _CHIP_HEIGHT = 18
    _NAME_GAP = 10

    @classmethod
    def chip_rect(cls, rect: QRect, metrics, text: str) -> QRect:
        width = metrics.horizontalAdvance(text) + cls._CHIP_PAD_X * 2
        height = min(cls._CHIP_HEIGHT, max(12, rect.height() - 4))
        x = rect.right() - cls._RIGHT_MARGIN - width + 1
        y = rect.top() + (rect.height() - height) // 2
        return QRect(x, y, width, height)

    def paint(self, painter, option, index):
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        effort = ""
        if data.get("kind") == "model" and data.get("effort_levels"):
            effort = effort_label(data.get("effort_current", ""))
        if not effort:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        chip = self.chip_rect(opt.rect, opt.fontMetrics, effort)
        available = chip.left() - opt.rect.left() - self._NAME_GAP
        opt.text = opt.fontMetrics.elidedText(
            opt.text, Qt.TextElideMode.ElideRight, max(0, available))
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor("#d0d0d0" if selected else "#5f5f5f"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(chip.adjusted(0, 0, -1, -1), 3.0, 3.0)
        painter.setPen(QColor("#ffffff" if selected else "#c9c9c9"))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, effort)
        painter.restore()


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
        self.model_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.model_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.model_list.setItemDelegate(_ModelItemDelegate(self.model_list))
        self.model_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.model_list.customContextMenuRequested.connect(self._on_list_context_menu)
        self.model_list.itemClicked.connect(self._on_item_clicked)
        self.model_list.itemEntered.connect(self._update_details_from_item)
        self.model_list.currentItemChanged.connect(
            lambda cur, _prev: self._update_details_from_item(cur))
        self.model_list.installEventFilter(self)
        self.model_list.viewport().installEventFilter(self)
        layout.addWidget(self.model_list, 1)

        # Floating info panel (not a permanent widget), shown next to the
        # highlighted model and hidden when the pointer leaves the list.
        self._details_data = None
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
        elif obj is self.model_list.viewport():
            if event.type() == QEvent.Type.MouseMove:
                pos = event.position().toPoint()
                item = self.model_list.itemAt(pos)
                over_chip = (item is not None
                             and self._effort_chip_rect(item).adjusted(-4, -3, 4, 3).contains(pos))
                self.model_list.viewport().setCursor(
                    Qt.CursorShape.PointingHandCursor if over_chip
                    else Qt.CursorShape.ArrowCursor)
            elif event.type() == QEvent.Type.Leave:
                self.model_list.viewport().setCursor(Qt.CursorShape.ArrowCursor)
                self._details.hide()
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        self._details.hide()
        self._details_data = None
        super().hideEvent(event)

    def set_sections(self, sections: list):
        """Populates the list.

        sections: list of dicts with kind 'header'/'subheader' (label) or
        'model' (label, provider_id, model_id, provider_display, is_current,
        effort_levels, effort_current).
        """
        self._details.hide()
        self._details_data = None
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
                item.setToolTip(self._model_tooltip(data))
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

    @staticmethod
    def _model_tooltip(data: dict) -> str:
        if data.get("effort_levels"):
            return (f"Reasoning effort: {effort_label(data.get('effort_current', ''))}. "
                    "Click the model name to switch; click the effort value to change it.")
        return "Click to switch to this model."

    def _effort_chip_rect(self, item) -> QRect:
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") != "model" or not data.get("effort_levels"):
            return QRect()
        text = effort_label(data.get("effort_current", ""))
        rect = self.model_list.visualItemRect(item)
        return _ModelItemDelegate.chip_rect(
            rect, self.model_list.fontMetrics(), text)

    def _build_effort_menu(self, item):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        levels = list(data.get("effort_levels") or [])
        if data.get("kind") != "model" or not levels:
            return None
        return build_effort_menu(
            self, levels, data.get("effort_current", ""),
            lambda level: self._set_effort(data, level))

    def _show_effort_menu(self, item):
        menu = self._build_effort_menu(item)
        if menu is not None:
            menu.exec(QCursor.pos())

    def _set_effort(self, data: dict, level: str):
        pid = data.get("provider_id", "")
        mid = data.get("model_id", "")
        if not pid or not mid or not level:
            return
        if data.get("effort_current") == level:
            return
        for i in range(self.model_list.count()):
            other = self.model_list.item(i)
            odata = other.data(Qt.ItemDataRole.UserRole) or {}
            if (odata.get("kind") == "model" and odata.get("provider_id") == pid
                    and odata.get("model_id") == mid):
                odata["effort_current"] = level
                other.setData(Qt.ItemDataRole.UserRole, odata)
                other.setToolTip(self._model_tooltip(odata))
        shown = self._details_data
        if (shown is not None and shown.get("provider_id") == pid
                and shown.get("model_id") == mid):
            shown["effort_current"] = level
            self._details.setText(self._details_html(shown))
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
        self._details_data = data
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
        self._details_data = None
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

    def _on_list_context_menu(self, pos):
        item = self.model_list.itemAt(pos)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") == "model" and data.get("effort_levels"):
            self._show_effort_menu(item)

    def _on_item_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") != "model":
            return
        click_pos = self.model_list.viewport().mapFromGlobal(QCursor.pos())
        self._activate_model_item(item, data, click_pos)

    def _activate_model_item(self, item, data: dict, click_pos):
        """Chip click edits the effort; anywhere else switches to the model."""
        if self._effort_chip_rect(item).adjusted(-4, -3, 4, 3).contains(click_pos):
            self._show_effort_menu(item)
            return
        logging.info(
            f"ModelSwitchPopup selected: {data.get('provider_id')} / {data.get('model_id')}"
        )
        self.model_selected.emit(data.get("provider_id", ""), data.get("model_id", ""))
        self.close()
