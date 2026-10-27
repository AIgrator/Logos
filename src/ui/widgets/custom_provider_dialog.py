"""Dialog for adding or editing a user-defined OpenAI-compatible provider."""
import re

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from llm.custom_providers import provider_id_error

_HEADER_NAME_RE = re.compile(r"^[^\s:]+$")


class CustomProviderDialog(QDialog):
    """Collects provider fields plus an optional key and custom headers."""

    def __init__(self, parent=None, definition=None, api_key="", reserved_ids=()):
        super().__init__(parent)
        self._original_id = (definition or {}).get("id", "")
        self._reserved_ids = set(reserved_ids) - {self._original_id}
        self.definition = None
        self.api_key = ""

        self.setWindowTitle("Edit Custom Provider" if definition
                            else "Add Custom Provider")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Connect any OpenAI-compatible Chat Completions endpoint "
            "(LM Studio, Ollama, llama.cpp, vLLM, a gateway). "
            "The base URL usually ends with /v1."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("local-llama")
        self.id_edit.setToolTip(
            "Lowercase letters, digits, hyphens or underscores. "
            "Used internally and in settings.json.")
        if definition:
            self.id_edit.setEnabled(False)
            self.id_edit.setToolTip(
                "The provider ID cannot be changed after creation.")
        form.addRow("Provider ID:", self.id_edit)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Local Llama")
        form.addRow("Display name:", self.name_edit)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("http://127.0.0.1:1234/v1")
        form.addRow("Base URL:", self.url_edit)

        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("Leave empty for local servers")
        form.addRow("API key (optional):", self.key_edit)
        layout.addLayout(form)

        models_label = QLabel("<b>Models</b>")
        layout.addWidget(models_label)
        models_hint = QLabel("At least one model id is required. "
                             "The display name is optional.")
        models_hint.setStyleSheet("color: #9E9E9E;")
        layout.addWidget(models_hint)

        self.models_table = self._create_table(
            ("Model ID", "Display name (optional)"))
        layout.addWidget(self.models_table)
        models_buttons = QHBoxLayout()
        add_model_button = QPushButton("Add Model")
        add_model_button.clicked.connect(
            lambda: self._add_row(self.models_table))
        remove_model_button = QPushButton("Remove Selected")
        remove_model_button.clicked.connect(
            lambda: self._remove_selected(self.models_table))
        models_buttons.addWidget(add_model_button)
        models_buttons.addWidget(remove_model_button)
        models_buttons.addStretch()
        layout.addLayout(models_buttons)

        headers_label = QLabel("<b>Custom headers (optional)</b>")
        layout.addWidget(headers_label)
        headers_hint = QLabel(
            "Sent with every request. Useful for gateways that need routing "
            "or organization headers.")
        headers_hint.setStyleSheet("color: #9E9E9E;")
        headers_hint.setWordWrap(True)
        layout.addWidget(headers_hint)

        self.headers_table = self._create_table(("Header", "Value"))
        layout.addWidget(self.headers_table)
        headers_buttons = QHBoxLayout()
        add_header_button = QPushButton("Add Header")
        add_header_button.clicked.connect(
            lambda: self._add_row(self.headers_table))
        remove_header_button = QPushButton("Remove Selected")
        remove_header_button.clicked.connect(
            lambda: self._remove_selected(self.headers_table))
        headers_buttons.addWidget(add_header_button)
        headers_buttons.addWidget(remove_header_button)
        headers_buttons.addStretch()
        layout.addLayout(headers_buttons)

        vision_note = QLabel(
            "Custom providers work in Chat, Terminal and Browser. Vision "
            "requires a native computer-use provider (OpenAI, Anthropic or "
            "Google), so custom providers are not offered there.")
        vision_note.setWordWrap(True)
        vision_note.setStyleSheet(
            "QLabel { padding: 8px 10px; border: none; "
            "border-left: 3px solid palette(highlight); "
            "border-radius: 3px; background: palette(alternate-base); }")
        layout.addWidget(vision_note)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #D32F2F;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load(definition or {}, api_key)

    @staticmethod
    def _create_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(list(headers))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setMinimumHeight(110)
        return table

    def _load(self, definition, api_key):
        self.id_edit.setText(definition.get("id", ""))
        self.name_edit.setText(definition.get("name", ""))
        self.url_edit.setText(definition.get("base_url", ""))
        self.key_edit.setText(api_key or "")

        for model in definition.get("models") or []:
            self._add_row(self.models_table,
                          (model.get("id", ""), model.get("name", "")))
        if self.models_table.rowCount() == 0:
            self._add_row(self.models_table)

        for name, value in (definition.get("headers") or {}).items():
            self._add_row(self.headers_table, (name, value))

    @staticmethod
    def _add_row(table, values=("", "")):
        row = table.rowCount()
        table.insertRow(row)
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(str(value)))
        table.setCurrentCell(row, 0)
        table.editItem(table.item(row, 0))

    @staticmethod
    def _remove_selected(table):
        rows = sorted({index.row() for index in table.selectedIndexes()},
                      reverse=True)
        if not rows:
            rows = [table.currentRow()] if table.currentRow() >= 0 else []
        for row in rows:
            table.removeRow(row)

    @staticmethod
    def _cell(table, row, column) -> str:
        item = table.item(row, column)
        return item.text().strip() if item else ""

    def _set_error(self, message: str):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _validate_and_accept(self):
        provider_id = self.id_edit.text().strip().lower()
        id_error = provider_id_error(provider_id, self._reserved_ids)
        if id_error:
            self._set_error(id_error)
            return

        base_url = self.url_edit.text().strip().rstrip("/")
        if base_url and "://" not in base_url:
            base_url = f"http://{base_url}"
        if not base_url.startswith(("http://", "https://")):
            self._set_error("Base URL must start with http:// or https://.")
            return

        models = []
        seen = set()
        for row in range(self.models_table.rowCount()):
            model_id = self._cell(self.models_table, row, 0)
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            label = self._cell(self.models_table, row, 1) or model_id
            models.append({"id": model_id, "name": label})
        if not models:
            self._set_error("Add at least one model id.")
            return

        headers = {}
        for row in range(self.headers_table.rowCount()):
            name = self._cell(self.headers_table, row, 0)
            if not name:
                continue
            if not _HEADER_NAME_RE.match(name):
                self._set_error(
                    f"Header name '{name}' cannot contain spaces or ':'.")
                return
            headers[name] = self._cell(self.headers_table, row, 1)

        display_name = " ".join(self.name_edit.text().split()) or provider_id
        self.id_edit.setText(provider_id)
        self.definition = {
            "id": provider_id,
            "name": display_name,
            "base_url": base_url,
            "models": models,
            "headers": headers,
        }
        self.api_key = self.key_edit.text().strip()
        self.accept()
