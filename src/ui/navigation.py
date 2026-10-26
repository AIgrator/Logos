"""Home screen and project sidebar (opencode-style navigation)."""
import logging
import os
import subprocess
import sys
import zlib
from datetime import date, datetime, timedelta

import qtawesome as qta
from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QMenu, QPushButton,
                               QVBoxLayout, QWidget)


AVATAR_COLORS = [
    "#3b82f6", "#8b5cf6", "#ec4899", "#f59e0b", "#10b981",
    "#06b6d4", "#ef4444", "#6366f1", "#84cc16", "#f97316",
]


def _theme_colors(theme_manager) -> dict:
    dark = getattr(theme_manager, "current_theme", "dark") == "dark"
    return {
        "bg": "#0d1117" if dark else "#ffffff",
        "panel": "#161b22" if dark else "#f6f8fa",
        "text": "#e6edf3" if dark else "#1f2328",
        "muted": "#8b949e" if dark else "#6e7781",
        "border": "#30363d" if dark else "#d0d7de",
        "hover": "rgba(255, 255, 255, 0.07)" if dark else "rgba(0, 0, 0, 0.06)",
        "selected": "rgba(255, 255, 255, 0.13)" if dark else "rgba(0, 0, 0, 0.09)",
    }


def make_letter_icon(name: str, size: int = 26) -> QIcon:
    """Colored circle with the project's first letter (stable color per name)."""
    label = (name or "?").strip()[:1].upper() or "?"
    color = AVATAR_COLORS[zlib.crc32((name or "").encode("utf-8")) % len(AVATAR_COLORS)]
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(0, 0, size, size)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(max(10, size // 2))
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, label)
    painter.end()
    return QIcon(pixmap)


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _reveal_in_explorer(path: str) -> None:
    """Opens the folder, or selects a file, in the system file manager."""
    target = os.path.normpath(path or "")
    if not target or not os.path.exists(target):
        return
    try:
        if sys.platform.startswith("win") and os.path.isfile(target):
            # explorer /select needs the comma glued to the path; its exit
            # code is 1 even on success, so it is not checked.
            subprocess.Popen(["explorer", f"/select,{target}"])
            return
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    except Exception as exc:
        logging.warning(f"Could not open the file manager for {path}: {exc}")


def _ask_project_name(parent, current: str) -> str:
    name, ok = QInputDialog.getText(
        parent, "Rename Project", "Project name:", text=current or "")
    if not ok:
        return ""
    name = " ".join((name or "").split())
    return "" if name == current else name


def _confirm_project_removal(parent, name: str) -> tuple:
    """Returns ``(confirmed, delete_data)``."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("Remove Project")
    box.setText(f'Remove "{name}" from the project list?')
    box.setInformativeText("The project folder itself is not deleted.")
    delete_data = QCheckBox(
        "Also delete the .logos folder (sessions, context, attachments)")
    box.setCheckBox(delete_data)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel)
    confirmed = box.exec() == QMessageBox.Yes
    return confirmed, bool(delete_data.isChecked())


def _ask_session_title(parent, current: str) -> str:
    title, ok = QInputDialog.getText(
        parent, "Rename Session", "Session title:", text=current or "")
    if not ok:
        return ""
    title = " ".join((title or "").split())
    return "" if title == current else title


def _confirm_session_deletion(parent, title: str) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("Delete Session")
    box.setText(f'Delete "{_truncate(title or "this session", 60)}"?')
    box.setInformativeText("This cannot be undone.")
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel)
    return box.exec() == QMessageBox.Yes


class _ProjectMenuMixin:
    """Right-click menu shared by the home screen and the sidebar list."""

    def _project_name(self, path: str) -> str:
        for project in self._projects:
            if project.get("path") == path:
                return project.get("name") or os.path.basename(path)
        return os.path.basename(path.rstrip("\\/")) or path

    def _show_project_item_menu(self, pos):
        item = self.project_list.itemAt(pos)
        path = item.data(Qt.UserRole) if item is not None else ""
        if not path:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("Rename...")
        reveal_action = menu.addAction("Show in Explorer")
        menu.addSeparator()
        remove_action = menu.addAction("Remove Project...")
        chosen = menu.exec(self.project_list.viewport().mapToGlobal(pos))
        if chosen is rename_action:
            name = _ask_project_name(self, self._project_name(path))
            if name:
                self.project_renamed.emit(path, name)
        elif chosen is reveal_action:
            _reveal_in_explorer(path)
        elif chosen is remove_action:
            confirmed, delete_data = _confirm_project_removal(
                self, self._project_name(path))
            if confirmed:
                self.project_removed.emit(path, delete_data)


class HomePage(_ProjectMenuMixin, QWidget):
    """Start screen: projects on the left, recent sessions on the right."""

    project_selected = Signal(str)
    session_selected = Signal(str)
    open_project_requested = Signal()
    new_session_requested = Signal()
    settings_requested = Signal()
    project_renamed = Signal(str, str)   # project path, new name
    project_removed = Signal(str, bool)  # project path, delete .logos data
    session_renamed = Signal(str, str)   # session path, new title
    session_deleted = Signal(str)        # session path

    def __init__(self, project_manager, theme_manager, parent=None):
        super().__init__(parent)
        self.project_manager = project_manager
        self.theme_manager = theme_manager
        self._projects = []
        self._sessions = []
        self._build_ui()
        self.refresh()
        if hasattr(self.theme_manager, "theme_changed"):
            self.theme_manager.theme_changed.connect(self._apply_style)

    def _build_ui(self):
        colors = _theme_colors(self.theme_manager)
        self.setObjectName("HomePage")

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(18)

        header = QHBoxLayout()
        logo = QLabel("Logos")
        logo.setObjectName("HomeTitle")
        header.addWidget(logo)
        header.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search projects and sessions")
        self.search.setMinimumWidth(360)
        self.search.textChanged.connect(self._populate)
        header.addWidget(self.search)
        self.settings_button = QPushButton()
        self.settings_button.setIcon(qta.icon("fa5s.cog", color=colors["text"]))
        self.settings_button.setToolTip("Settings")
        self.settings_button.setFixedSize(34, 34)
        self.settings_button.clicked.connect(self.settings_requested.emit)
        header.addWidget(self.settings_button)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(28)

        left = QWidget()
        left.setFixedWidth(300)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        projects_header = QHBoxLayout()
        projects_label = QLabel("Projects")
        projects_label.setObjectName("SectionTitle")
        projects_header.addWidget(projects_label)
        projects_header.addStretch(1)
        self.project_menu_button = QPushButton()
        self.project_menu_button.setIcon(qta.icon("fa5s.plus", color=colors["text"]))
        self.project_menu_button.setToolTip("Add project folder")
        self.project_menu_button.setFixedSize(30, 30)
        self.project_menu_button.clicked.connect(self.open_project_requested.emit)
        projects_header.addWidget(self.project_menu_button)
        left_layout.addLayout(projects_header)
        self.project_list = QListWidget()
        self.project_list.setIconSize(QSize(26, 26))
        self.project_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.project_list.setTextElideMode(Qt.ElideRight)
        self.project_list.itemClicked.connect(self._on_project_clicked)
        self.project_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(
            self._show_project_item_menu)
        left_layout.addWidget(self.project_list, 1)
        body.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        sessions_header = QHBoxLayout()
        sessions_label = QLabel("Recent sessions")
        sessions_label.setObjectName("SectionTitle")
        sessions_header.addWidget(sessions_label)
        sessions_header.addStretch(1)
        self.new_session_button = QPushButton("New session")
        self.new_session_button.setIcon(qta.icon("fa5s.pen", color=colors["text"]))
        self.new_session_button.clicked.connect(self.new_session_requested.emit)
        sessions_header.addWidget(self.new_session_button)
        right_layout.addLayout(sessions_header)
        self.session_list = QListWidget()
        self.session_list.setIconSize(QSize(24, 24))
        self.session_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.session_list.setTextElideMode(Qt.ElideRight)
        self.session_list.itemClicked.connect(self._on_session_clicked)
        self.session_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(
            self._show_session_item_menu)
        right_layout.addWidget(self.session_list, 1)
        body.addWidget(right, 1)

        root.addLayout(body, 1)
        self._apply_style()

    def _apply_style(self):
        colors = _theme_colors(self.theme_manager)
        self.setStyleSheet(f"""
            QWidget#HomePage {{ background: {colors['bg']}; }}
            QLabel {{ color: {colors['text']}; background: transparent; }}
            QLabel#HomeTitle {{ font-size: 22px; font-weight: 600; }}
            QLabel#SectionTitle {{ font-size: 13px; font-weight: 600; color: {colors['muted']}; }}
            QLabel#Hint {{ color: {colors['muted']}; }}
            QLineEdit {{
                background: {colors['panel']}; color: {colors['text']};
                border: 1px solid {colors['border']}; border-radius: 8px;
                padding: 8px 12px;
            }}
            QListWidget {{ background: transparent; border: none; outline: none; }}
            QListWidget::item {{
                color: {colors['text']}; padding: 7px 8px;
                border-radius: 7px;
            }}
            QListWidget::item:hover {{ background: {colors['hover']}; }}
            QListWidget::item:selected {{ background: {colors['selected']}; color: {colors['text']}; }}
            QScrollBar:vertical {{
                background: transparent; width: 6px; margin: 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background: {colors['border']}; min-height: 24px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {colors['muted']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; background: none; border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QScrollBar:horizontal {{ height: 0; }}
            QPushButton {{
                background: transparent; color: {colors['text']};
                border: none; border-radius: 7px; padding: 6px 8px;
                text-align: left;
            }}
            QPushButton:hover {{ background: {colors['hover']}; }}
        """)
        self.settings_button.setIcon(qta.icon("fa5s.cog", color=colors["text"]))
        self.project_menu_button.setIcon(qta.icon("fa5s.plus", color=colors["text"]))
        self.new_session_button.setIcon(qta.icon("fa5s.pen", color=colors["text"]))

    def refresh(self):
        if self.project_manager is None:
            self._projects = []
            self._sessions = []
        else:
            self._projects = self.project_manager.recent_projects()
            self._sessions = self.project_manager.recent_sessions(limit=50)
        self.new_session_button.setEnabled(bool(
            self.project_manager and self.project_manager.get_active_root()))
        self._populate()

    def _populate(self, *_args):
        query = self.search.text().strip().lower()
        self.project_list.blockSignals(True)
        self.project_list.clear()
        active = self.project_manager.get_active_root() if self.project_manager else ""
        for project in self._projects:
            name = project.get("name") or os.path.basename(project.get("path", ""))
            path = project.get("path", "")
            if query and query not in name.lower() and query not in path.lower():
                continue
            item = QListWidgetItem(make_letter_icon(name), name)
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self.project_list.addItem(item)
            if path == active:
                self.project_list.setCurrentItem(item)
        if self.project_list.count() == 0:
            empty = QListWidgetItem("No projects yet. Use + to add one.")
            empty.setFlags(Qt.NoItemFlags)
            self.project_list.addItem(empty)
        self.project_list.blockSignals(False)

        self.session_list.blockSignals(True)
        self.session_list.clear()
        last_day = None
        today = date.today()
        for session in self._sessions:
            title = session.get("title") or "Empty session"
            project = session.get("project_name") or ""
            if query and query not in title.lower() and query not in project.lower():
                continue
            try:
                day = datetime.fromtimestamp(session.get("mtime", 0)).date()
            except (OverflowError, OSError, ValueError):
                day = today
            if day != last_day:
                if day == today:
                    label = "Today"
                elif day == today - timedelta(days=1):
                    label = "Yesterday"
                else:
                    label = day.strftime("%d %b %Y")
                header = QListWidgetItem(label)
                header.setFlags(Qt.NoItemFlags)
                header.setData(Qt.UserRole, "")
                self.session_list.addItem(header)
                last_day = day
            icon = make_letter_icon(project, 24)
            item = QListWidgetItem(icon, _truncate(title, 90))
            item.setData(Qt.UserRole, session.get("path", ""))
            objective = session.get("objective") or ""
            tooltip = f"{project}\n{title}"
            if objective:
                tooltip += f"\nGoal: {objective}"
            tooltip += f"\n{session.get('path', '')}"
            item.setToolTip(tooltip)
            self.session_list.addItem(item)
        if not self._sessions:
            empty = QListWidgetItem("No sessions yet.")
            empty.setFlags(Qt.NoItemFlags)
            self.session_list.addItem(empty)
        self.session_list.blockSignals(False)

    def _on_project_clicked(self, item):
        path = item.data(Qt.UserRole) if item is not None else ""
        if path:
            self.project_selected.emit(path)

    def _on_session_clicked(self, item):
        path = item.data(Qt.UserRole) if item is not None else ""
        if path:
            self.session_selected.emit(path)

    # -------------------------------------------------- session context menu
    def _session_info(self, path: str) -> dict:
        for session in self._sessions:
            if session.get("path") == path:
                return session
        return {}

    def _show_session_item_menu(self, pos):
        item = self.session_list.itemAt(pos)
        path = item.data(Qt.UserRole) if item is not None else ""
        if not path:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("Rename...")
        reveal_action = menu.addAction("Show in Explorer")
        menu.addSeparator()
        delete_action = menu.addAction("Delete Session...")
        chosen = menu.exec(self.session_list.viewport().mapToGlobal(pos))
        if chosen is rename_action:
            info = self._session_info(path)
            current = info.get("title") or "Empty session"
            title = _ask_session_title(self, current)
            if title:
                self.session_renamed.emit(path, title)
        elif chosen is reveal_action:
            _reveal_in_explorer(path)
        elif chosen is delete_action:
            info = self._session_info(path)
            if _confirm_session_deletion(self, info.get("title") or ""):
                self.session_deleted.emit(path)


class ProjectSidebar(_ProjectMenuMixin, QWidget):
    """Collapsible workspace sidebar: Home, projects, new session, settings."""

    home_requested = Signal()
    project_selected = Signal(str)
    new_session_requested = Signal()
    context_requested = Signal()
    journal_requested = Signal()
    settings_requested = Signal()
    project_renamed = Signal(str, str)   # project path, new name
    project_removed = Signal(str, bool)  # project path, delete .logos data

    EXPANDED_WIDTH = 238
    COLLAPSED_WIDTH = 54

    def __init__(self, project_manager, theme_manager, parent=None):
        super().__init__(parent)
        self.project_manager = project_manager
        self.theme_manager = theme_manager
        self.collapsed = False
        self._projects = []
        self._build_ui()
        self.refresh()
        if hasattr(self.theme_manager, "theme_changed"):
            self.theme_manager.theme_changed.connect(self._apply_style)

    def _build_ui(self):
        colors = _theme_colors(self.theme_manager)
        self.setObjectName("ProjectSidebar")
        self.setFixedWidth(self.EXPANDED_WIDTH)

        root = QVBoxLayout(self)
        # Equal, deliberately small air on both sides in collapsed and
        # expanded modes. The window itself is flush with the panel on the
        # left (see UIManager.show_workspace), so this is the only inset the
        # user sees around the rail.
        root.setContentsMargins(6, 8, 6, 8)
        root.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(2)
        top.addStretch(0)
        self.collapse_button = QPushButton()
        self.collapse_button.setIcon(qta.icon("fa5s.bars", color=colors["muted"]))
        self.collapse_button.setToolTip("Collapse sidebar")
        self.collapse_button.setFixedSize(34, 34)
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        top.addWidget(self.collapse_button)
        top.addStretch(1)
        self._top_layout = top
        root.addLayout(top)

        self.home_button = QPushButton("Home")
        self.home_button.setIcon(qta.icon("fa5s.home", color=colors["text"]))
        self.home_button.setToolTip("Home")
        self.home_button.clicked.connect(self.home_requested.emit)
        root.addWidget(self.home_button)

        self.section_label = QLabel("PROJECTS")
        self.section_label.setObjectName("SidebarSection")
        self.section_label.setContentsMargins(13, 8, 6, 0)
        root.addWidget(self.section_label)

        self.project_list = QListWidget()
        self.project_list.setIconSize(QSize(24, 24))
        self.project_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.project_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.project_list.setTextElideMode(Qt.ElideRight)
        self.project_list.setWordWrap(False)
        self.project_list.itemClicked.connect(self._on_project_clicked)
        self.project_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(
            self._show_project_item_menu)
        root.addWidget(self.project_list, 1)

        self.new_session_button = QPushButton("New session")
        self.new_session_button.setIcon(qta.icon("fa5s.pen", color=colors["text"]))
        self.new_session_button.setToolTip("New session in the current project")
        self.new_session_button.clicked.connect(self.new_session_requested.emit)
        root.addWidget(self.new_session_button)

        self.context_button = QPushButton("Context")
        self.context_button.setIcon(qta.icon("fa5s.file-alt", color=colors["text"]))
        self.context_button.setToolTip("Show session context summary")
        self.context_button.clicked.connect(self.context_requested.emit)
        root.addWidget(self.context_button)

        self.journal_button = QPushButton("Journal")
        self.journal_button.setIcon(qta.icon("fa5s.list", color=colors["text"]))
        self.journal_button.setToolTip("Show the current session journal")
        self.journal_button.clicked.connect(self.journal_requested.emit)
        root.addWidget(self.journal_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setIcon(qta.icon("fa5s.cog", color=colors["text"]))
        self.settings_button.setToolTip("Settings")
        self.settings_button.clicked.connect(self.settings_requested.emit)
        root.addWidget(self.settings_button)
        self._apply_style()

    def _apply_style(self):
        colors = _theme_colors(self.theme_manager)
        # Collapsed mode has no labels: centre the icons in the narrow rail.
        # The rail is 42 px wide (54 minus the 6 px root insets), so the 16 px
        # button glyphs sit 19 px from the panel edge and the 24 px project
        # tiles 12 px from it. Expanded mode repeats those exact offsets
        # instead of pushing the icons to the edge, so nothing moves.
        button_align = "center" if self.collapsed else "left"
        button_padding = "7px 0" if self.collapsed else "7px 13px"
        # Project tiles keep their collapsed inset in both modes.
        item_padding = "7px 6px"
        self.setStyleSheet(f"""
            QWidget#ProjectSidebar {{
                background: {colors['panel']};
                border-right: 1px solid {colors['border']};
            }}
            QLabel {{ color: {colors['muted']}; background: transparent; }}
            QLabel#SidebarSection {{ font-size: 11px; font-weight: 600; }}
            QListWidget {{ background: transparent; border: none; outline: none; }}
            QListWidget::item {{
                color: {colors['text']}; padding: {item_padding}; border-radius: 7px;
            }}
            QListWidget::item:hover {{ background: {colors['hover']}; }}
            QListWidget::item:selected {{ background: {colors['selected']}; color: {colors['text']}; }}
            QScrollBar:vertical {{
                background: transparent; width: 6px; margin: 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background: {colors['border']}; min-height: 24px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {colors['muted']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; background: none; border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QScrollBar:horizontal {{ height: 0; }}
            QPushButton {{
                background: transparent; color: {colors['text']};
                border: none; border-radius: 7px; padding: {button_padding};
                text-align: {button_align};
            }}
            QPushButton:hover {{ background: {colors['hover']}; }}
        """)
        self.collapse_button.setIcon(qta.icon("fa5s.bars", color=colors["muted"]))
        self.home_button.setIcon(qta.icon("fa5s.home", color=colors["text"]))
        self.new_session_button.setIcon(qta.icon("fa5s.pen", color=colors["text"]))
        self.context_button.setIcon(qta.icon("fa5s.file-alt", color=colors["text"]))
        self.journal_button.setIcon(qta.icon("fa5s.list", color=colors["text"]))
        self.settings_button.setIcon(qta.icon("fa5s.cog", color=colors["text"]))
        self._render_projects()

    def toggle_collapsed(self):
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, collapsed: bool):
        self.collapsed = bool(collapsed)
        self.setFixedWidth(self.COLLAPSED_WIDTH if self.collapsed else self.EXPANDED_WIDTH)
        self.collapse_button.setToolTip("Expand sidebar" if self.collapsed else "Collapse sidebar")
        self.home_button.setText("" if self.collapsed else "Home")
        self.new_session_button.setText("" if self.collapsed else "New session")
        self.context_button.setText("" if self.collapsed else "Context")
        self.journal_button.setText("" if self.collapsed else "Journal")
        self.settings_button.setText("" if self.collapsed else "Settings")
        self.section_label.setVisible(not self.collapsed)
        self._top_layout.setStretch(0, 1 if self.collapsed else 0)
        self._apply_style()

    def refresh(self):
        self._projects = self.project_manager.recent_projects() if self.project_manager else []
        self._render_projects()

    def _render_projects(self):
        self.project_list.blockSignals(True)
        self.project_list.clear()
        active = self.project_manager.get_active_root() if self.project_manager else ""
        for project in self._projects:
            name = project.get("name") or os.path.basename(project.get("path", ""))
            path = project.get("path", "")
            item = QListWidgetItem(make_letter_icon(name), "" if self.collapsed else name)
            item.setData(Qt.UserRole, path)
            item.setToolTip(name)
            self.project_list.addItem(item)
            if path == active:
                self.project_list.setCurrentItem(item)
                self.project_list.scrollToItem(
                    item, QAbstractItemView.EnsureVisible)
        if not self._projects:
            empty = QListWidgetItem("" if self.collapsed else "No projects yet")
            empty.setFlags(Qt.NoItemFlags)
            self.project_list.addItem(empty)
        self.project_list.blockSignals(False)

    def set_active_project(self, path: str):
        for row in range(self.project_list.count()):
            item = self.project_list.item(row)
            if item.data(Qt.UserRole) == path:
                self.project_list.setCurrentItem(item)
                self.project_list.scrollToItem(
                    item, QAbstractItemView.EnsureVisible)
                return

    def _on_project_clicked(self, item):
        path = item.data(Qt.UserRole) if item is not None else ""
        if path:
            self.project_selected.emit(path)
