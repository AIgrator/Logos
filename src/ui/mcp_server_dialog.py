"""Dialogs for adding, editing and importing MCP server definitions."""
from __future__ import annotations

import copy
import json
import os

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_CONNECT_SECONDS = 30


def _create_table(headers) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    return table


def _add_row(table: QTableWidget, name: str = "", value: str = "") -> None:
    row = table.rowCount()
    table.insertRow(row)
    table.setItem(row, 0, QTableWidgetItem(name))
    table.setItem(row, 1, QTableWidgetItem(value))


def _remove_selected(table: QTableWidget) -> None:
    rows = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)
    for row in rows:
        table.removeRow(row)


def _fill_table(table: QTableWidget, values) -> None:
    table.setRowCount(0)
    for name, value in (values or {}).items():
        _add_row(table, str(name), str(value))


def _table_to_dict(table: QTableWidget) -> dict:
    values = {}
    for row in range(table.rowCount()):
        name_item = table.item(row, 0)
        value_item = table.item(row, 1)
        name = name_item.text().strip() if name_item else ""
        value = value_item.text() if value_item else ""
        if name:
            values[name] = value
    return values


def _table_buttons(table: QTableWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    add_button = QPushButton("Add")
    add_button.clicked.connect(lambda: _add_row(table))
    remove_button = QPushButton("Remove Selected")
    remove_button.clicked.connect(lambda: _remove_selected(table))
    row.addWidget(add_button)
    row.addWidget(remove_button)
    row.addStretch()
    return row


class MCPServerDialog(QDialog):
    """Collects one MCP server definition (stdio process or Streamable HTTP)."""

    def __init__(self, parent=None, name: str = "", definition=None, taken_names=()):
        super().__init__(parent)
        self.server_name = name
        self._editing = bool(name)
        self._taken = {str(item) for item in taken_names} - {name}
        self._base = copy.deepcopy(definition) if isinstance(definition, dict) else {}
        self.definition = None

        self.setWindowTitle("Edit MCP Server" if self._editing else "Add MCP Server")
        self.setModal(True)
        self.setMinimumWidth(580)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "MCP servers add tools to Terminal mode. Logos launches local "
            "stdio servers itself and connects to remote Streamable HTTP "
            "servers with the headers you enter.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("github")
        self.name_edit.setToolTip(
            "Letters, digits, '.', '_' or '-'. Used as the server id in "
            "settings.json and in the agent's tool calls.")
        if self._editing:
            self.name_edit.setEnabled(False)
            self.name_edit.setToolTip("The server name cannot be changed after creation.")
        form.addRow("Name:", self.name_edit)

        self.transport_combo = QComboBox()
        self.transport_combo.addItem("Local process (stdio)", "stdio")
        self.transport_combo.addItem("Remote (Streamable HTTP)", "http")
        self.transport_combo.setToolTip(
            "Stdio servers run as a local child process. HTTP servers are "
            "reached by URL; the legacy SSE transport is not supported.")
        form.addRow("Transport:", self.transport_combo)

        self.enabled_checkbox = QCheckBox("Enabled")
        self.enabled_checkbox.setToolTip(
            "Disabled servers stay configured but are not started and their "
            "tools are not offered to the agent.")
        form.addRow("", self.enabled_checkbox)
        layout.addLayout(form)

        self.stdio_widget = self._create_stdio_widget()
        layout.addWidget(self.stdio_widget)

        self.http_widget = self._create_http_widget()
        layout.addWidget(self.http_widget)

        timeouts = QFormLayout()
        timeouts.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 3600)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setToolTip("Upper bound for one tool call.")
        timeouts.addRow("Tool timeout:", self.timeout_spin)
        self.connect_spin = QSpinBox()
        self.connect_spin.setRange(1, 300)
        self.connect_spin.setSuffix(" s")
        self.connect_spin.setToolTip("How long the first connection may take.")
        timeouts.addRow("Connect timeout:", self.connect_spin)
        layout.addLayout(timeouts)

        hint = QLabel(
            "Values for variables whose names look like credentials "
            "(TOKEN, KEY, SECRET, PASSWORD) are stored in the OS keychain; "
            "settings.json keeps only a placeholder. ${workspaceFolder}, "
            "${userHome} and ${env:NAME} are expanded at connect time.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9E9E9E;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_definition()
        self.transport_combo.currentIndexChanged.connect(self._sync_transport)
        self._sync_transport()

    def _create_stdio_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText("npx")
        form.addRow("Command:", self.command_edit)

        self.args_edit = QPlainTextEdit()
        self.args_edit.setPlaceholderText("-y\n@modelcontextprotocol/server-filesystem\n${workspaceFolder}")
        self.args_edit.setFixedHeight(78)
        self.args_edit.setToolTip("One argument per line.")
        form.addRow("Arguments:", self.args_edit)

        cwd_row = QHBoxLayout()
        self.cwd_edit = QLineEdit()
        self.cwd_edit.setPlaceholderText("Defaults to the active project")
        cwd_row.addWidget(self.cwd_edit, 1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse_cwd)
        cwd_row.addWidget(browse_button)
        form.addRow("Working directory:", cwd_row)
        layout.addLayout(form)

        layout.addWidget(QLabel("Environment variables:"))
        self.env_table = _create_table(("Name", "Value"))
        self.env_table.setToolTip(
            "Extra environment for the child process. Credential-like names "
            "are kept in the OS keychain.")
        layout.addWidget(self.env_table)
        layout.addLayout(_table_buttons(self.env_table))
        return widget

    def _create_http_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://mcp.example.com/mcp")
        form.addRow("URL:", self.url_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("Headers:"))
        self.headers_table = _create_table(("Header", "Value"))
        self.headers_table.setToolTip(
            "Sent with every request. Credential-like headers are kept in "
            "the OS keychain.")
        layout.addWidget(self.headers_table)
        layout.addLayout(_table_buttons(self.headers_table))
        return widget

    def _browse_cwd(self):
        start = self.cwd_edit.text().strip() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Choose working directory", start)
        if folder:
            self.cwd_edit.setText(folder)

    def _load_definition(self):
        raw = self._base
        transport = str(raw.get("type") or raw.get("transport") or "").lower()
        if transport in ("streamable-http", "streamablehttp"):
            transport = "http"
        if not transport:
            transport = "http" if raw.get("url") else "stdio"
        index = self.transport_combo.findData(transport)
        self.transport_combo.setCurrentIndex(max(0, index))
        self.enabled_checkbox.setChecked(bool(raw.get("enabled", not raw.get("disabled", False))))
        self.timeout_spin.setValue(int(float(raw.get("timeout") or DEFAULT_TIMEOUT_SECONDS)))
        self.connect_spin.setValue(int(float(raw.get("connect_timeout") or DEFAULT_CONNECT_SECONDS)))
        self.command_edit.setText(str(raw.get("command") or ""))
        args = raw.get("args") or []
        if isinstance(args, (list, tuple)):
            self.args_edit.setPlainText("\n".join(str(arg) for arg in args))
        self.cwd_edit.setText(str(raw.get("cwd") or ""))
        _fill_table(self.env_table, raw.get("env") or {})
        self.url_edit.setText(str(raw.get("url") or ""))
        _fill_table(self.headers_table, raw.get("headers") or {})

    def _sync_transport(self):
        stdio = self.transport_combo.currentData() == "stdio"
        self.stdio_widget.setVisible(stdio)
        self.http_widget.setVisible(not stdio)

    def _build_definition(self) -> dict:
        raw = copy.deepcopy(self._base)
        transport = self.transport_combo.currentData() or "stdio"
        raw.pop("transport", None)
        raw["type"] = transport
        raw["enabled"] = self.enabled_checkbox.isChecked()
        raw["timeout"] = float(self.timeout_spin.value())
        raw["connect_timeout"] = float(self.connect_spin.value())
        if transport == "stdio":
            for key in ("url", "headers"):
                raw.pop(key, None)
            raw["command"] = self.command_edit.text().strip()
            args = [line.strip() for line in self.args_edit.toPlainText().splitlines() if line.strip()]
            if args:
                raw["args"] = args
            else:
                raw.pop("args", None)
            cwd = self.cwd_edit.text().strip()
            if cwd:
                raw["cwd"] = cwd
            else:
                raw.pop("cwd", None)
            env = _table_to_dict(self.env_table)
            if env:
                raw["env"] = env
            else:
                raw.pop("env", None)
        else:
            for key in ("command", "args", "cwd", "env"):
                raw.pop(key, None)
            raw["url"] = self.url_edit.text().strip()
            headers = _table_to_dict(self.headers_table)
            if headers:
                raw["headers"] = headers
            else:
                raw.pop("headers", None)
        return raw

    def accept(self):
        from src.mcp_client import definition_error, server_name_error

        title = self.windowTitle()
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, title, "Enter a server name.")
            return
        error = server_name_error(name)
        if error:
            QMessageBox.warning(self, title, f"Invalid server name.\n\n{error}")
            return
        if name in self._taken:
            QMessageBox.warning(
                self, title,
                f"A server named \"{name}\" already exists.")
            return
        raw = self._build_definition()
        error = definition_error(name, raw)
        if error:
            QMessageBox.warning(self, title, f"The definition is not valid.\n\n{error}")
            return
        self.server_name = name
        self.definition = raw
        super().accept()


class MCPImportDialog(QDialog):
    """Imports server definitions from `.mcp.json`-style JSON."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.servers: dict = {}

        self.setWindowTitle("Import MCP Servers")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Paste a `.mcp.json` (or Claude Desktop `mcpServers`) document, or "
            "load one from disk. Imported servers are added to your user "
            "servers; entries with the same name are replaced.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            '{\n  "mcpServers": {\n    "filesystem": {\n'
            '      "command": "npx",\n'
            '      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]\n'
            '    }\n  }\n}')
        self.editor.setMinimumHeight(220)
        layout.addWidget(self.editor)

        buttons_row = QHBoxLayout()
        file_button = QPushButton("Open File...")
        file_button.clicked.connect(self._open_file)
        buttons_row.addWidget(file_button)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Import")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def _open_file(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open MCP configuration", "",
            "JSON files (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            QMessageBox.warning(self, "Import MCP Servers", f"Could not read the file.\n\n{error}")
            return
        self.editor.setPlainText(text)

    def accept(self):
        from src.mcp_client import definition_error, server_name_error

        text = self.editor.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Import MCP Servers", "Paste JSON or open a file first.")
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            QMessageBox.warning(
                self, "Import MCP Servers", f"The text is not valid JSON.\n\n{error}")
            return
        raw = data.get("mcpServers", data) if isinstance(data, dict) else data
        if not isinstance(raw, dict):
            QMessageBox.warning(
                self, "Import MCP Servers",
                "Expected an object of server definitions (optionally under \"mcpServers\").")
            return

        servers: dict = {}
        errors: list[str] = []
        for raw_name, entry in raw.items():
            name = str(raw_name)
            error = server_name_error(name) or definition_error(name, entry)
            if error:
                errors.append(f"{name}: {error}")
            else:
                servers[name] = copy.deepcopy(entry)
        if errors:
            more = "" if len(errors) <= 8 else f"\n... and {len(errors) - 8} more"
            QMessageBox.warning(
                self, "Import MCP Servers",
                "These entries are not valid and were not imported:\n\n"
                + "\n".join(errors[:8]) + more)
            return
        if not servers:
            QMessageBox.information(self, "Import MCP Servers", "No server definitions found.")
            return
        self.servers = servers
        super().accept()


__all__ = ["MCPImportDialog", "MCPServerDialog"]
