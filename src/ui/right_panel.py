"""Right-hand workspace panel: project files now, Git and context to follow."""
import os
import sys

import qtawesome as qta
from PySide6.QtCore import QDir, QSize, QSortFilterProxyModel, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import editor_launcher
from .navigation import _theme_colors

_HIDDEN_NAMES = frozenset({
    ".git", ".logos", ".idea", ".vscode", ".venv", "venv", "env",
    "__pycache__", "node_modules", "dist", "build", "target",
    ".mypy_cache", ".pytest_cache", "cache",
})


class _ProjectTreeFilter(QSortFilterProxyModel):
    """Hides repository, cache and environment folders from the tree."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.folder_icon = QIcon()
        self.file_icon = QIcon()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        return model.fileName(index) not in _HIDDEN_NAMES

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            source = self.mapToSource(index)
            if source.isValid():
                info = self.sourceModel().fileInfo(source)
                return self.folder_icon if info.isDir() else self.file_icon
        return super().data(index, role)


class RightPanel(QWidget):
    """Fixed-width panel on the right edge of the workspace.

    Files is functional: the tree follows the active project and files open
    in the detected editor. Git is a placeholder until read-only status is
    wired in.
    """

    EXPANDED_WIDTH = 320
    COLLAPSED_WIDTH = 44

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.collapsed = False
        self._root = ""
        self._build_ui()
        self._apply_style()
        theme_changed = getattr(theme_manager, "theme_changed", None)
        if theme_changed is not None:
            theme_changed.connect(self._apply_style)

    # -- construction ---------------------------------------------------

    def _build_ui(self):
        self.setObjectName("RightPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self.EXPANDED_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(4)
        self.title_label = QLabel("WORKSPACE")
        self.title_label.setObjectName("PanelSection")
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.collapse_button = QPushButton()
        self.collapse_button.setObjectName("PanelIconButton")
        self.collapse_button.setFixedSize(26, 26)
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.collapse_button)
        layout.addLayout(header)

        self.project_label = QLabel("No project")
        self.project_label.setObjectName("PanelProject")
        layout.addWidget(self.project_label)

        tabs = QHBoxLayout()
        tabs.setSpacing(4)
        self.files_tab = QPushButton("Files")
        self.files_tab.setObjectName("PanelTab")
        self.files_tab.setCheckable(True)
        self.files_tab.setChecked(True)
        self.git_tab = QPushButton("Git")
        self.git_tab.setObjectName("PanelTab")
        self.git_tab.setCheckable(True)
        tabs.addWidget(self.files_tab)
        tabs.addWidget(self.git_tab)
        tabs.addStretch(1)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tab_group.addButton(self.files_tab, 0)
        self.tab_group.addButton(self.git_tab, 1)
        layout.addLayout(tabs)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_files_page())
        self.stack.addWidget(self._build_git_page())
        self.tab_group.idClicked.connect(self.stack.setCurrentIndex)
        layout.addWidget(self.stack, 1)

    def _build_files_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        self.fs_model = QFileSystemModel(self)
        self.fs_model.setReadOnly(True)
        self.fs_model.setFilter(
            QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.project_filter = _ProjectTreeFilter(self)
        self.project_filter.setSourceModel(self.fs_model)

        self.files_tree = QTreeView(page)
        self.files_tree.setObjectName("PanelTree")
        self.files_tree.setModel(self.project_filter)
        self.files_tree.setHeaderHidden(True)
        self.files_tree.setAnimated(False)
        self.files_tree.setIndentation(14)
        self.files_tree.setIconSize(QSize(16, 16))
        self.files_tree.setUniformRowHeights(True)
        self.files_tree.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.files_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.files_tree.customContextMenuRequested.connect(self._show_file_menu)
        self.files_tree.doubleClicked.connect(self._activate_index)
        for column in range(1, 4):
            self.files_tree.setColumnHidden(column, True)
        page_layout.addWidget(self.files_tree, 1)

        self.empty_label = QLabel("Open a project to browse files.")
        self.empty_label.setObjectName("PanelEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        page_layout.addWidget(self.empty_label, 1)
        self.files_tree.setVisible(False)
        return page

    @staticmethod
    def _build_git_page():
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(
            "Read-only Git status is coming next.\n\n"
            "Branch, changed files and diff stats will appear here.")
        label.setObjectName("PanelEmpty")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        page_layout.addWidget(label, 1)
        return page

    # -- state ----------------------------------------------------------

    def set_project_root(self, root: str):
        root = (root or "").strip()
        if root == self._root:
            return
        self._root = root
        if not root or not os.path.isdir(root):
            self.project_label.setText("No project")
            self.project_label.setToolTip("")
            self.files_tree.setVisible(False)
            self.empty_label.setVisible(True)
            return
        self.project_label.setText(self.project_label.fontMetrics().elidedText(
            os.path.basename(os.path.normpath(root)),
            Qt.TextElideMode.ElideRight, self.EXPANDED_WIDTH - 32))
        self.project_label.setToolTip(root)
        self.fs_model.setRootPath(root)
        source_root = self.fs_model.index(root)
        self.files_tree.setRootIndex(
            self.project_filter.mapFromSource(source_root))
        self.empty_label.setVisible(False)
        self.files_tree.setVisible(True)

    def toggle_collapsed(self):
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, collapsed: bool):
        self.collapsed = bool(collapsed)
        self.setFixedWidth(
            self.COLLAPSED_WIDTH if self.collapsed else self.EXPANDED_WIDTH)
        for widget in (self.title_label, self.project_label,
                       self.files_tab, self.git_tab, self.stack):
            widget.setVisible(not self.collapsed)
        self.collapse_button.setToolTip(
            "Expand panel" if self.collapsed else "Collapse panel")
        self._apply_style()

    # -- file actions ---------------------------------------------------

    def _path_for_index(self, index) -> str:
        if not index.isValid():
            return ""
        return self.fs_model.filePath(self.project_filter.mapToSource(index))

    def _activate_index(self, index):
        path = self._path_for_index(index)
        if path and not os.path.isdir(path):
            editor_launcher.open_path(path)

    def _show_file_menu(self, position):
        path = self._path_for_index(self.files_tree.indexAt(position))
        if not path:
            return
        menu = QMenu(self)
        open_action = menu.addAction("Open in Editor")
        reveal_action = menu.addAction(self._reveal_label())
        copy_action = menu.addAction("Copy Path")
        chosen = menu.exec(self.files_tree.viewport().mapToGlobal(position))
        if chosen is open_action:
            editor_launcher.open_path(path)
        elif chosen is reveal_action:
            editor_launcher.reveal_path(path)
        elif chosen is copy_action:
            QApplication.clipboard().setText(path)

    @staticmethod
    def _reveal_label() -> str:
        if sys.platform == "win32":
            return "Reveal in File Explorer"
        if sys.platform == "darwin":
            return "Reveal in Finder"
        return "Open Containing Folder"

    # -- theme ----------------------------------------------------------

    def _apply_style(self):
        colors = _theme_colors(self.theme_manager)
        self.collapse_button.setIcon(qta.icon(
            "fa5s.chevron-left" if self.collapsed else "fa5s.chevron-right",
            color=colors["muted"]))
        self.project_filter.folder_icon = qta.icon(
            "fa5s.folder", color=colors["muted"])
        self.project_filter.file_icon = qta.icon(
            "fa5s.file-alt", color=colors["muted"])
        if hasattr(self, "files_tree"):
            self.files_tree.viewport().update()
        self.setStyleSheet(f"""
            QWidget#RightPanel {{
                background: {colors['panel']};
                border-left: 1px solid {colors['border']};
            }}
            QLabel#PanelSection {{
                color: {colors['muted']};
                font-size: 11px;
                font-weight: 600;
            }}
            QLabel#PanelProject {{
                color: {colors['text']};
                font-weight: 600;
            }}
            QLabel#PanelEmpty {{ color: {colors['muted']}; }}
            QPushButton#PanelIconButton {{
                border: none;
                background: transparent;
                border-radius: 6px;
            }}
            QPushButton#PanelIconButton:hover {{ background: {colors['hover']}; }}
            QPushButton#PanelTab {{
                border: none;
                background: transparent;
                color: {colors['muted']};
                padding: 5px 10px;
                border-radius: 6px;
            }}
            QPushButton#PanelTab:hover {{ background: {colors['hover']}; }}
            QPushButton#PanelTab:checked {{
                background: {colors['selected']};
                color: {colors['text']};
            }}
            QTreeView#PanelTree {{
                background: transparent;
                border: none;
                outline: none;
                color: {colors['text']};
            }}
            QTreeView#PanelTree::item {{ padding: 3px 2px; border-radius: 4px; }}
            QTreeView#PanelTree::item:hover {{ background: {colors['hover']}; }}
            QTreeView#PanelTree::item:selected {{
                background: {colors['selected']};
                color: {colors['text']};
            }}
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
        """)
