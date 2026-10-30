"""One-click install of curated MCP servers (Settings -> MCP Servers)."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from src.mcp_client import definition_error
from src.mcp_client.catalog import (
    CATALOG,
    CatalogEntry,
    CatalogParameter,
    build_definition,
    ensure_catalog_dir,
    missing_requirements,
)

_MUTED = "color: #9E9E9E;"
_ERROR = "color: #D32F2F;"


class MCPCatalogParametersDialog(QDialog):
    """Collects the few values a catalog entry cannot fill in by itself."""

    def __init__(self, entry: CatalogEntry, parent=None, project_root: str = ""):
        super().__init__(parent)
        self.entry = entry
        self.values: dict = {}
        self._edits: dict[str, QLineEdit] = {}
        self._project_root = project_root

        self.setWindowTitle(f"Install {entry.title}")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        intro = QLabel(f"Choose the values {entry.title} needs. Everything else is filled in for you.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for parameter in entry.parameters:
            edit = QLineEdit(parameter.default)
            self._edits[parameter.key] = edit
            if parameter.kind == "secret":
                edit.setEchoMode(QLineEdit.Password)
                edit.setPlaceholderText("Paste the token")
                form.addRow(f"{parameter.label}:", edit)
            elif parameter.kind in ("folder", "file"):
                row = QHBoxLayout()
                row.addWidget(edit, 1)
                browse = QPushButton("Browse...")
                browse.clicked.connect(lambda _checked=False, p=parameter: self._browse(p))
                row.addWidget(browse)
                form.addRow(f"{parameter.label}:", row)
            else:
                form.addRow(f"{parameter.label}:", edit)
            if parameter.hint:
                hint = QLabel(parameter.hint)
                hint.setWordWrap(True)
                hint.setStyleSheet(_MUTED)
                form.addRow("", hint)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Install")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self, parameter: CatalogParameter):
        edit = self._edits[parameter.key]
        start = self._expanded(edit.text())
        if parameter.kind == "folder":
            if not start or not os.path.isdir(start):
                start = self._project_root or str(Path.home())
            folder = QFileDialog.getExistingDirectory(self, "Choose folder", start)
            if folder:
                edit.setText(folder)
            return
        if start and not os.path.isdir(os.path.dirname(start)):
            start = ""
        path, _filter = QFileDialog.getSaveFileName(
            self, "Choose database file", start,
            "SQLite database (*.db *.sqlite *.sqlite3);;All files (*)",
            options=QFileDialog.DontConfirmOverwrite)
        if path:
            edit.setText(path)

    def _expanded(self, value: str) -> str:
        text = value.strip().replace("${userHome}", str(Path.home()))
        if self._project_root:
            text = text.replace("${workspaceFolder}", self._project_root)
        return "" if "${" in text else text

    def accept(self):
        values = {
            parameter.key: self._edits[parameter.key].text().strip()
            for parameter in self.entry.parameters
        }
        empty = [
            parameter.label for parameter in self.entry.parameters
            if not values.get(parameter.key)
        ]
        if empty:
            QMessageBox.warning(
                self, self.windowTitle(),
                "Enter a value for " + ", ".join(empty) + ".")
            return
        self.values = values
        super().accept()


class MCPCatalogDialog(QDialog):
    """Lists the bundled catalog and installs entries into the user servers."""

    def __init__(self, parent=None, installed=(), project_root: str = "", install=None):
        super().__init__(parent)
        self._installed = {str(name) for name in installed}
        self._project_root = str(project_root or "")
        self._install = install
        self._items: dict[str, QListWidgetItem] = {}

        self.setWindowTitle("MCP Catalog")
        self.setMinimumSize(760, 520)
        self.setModal(True)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Install a standard MCP server without typing commands or paths. "
            "Logos fills in the definition; you only choose the folder, file or "
            "token it cannot know. Add Server and Import stay available for "
            "custom servers.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        content = QHBoxLayout()
        self.list = QListWidget()
        self.list.setMinimumWidth(240)
        self.list.setMaximumWidth(300)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self._update_details)
        self.list.itemDoubleClicked.connect(self._on_double_clicked)
        content.addWidget(self.list)

        details = QVBoxLayout()
        details.setContentsMargins(12, 0, 0, 0)
        self.title_label = QLabel()
        font = self.title_label.font()
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setBold(True)
        self.title_label.setFont(font)
        self.title_label.setWordWrap(True)
        details.addWidget(self.title_label)
        self.meta_label = QLabel()
        self.meta_label.setWordWrap(True)
        self.meta_label.setStyleSheet(_MUTED)
        details.addWidget(self.meta_label)
        self.description_label = QLabel()
        self.description_label.setWordWrap(True)
        details.addWidget(self.description_label)
        self.requirement_label = QLabel()
        self.requirement_label.setWordWrap(True)
        details.addWidget(self.requirement_label)
        details.addStretch()
        content.addLayout(details, 1)
        layout.addLayout(content, 1)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(_MUTED)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.install_button = QPushButton("Install")
        self.install_button.setToolTip("Add the selected server to your user servers.")
        self.install_button.clicked.connect(self._install_selected)
        buttons.addWidget(self.install_button)
        buttons.addStretch()
        self.button_box = QDialogButtonBox(QDialogButtonBox.Close)
        self.button_box.rejected.connect(self.reject)
        buttons.addWidget(self.button_box)
        layout.addLayout(buttons)

        self._populate()

    # -- list -------------------------------------------------------------

    def _add_group(self, title: str):
        item = QListWidgetItem(title)
        item.setFlags(Qt.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.list.addItem(item)

    def _populate(self):
        first = None
        for local in (True, False):
            self._add_group(
                "Local servers (run here)" if local
                else "Remote services (hosted)")
            for entry in CATALOG:
                if entry.local != local:
                    continue
                item = QListWidgetItem()
                item.setData(Qt.UserRole, entry)
                item.setToolTip(entry.description)
                self._items[entry.name] = item
                self._fill_label(entry)
                self.list.addItem(item)
                if first is None:
                    first = item
        if first is not None:
            self.list.setCurrentItem(first)
        else:
            self._update_details()

    def _fill_label(self, entry: CatalogEntry):
        item = self._items.get(entry.name)
        if item is None:
            return
        suffix = "  (installed)" if entry.name in self._installed else ""
        item.setText(entry.title + suffix)

    def _selected_entry(self) -> CatalogEntry | None:
        item = self.list.currentItem()
        data = item.data(Qt.UserRole) if item is not None else None
        return data if isinstance(data, CatalogEntry) else None

    # -- details ----------------------------------------------------------

    def _update_details(self, *_args):
        entry = self._selected_entry()
        if entry is None:
            self.title_label.setText("Select a server")
            self.meta_label.setText("")
            self.description_label.setText("")
            self.requirement_label.setText("")
            self.install_button.setText("Install")
            self.install_button.setEnabled(False)
            return

        self.title_label.setText(entry.title)
        meta = "Runs on this computer (stdio)" if entry.local else "Hosted service (HTTP)"
        if entry.requirement_label:
            meta += f" · Requires {entry.requirement_label}"
        self.meta_label.setText(meta)
        self.description_label.setText(entry.description)

        missing = missing_requirements(entry)
        if missing:
            self.requirement_label.setStyleSheet(_ERROR)
            self.requirement_label.setText(
                "\n".join(f"{label} was not found. {hint}" for label, hint in missing))
        else:
            self.requirement_label.setStyleSheet(_MUTED)
            self.requirement_label.setText("")

        installed = entry.name in self._installed
        self.install_button.setText("Installed" if installed else "Install")
        self.install_button.setEnabled(
            not installed and not missing and self._install is not None)
        if installed:
            self.install_button.setToolTip(
                "This server is already in your user servers. Edit it on the MCP Servers page.")
        elif missing:
            self.install_button.setToolTip("Install the required tool first.")
        else:
            self.install_button.setToolTip("Add the selected server to your user servers.")

    # -- install ----------------------------------------------------------

    def _on_double_clicked(self, item: QListWidgetItem):
        if item.data(Qt.UserRole) is not None:
            self._install_selected()

    def _install_selected(self, *_args):
        entry = self._selected_entry()
        if entry is None or entry.name in self._installed or self._install is None:
            return
        missing = missing_requirements(entry)
        if missing:
            label, hint = missing[0]
            QMessageBox.information(
                self, "MCP Catalog",
                f"\"{entry.title}\" needs {label}.\n\n{hint}")
            return

        values: dict = {}
        if entry.parameters:
            dialog = MCPCatalogParametersDialog(entry, self, project_root=self._project_root)
            if dialog.exec() != QDialog.Accepted:
                return
            values = dialog.values

        definition = build_definition(entry, values)
        error = definition_error(entry.name, definition)
        if error:
            QMessageBox.warning(
                self, "MCP Catalog", f"The definition is not valid.\n\n{error}")
            return

        ensure_catalog_dir()
        error = self._install(entry.name, definition) or ""
        if error:
            self._set_status(error, error=True)
            QMessageBox.warning(self, "MCP Catalog", error)
            return

        self._installed.add(entry.name)
        self._fill_label(entry)
        self._update_details()
        self._set_status(
            f"Installed \"{entry.title}\". It starts automatically and appears "
            "on the MCP Servers page.")

    def _set_status(self, text: str, error: bool = False):
        self.status_label.setStyleSheet(_ERROR if error else _MUTED)
        self.status_label.setText(text)


__all__ = ["MCPCatalogDialog", "MCPCatalogParametersDialog"]
