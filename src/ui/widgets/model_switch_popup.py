"""OpenCode-style popup for in-session LLM model switching.

Shows provider groups (only providers with an API key configured), a
search field, and the current model marked with a check. Emits
model_selected(provider_id, model_id) with the stored model id
(e.g. "gemini-3.8-flash" or "anthropic/claude-sonnet-5.5" for OpenRouter).
"""
import logging

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)


class ModelSwitchPopup(QDialog):
    model_selected = Signal(str, str)  # provider_id, stored model id

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
        self.search.setPlaceholderText("Поиск моделей")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.model_list = QListWidget(self)
        self.model_list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.model_list, 1)

    def set_sections(self, sections: list):
        """Populates the list.

        sections: list of dicts with kind 'header'/'subheader' (label) or
        'model' (label, provider_id, model_id, is_current).
        """
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
                item.setData(Qt.ItemDataRole.UserRole, {
                    "kind": "model",
                    "label": entry.get("label", ""),
                    "provider_id": entry.get("provider_id", ""),
                    "model_id": entry.get("model_id", ""),
                })
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

    def _apply_filter(self, query: str):
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
