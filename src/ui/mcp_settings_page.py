"""Settings page for user and project MCP servers."""
from __future__ import annotations

import copy
import logging
import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from context.secrets import prune_mcp_secrets
from settings_manager import KEYRING_SERVICE_NAME
from src.ui.editor_launcher import open_path

_STATUS_TEXT = {
    "connected": "Connected",
    "starting": "Starting…",
    "error": "Error",
    "stopped": "Stopped",
    "disabled": "Disabled",
    "needs_trust": "Needs trust",
}

_WAIT_LIMIT_SECONDS = 45.0
_POLL_MS = 700


class MCPSettingsPage(QWidget):
    """Lists MCP servers, their live state and project trust decisions.

    Changes are saved as they are made, so restart/test/logs act on them and
    leaving the settings page or closing the app keeps them. The snapshot is
    only kept so ``commit()`` can prune keychain entries of removed servers.
    """

    def __init__(self, settings_manager, controller, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.controller = controller
        self._snapshot = None
        self._populating = False
        self._rows: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._row_info: dict[tuple[str, str], dict] = {}
        self._waiting: dict[str, float] = {}
        self._log_target = ""
        self._log_text = ""
        self._project_servers_cache: dict = {}

        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._tick)

    # -- layout -----------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("MCP Servers")
        layout = QVBoxLayout(group)

        intro = QLabel(
            "MCP servers give the Terminal agent extra tools. Servers defined "
            "here are available in every project; the active project's "
            ".mcp.json can add more. A project server overrides a user server "
            "with the same name and must be trusted before it starts. Values "
            "whose names look like credentials are kept in the OS keychain.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Server", "Scope", "Transport", "Status", "Tools"))
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setMinimumHeight(230)
        self.tree.setTextElideMode(Qt.ElideRight)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.tree)

        self.config_errors_label = QLabel()
        self.config_errors_label.setWordWrap(True)
        self.config_errors_label.setStyleSheet("color: #C0392B;")
        self.config_errors_label.hide()
        layout.addWidget(self.config_errors_label)

        first_row = QHBoxLayout()
        self.catalog_button = QPushButton("Browse Catalog...")
        self.catalog_button.setToolTip(
            "Install a standard MCP server in one click. Add Server and "
            "Import remain for custom definitions.")
        self.catalog_button.clicked.connect(self._browse_catalog)
        first_row.addWidget(self.catalog_button)
        self.add_button = QPushButton("Add Server...")
        self.add_button.setToolTip("Create a local stdio or remote HTTP server.")
        self.add_button.clicked.connect(self._add_server)
        self.edit_button = QPushButton("Edit...")
        self.edit_button.clicked.connect(self._edit_server)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Delete the selected user server.")
        self.remove_button.clicked.connect(self._remove_server)
        self.import_button = QPushButton("Import...")
        self.import_button.setToolTip(
            "Add servers from a .mcp.json or Claude Desktop mcpServers document.")
        self.import_button.clicked.connect(self._import_servers)
        first_row.addWidget(self.add_button)
        first_row.addWidget(self.edit_button)
        first_row.addWidget(self.remove_button)
        first_row.addWidget(self.import_button)
        first_row.addStretch()
        layout.addLayout(first_row)

        second_row = QHBoxLayout()
        self.restart_button = QPushButton("Restart")
        self.restart_button.setToolTip("Stop and start the selected server again.")
        self.restart_button.clicked.connect(self._restart_server)
        self.test_button = QPushButton("Test")
        self.test_button.setToolTip("Restart the server and report whether it connects.")
        self.test_button.clicked.connect(self._test_server)
        self.logs_button = QPushButton("Logs")
        self.logs_button.setToolTip("Show startup output and call failures for the selected server.")
        self.logs_button.clicked.connect(self._toggle_logs)
        second_row.addWidget(self.restart_button)
        second_row.addWidget(self.test_button)
        second_row.addWidget(self.logs_button)
        second_row.addStretch()
        self.trust_button = QPushButton("Trust Server")
        self.trust_button.clicked.connect(self._toggle_trust)
        second_row.addWidget(self.trust_button)
        self.open_file_button = QPushButton("Open .mcp.json")
        self.open_file_button.setToolTip("Open the active project's MCP configuration.")
        self.open_file_button.clicked.connect(self._open_project_file)
        second_row.addWidget(self.open_file_button)
        self.reload_button = QPushButton("Reload")
        self.reload_button.setToolTip(
            "Re-read settings and the project's .mcp.json, and refresh server states.")
        self.reload_button.clicked.connect(self._reload)
        second_row.addWidget(self.reload_button)
        layout.addLayout(second_row)

        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.hide()
        layout.addWidget(self.message_label)

        self.log_widget = QWidget()
        log_layout = QVBoxLayout(self.log_widget)
        log_layout.setContentsMargins(0, 6, 0, 0)
        self.log_label = QLabel("Logs")
        log_layout.addWidget(self.log_label)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(150)
        self.log_view.setToolTip("Server stderr and connection/call failures.")
        log_layout.addWidget(self.log_view)
        self.log_widget.hide()
        layout.addWidget(self.log_widget)

        outer.addWidget(group)

    # -- lifecycle --------------------------------------------------------

    def load_settings(self):
        self._snapshot = None
        self._waiting.clear()
        self._log_target = ""
        self._log_text = ""
        self.log_widget.hide()
        self.message_label.hide()
        self._populate()

    def showEvent(self, event):
        self._ensure_manager()
        self._populate()
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def commit(self):
        """Drops secrets of removed servers/fields after settings were saved."""
        snapshot = self._snapshot
        self._snapshot = None
        if snapshot is None:
            return
        old = snapshot.get("servers") if isinstance(snapshot, dict) else None
        current = self.settings_manager.get("mcp") or {}
        new = current.get("servers") if isinstance(current, dict) else None
        try:
            prune_mcp_secrets(KEYRING_SERVICE_NAME, old, new)
        except Exception as error:  # noqa: BLE001 - cleanup is best effort
            logging.warning(f"Could not prune MCP secrets: {error}")

    # -- state helpers ----------------------------------------------------

    def _remember_state(self):
        if self._snapshot is not None:
            return
        self._snapshot = copy.deepcopy(self.settings_manager.get("mcp") or {})
        if not isinstance(self._snapshot, dict):
            self._snapshot = {}

    def _mcp_settings(self) -> dict:
        settings = self.settings_manager.get("mcp") or {}
        return copy.deepcopy(settings) if isinstance(settings, dict) else {}

    def _user_servers(self) -> dict:
        servers = self._mcp_settings().get("servers")
        return servers if isinstance(servers, dict) else {}

    def _project_root(self) -> str:
        return str(getattr(self.controller, "project_root", "") or "")

    def _mcp_file(self) -> str:
        root = self._project_root()
        return os.path.join(root, ".mcp.json") if root else ""

    def _project_servers(self) -> dict:
        self.config_errors_label.hide()
        getter = getattr(self.controller, "mcp_config", None)
        if not callable(getter):
            return {}
        try:
            config = getter()
        except Exception as error:  # noqa: BLE001 - settings must still open
            logging.warning(f"Could not read the MCP configuration: {error}")
            self.config_errors_label.setText(f"Could not read the MCP configuration: {error}")
            self.config_errors_label.show()
            return {}
        servers = {
            name: server for name, server in getattr(config, "servers", {}).items()
            if getattr(server, "scope", "") == "project"
        }
        errors = list(getattr(config, "errors", []) or [])
        if errors:
            self.config_errors_label.setText("\n".join(errors))
            self.config_errors_label.show()
        else:
            self.config_errors_label.hide()
        return servers

    def _manager(self):
        return getattr(self.controller, "mcp_manager", None)

    def _ensure_manager(self):
        ensure = getattr(self.controller, "ensure_mcp_manager", None)
        if callable(ensure):
            try:
                return ensure()
            except Exception as error:  # noqa: BLE001 - page keeps working
                logging.warning(f"Could not start the MCP manager: {error}")
        return self._manager()

    def _refresh_manager(self):
        return self._ensure_manager()

    def _set_message(self, text: str):
        self.message_label.setText(text)
        self.message_label.setVisible(bool(text))

    # -- population -------------------------------------------------------

    def _add_group(self, title: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([title, "", "", "", ""])
        item.setFlags(Qt.ItemIsEnabled)
        self.tree.addTopLevelItem(item)
        item.setFirstColumnSpanned(True)
        item.setExpanded(True)
        return item

    def _add_placeholder(self, group: QTreeWidgetItem, text: str):
        item = QTreeWidgetItem([text, "", "", "", ""])
        item.setFlags(Qt.ItemIsEnabled)
        item.setFirstColumnSpanned(True)
        group.addChild(item)

    def _transport_label(self, raw) -> str:
        if not isinstance(raw, dict):
            return ""
        transport = str(raw.get("type") or raw.get("transport") or "").lower()
        if transport in ("streamable-http", "streamablehttp"):
            transport = "http"
        if not transport:
            transport = "http" if raw.get("url") else "stdio"
        return "HTTP" if transport == "http" else "stdio"

    def _definition_summary(self, raw) -> str:
        if not isinstance(raw, dict):
            return "Invalid definition"
        if str(raw.get("type") or "").lower() == "http" or raw.get("url"):
            return str(raw.get("url") or "No URL")
        parts = [str(raw.get("command") or "(no command)")]
        args = raw.get("args")
        if isinstance(args, (list, tuple)):
            parts.extend(str(arg) for arg in args)
        return " ".join(parts)

    def _populate(self, select=None):
        selected = select or self._selected_key()
        self._project_servers_cache = self._project_servers()
        user_servers = self._user_servers()
        project_servers = self._project_servers_cache

        self._populating = True
        try:
            self.tree.clear()
            self._rows = {}
            self._row_info = {}

            user_group = self._add_group("User servers (settings.json)")
            if not user_servers:
                self._add_placeholder(
                    user_group, "No user servers. Use Browse Catalog, Add Server or Import.")
            for name in sorted(user_servers):
                raw = user_servers[name]
                enabled = bool(raw.get("enabled", not raw.get("disabled", False))) if isinstance(raw, dict) else False
                shadowed = name in project_servers
                info = {
                    "scope": "user",
                    "name": name,
                    "enabled": enabled,
                    "trusted": True,
                    "shadowed": shadowed,
                    "transport": self._transport_label(raw),
                    "error": "" if isinstance(raw, dict) else "The definition must be an object.",
                }
                item = QTreeWidgetItem([name, "User", info["transport"], "", ""])
                flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
                item.setFlags(flags)
                item.setCheckState(0, Qt.Checked if enabled else Qt.Unchecked)
                item.setData(0, Qt.UserRole, {"scope": "user", "name": name})
                tooltip = self._definition_summary(raw) + "\nUser server · settings.json"
                if shadowed:
                    tooltip += "\nOverridden by a project server with the same name."
                info["base_tooltip"] = tooltip
                item.setToolTip(0, tooltip)
                user_group.addChild(item)
                self._rows[("user", name)] = item
                self._row_info[("user", name)] = info

            project_group = self._add_group("Project servers (.mcp.json)")
            if not self._project_root():
                self._add_placeholder(project_group, "No active project.")
            elif not os.path.isfile(self._mcp_file()):
                self._add_placeholder(project_group, "No .mcp.json in this project.")
            elif not project_servers:
                self._add_placeholder(project_group, "No servers defined in .mcp.json.")
            for name in sorted(project_servers):
                server = project_servers[name]
                info = {
                    "scope": "project",
                    "name": name,
                    "enabled": bool(getattr(server, "enabled", True)),
                    "trusted": bool(getattr(server, "trusted", False)),
                    "shadowed": False,
                    "transport": "HTTP" if getattr(server, "transport", "") == "http" else "stdio",
                    "error": str(getattr(server, "error", "") or ""),
                }
                item = QTreeWidgetItem([name, "Project", info["transport"], "", ""])
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setData(0, Qt.UserRole, {"scope": "project", "name": name})
                summary = getattr(server, "url", "") or " ".join(
                    [str(getattr(server, "command", "") or "")] + list(getattr(server, "args", []) or []))
                tooltip = f"{summary}\nProject server · {self._mcp_file()}"
                info["base_tooltip"] = tooltip
                item.setToolTip(0, tooltip)
                project_group.addChild(item)
                self._rows[("project", name)] = item
                self._row_info[("project", name)] = info
        finally:
            self._populating = False

        if selected:
            item = self._rows.get(selected)
            if item is not None:
                self.tree.setCurrentItem(item)
        self._refresh_statuses()
        self._update_buttons()

    def _selected_key(self):
        item = self.tree.currentItem() if hasattr(self, "tree") else None
        data = item.data(0, Qt.UserRole) if item is not None else None
        if not isinstance(data, dict):
            return None
        return data.get("scope"), data.get("name")

    # -- live status ------------------------------------------------------

    def _status_text(self, info: dict, status):
        if info["scope"] == "user" and info["shadowed"]:
            return "Overridden by project", ""
        if info.get("error"):
            return "Invalid", ""
        if not info.get("enabled"):
            return "Disabled", ""
        if info["scope"] == "project" and not info.get("trusted"):
            return "Needs trust", ""
        if status is None:
            return "Not started", ""
        state = status.state
        if state == "connected":
            return _STATUS_TEXT["connected"], str(status.tools)
        return _STATUS_TEXT.get(state, state.title()), ""

    def _status_detail(self, info: dict, status) -> str:
        if info.get("error"):
            return info["error"]
        if info["scope"] == "user" and info["shadowed"]:
            return "A project server with the same name takes precedence."
        if not info.get("enabled"):
            return "Disabled in the server settings."
        if info["scope"] == "project" and not info.get("trusted"):
            return "Trust this project server to let it start."
        if status is not None and status.error:
            return status.error
        return ""

    def _refresh_statuses(self):
        manager = self._manager()
        statuses = manager.statuses() if manager is not None else {}
        for key, item in self._rows.items():
            info = self._row_info[key]
            status = statuses.get(info["name"])
            text, tools = self._status_text(info, status)
            item.setText(3, text)
            item.setText(4, tools)
            detail = self._status_detail(info, status)
            item.setToolTip(3, detail or text)
            base = info.get("base_tooltip", "")
            item.setToolTip(0, f"{base}\n\n{detail}" if detail else base)

    def _tick(self):
        if not self.isVisible():
            return
        self._refresh_statuses()
        self._update_buttons()
        self._update_waiting()
        self._render_logs()

    def _update_waiting(self):
        if not self._waiting:
            return
        manager = self._manager()
        statuses = manager.statuses() if manager is not None else {}
        now = time.monotonic()
        for name, deadline in list(self._waiting.items()):
            status = statuses.get(name)
            state = status.state if status is not None else ""
            if state == "connected":
                self._set_message(f"\"{name}\" is connected · {status.tools} tools.")
                self._waiting.pop(name, None)
            elif state in ("error", "needs_trust", "disabled"):
                detail = status.error if status is not None else ""
                self._set_message(f"\"{name}\" could not start: {detail or state}")
                self._waiting.pop(name, None)
            elif now > deadline:
                self._set_message(f"Timed out waiting for \"{name}\".")
                self._waiting.pop(name, None)

    def _render_logs(self):
        if not self._log_target or not self.log_widget.isVisible():
            return
        manager = self._manager()
        lines = manager.logs(self._log_target, 200) if manager is not None else []
        text = "\n".join(lines) if lines else "No log lines yet."
        if text == self._log_text:
            return
        self._log_text = text
        self.log_view.setPlainText(text)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # -- buttons ----------------------------------------------------------

    def _update_buttons(self):
        key = self._selected_key()
        scope, name = key if key else ("", "")
        info = self._row_info.get(key or ("", ""), {})
        is_user = scope == "user"
        is_project = scope == "project"
        manager = self._manager()
        statuses = manager.statuses() if manager is not None else {}
        status = statuses.get(name)
        startable = bool(
            status is not None
            and info.get("enabled")
            and not info.get("shadowed")
            and (is_user or info.get("trusted"))
        )

        self.edit_button.setEnabled(is_user)
        self.remove_button.setEnabled(is_user)
        self.restart_button.setEnabled(startable)
        self.test_button.setEnabled(startable)
        self.logs_button.setEnabled(status is not None and not info.get("shadowed"))
        showing_logs = bool(name) and self._log_target == name and self.log_widget.isVisible()
        self.logs_button.setText("Hide Logs" if showing_logs else "Logs")
        self.trust_button.setVisible(is_project)
        self.trust_button.setEnabled(is_project)
        self.trust_button.setText("Revoke Trust" if info.get("trusted") else "Trust Server")
        self.trust_button.setToolTip(
            "Trusted project servers start automatically with Terminal mode. "
            "Only trust projects you trust.")
        path = self._mcp_file()
        self.open_file_button.setEnabled(bool(path) and os.path.isfile(path))

    def _on_item_changed(self, item, column):
        if self._populating or column != 0:
            return
        data = item.data(0, Qt.UserRole)
        if not isinstance(data, dict) or data.get("scope") != "user":
            return
        key = (data.get("scope"), data.get("name"))
        info = self._row_info.get(key)
        if info is None:
            return
        enabled = item.checkState(0) == Qt.Checked
        if enabled == bool(info.get("enabled")):
            # setData/setToolTip also emit itemChanged on column 0; only a
            # real checkbox toggle edits the server.
            return
        name = data.get("name")
        servers = self._user_servers()
        entry = servers.get(name)
        if not isinstance(entry, dict):
            return
        entry["enabled"] = enabled
        self._remember_state()
        mcp = self._mcp_settings()
        mcp["servers"] = servers
        self.settings_manager.set_many({"mcp": mcp}, save=True, emit=False)
        self._refresh_manager()
        info["enabled"] = enabled
        # No repopulation here: the tree is inside the itemChanged signal of
        # this very item, and clearing it would delete the emitter.
        self._refresh_statuses()
        self._update_buttons()

    def _on_item_double_clicked(self, item, _column):
        data = item.data(0, Qt.UserRole)
        if isinstance(data, dict) and data.get("scope") == "user":
            # Deferred: the edit repopulates the tree, and clearing it inside
            # the item's own signal would delete the emitter.
            QTimer.singleShot(0, self._edit_server)

    # -- edits ------------------------------------------------------------

    def _apply_user_servers(self, servers, select=None):
        self._remember_state()
        mcp = self._mcp_settings()
        mcp["servers"] = servers
        self.settings_manager.set_many({"mcp": mcp}, save=True, emit=False)
        self._refresh_manager()
        self._populate(select=select)

    def _set_trusted(self, name: str, trusted: bool):
        self._remember_state()
        mcp = self._mcp_settings()
        trusted_map = mcp.get("trusted_projects")
        if not isinstance(trusted_map, dict):
            trusted_map = {}
        root = self._project_root()
        names = {str(item) for item in (trusted_map.get(root) or [])}
        if trusted:
            names.add(name)
        else:
            names.discard(name)
        if names:
            trusted_map[root] = sorted(names)
        else:
            trusted_map.pop(root, None)
        mcp["trusted_projects"] = trusted_map
        self.settings_manager.set_many({"mcp": mcp}, save=True, emit=False)
        self._refresh_manager()
        self._populate(select=("project", name))

    def _browse_catalog(self):
        from src.ui.mcp_catalog_dialog import MCPCatalogDialog

        dialog = MCPCatalogDialog(
            self,
            installed=set(self._user_servers()),
            project_root=self._project_root(),
            install=self._install_catalog_entry)
        dialog.exec()

    def _install_catalog_entry(self, name: str, definition: dict) -> str:
        servers = self._user_servers()
        servers[name] = definition
        try:
            self._apply_user_servers(servers, select=("user", name))
        except Exception as error:  # noqa: BLE001 - reported in the catalog
            logging.warning(f"Could not install MCP server '{name}': {error}")
            return f"Could not install \"{name}\": {error}"
        if name in self._project_servers_cache:
            self._set_message(
                f"The project's .mcp.json also defines \"{name}\" and takes "
                "precedence while this project is open.")
        return ""

    def _add_server(self):
        from src.ui.mcp_server_dialog import MCPServerDialog

        servers = self._user_servers()
        dialog = MCPServerDialog(self, taken_names=set(servers))
        if dialog.exec() != QDialog.Accepted or not dialog.definition:
            return
        if dialog.server_name in self._project_servers_cache:
            answer = QMessageBox.question(
                self, "MCP server name",
                f"The project's .mcp.json already defines \"{dialog.server_name}\".\n\n"
                "The project server takes precedence while this project is open. "
                "Add the user server anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        servers[dialog.server_name] = dialog.definition
        self._apply_user_servers(servers, select=("user", dialog.server_name))

    def _edit_server(self):
        from src.ui.mcp_server_dialog import MCPServerDialog

        key = self._selected_key()
        if not key or key[0] != "user":
            return
        name = key[1]
        servers = self._user_servers()
        definition = servers.get(name)
        if not isinstance(definition, dict):
            QMessageBox.warning(
                self, "Edit MCP Server",
                f"The definition of \"{name}\" is not an object. Remove it and add it again.")
            return
        dialog = MCPServerDialog(
            self, name=name, definition=definition, taken_names=set(servers))
        if dialog.exec() != QDialog.Accepted or not dialog.definition:
            return
        servers[name] = dialog.definition
        self._apply_user_servers(servers, select=("user", name))

    def _remove_server(self):
        key = self._selected_key()
        if not key or key[0] != "user":
            return
        name = key[1]
        answer = QMessageBox.question(
            self, "Remove MCP server",
            f"Remove the user server \"{name}\"?\n\n"
            "Stored secrets for it are deleted when you press OK.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        servers = self._user_servers()
        servers.pop(name, None)
        self._apply_user_servers(servers)

    def _import_servers(self):
        from src.ui.mcp_server_dialog import MCPImportDialog

        dialog = MCPImportDialog(self)
        if dialog.exec() != QDialog.Accepted or not dialog.servers:
            return

        from src.mcp_client import definition_error

        accepted = {}
        rejected = []
        for name, definition in dialog.servers.items():
            error = definition_error(name, definition)
            if error:
                rejected.append(f"{name}: {error}")
            else:
                accepted[name] = definition
        if rejected:
            more = "" if len(rejected) <= 8 else f"\n... and {len(rejected) - 8} more"
            QMessageBox.warning(
                self, "Import MCP Servers",
                "These servers were not imported:\n\n" + "\n".join(rejected[:8]) + more)
        if not accepted:
            return

        servers = self._user_servers()
        clashes = sorted(set(accepted) & set(servers))
        if clashes:
            more = "" if len(clashes) <= 6 else f"\n... and {len(clashes) - 6} more"
            answer = QMessageBox.question(
                self, "Import MCP Servers",
                f"Replace {len(clashes)} existing server(s)?\n\n"
                + "\n".join(clashes[:6]) + more,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        servers.update(accepted)
        self._apply_user_servers(servers)

    def _toggle_trust(self):
        key = self._selected_key()
        if not key or key[0] != "project":
            return
        name = key[1]
        info = self._row_info.get(key, {})
        if info.get("trusted"):
            self._set_trusted(name, False)
            self._set_message(f"Trust revoked for \"{name}\"; the server was stopped.")
            return
        server = self._project_servers_cache.get(name)
        summary = self._definition_summary({
            "url": getattr(server, "url", ""),
            "command": getattr(server, "command", ""),
            "args": getattr(server, "args", []),
        }) if server is not None else name
        answer = QMessageBox.question(
            self, "Trust project MCP server",
            f"Trust \"{name}\" from this project's .mcp.json?\n\n{summary}\n\n"
            "Trusted project servers start automatically with Terminal mode and "
            "can run code on this computer. Only trust projects you trust.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._set_trusted(name, True)

    def _start_selected(self, label: str):
        key = self._selected_key()
        if not key:
            return
        name = key[1]
        manager = self._ensure_manager()
        if manager is None:
            self._set_message("MCP is not available. Check the configuration and try Reload.")
            return
        try:
            manager.restart(name)
        except Exception as error:  # noqa: BLE001 - reported to the user
            self._set_message(f"Could not {label.lower()} \"{name}\": {error}")
            return
        self._waiting[name] = time.monotonic() + _WAIT_LIMIT_SECONDS
        self._set_message(f"{label} \"{name}\"…")
        self._refresh_statuses()
        self._update_buttons()

    def _restart_server(self):
        self._start_selected("Restarting")

    def _test_server(self):
        self._start_selected("Testing")

    def _toggle_logs(self):
        key = self._selected_key()
        if not key:
            return
        name = key[1]
        if self.log_widget.isVisible() and self._log_target == name:
            self.log_widget.hide()
            self._log_target = ""
            self._log_text = ""
            self._update_buttons()
            return
        self._log_target = name
        self.log_label.setText(f"Logs · {name}")
        self._log_text = ""
        self.log_widget.show()
        self._render_logs()
        self._update_buttons()

    def _open_project_file(self):
        path = self._mcp_file()
        if not path or not os.path.isfile(path):
            self._set_message("This project has no .mcp.json file.")
            return
        if not open_path(path):
            self._set_message(f"Could not open {path}.")

    def _reload(self):
        refresh = getattr(self.controller, "refresh_mcp_servers", None)
        if callable(refresh):
            try:
                refresh()
            except Exception as error:  # noqa: BLE001 - keep the page usable
                logging.warning(f"Could not refresh MCP servers: {error}")
        self._set_message("")
        self._populate()


__all__ = ["MCPSettingsPage"]
