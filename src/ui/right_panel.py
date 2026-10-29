"""Right-hand workspace panel: session statistics and recap, activity, files,
and read-only Git status."""
import html
import logging
import os
import sys
from datetime import datetime

import qtawesome as qta
from PySide6.QtCore import (
    QDir,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QFileSystemModel,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from attachments import format_size
from git_status import read_status
from . import editor_launcher
from .navigation import _theme_colors

_HIDDEN_NAMES = frozenset({
    ".git", ".logos", ".idea", ".vscode", ".venv", "venv", "env",
    "__pycache__", "node_modules", "dist", "build", "target",
    ".mypy_cache", ".pytest_cache", "cache",
})

_GIT_GROUPS = (
    ("staged", "STAGED"),
    ("changes", "CHANGES"),
    ("conflicts", "CONFLICTS"),
    ("untracked", "UNTRACKED"),
)

_GIT_STATUS_COLORS = {
    "dark": {
        "A": "#3fb950", "M": "#d29922", "D": "#f85149", "R": "#58a6ff",
        "C": "#58a6ff", "T": "#d29922", "U": "#f85149", "?": "#8b949e",
    },
    "light": {
        "A": "#1a7f37", "M": "#9a6700", "D": "#cf222e", "R": "#0969da",
        "C": "#0969da", "T": "#9a6700", "U": "#cf222e", "?": "#6e7781",
    },
}


class _GitStatusWorker(QThread):
    """Reads Git status off the GUI thread (read-only commands only)."""

    status_ready = Signal(str, dict)

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self._root = root

    def run(self):
        try:
            status = read_status(self._root)
        except Exception as exc:
            logging.warning("Could not read Git status: %s", exc)
            status = {"error": f"Could not read Git status. {exc}"}
        self.status_ready.emit(self._root, status)


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

    Session (context/token statistics, then a recap of this session) and
    Activity (skills, tools and attached files) plus Files (read-only project
    tree, files open in the detected editor) are available in every mode. Git
    (read-only branch and working-tree status) appears only in Terminal mode.
    """

    EXPANDED_WIDTH = 320
    COLLAPSED_WIDTH = 44

    PAGE_SESSION = 0
    PAGE_ACTIVITY = 1
    PAGE_FILES = 2
    PAGE_GIT = 3

    attachment_open_requested = Signal(str)

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.collapsed = False
        self._mode = ""
        self._root = ""
        self._last_title = ""
        self._last_summary = {}
        self._last_skills = []
        self._last_tools = []
        self._last_attachments = []
        self._last_git_status = None
        self._git_worker = None
        self._git_refresh_pending = False
        self._git_live_timer = QTimer(self)
        self._git_live_timer.setSingleShot(True)
        self._git_live_timer.setInterval(1500)
        self._git_live_timer.timeout.connect(self.refresh_git_status)
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
        self.session_tab = QPushButton("Session")
        self.session_tab.setObjectName("PanelTab")
        self.session_tab.setCheckable(True)
        self.session_tab.setChecked(True)
        self.activity_tab = QPushButton("Activity")
        self.activity_tab.setObjectName("PanelTab")
        self.activity_tab.setCheckable(True)
        self.files_tab = QPushButton("Files")
        self.files_tab.setObjectName("PanelTab")
        self.files_tab.setCheckable(True)
        self.git_tab = QPushButton("Git")
        self.git_tab.setObjectName("PanelTab")
        self.git_tab.setCheckable(True)
        tabs.addWidget(self.session_tab)
        tabs.addWidget(self.activity_tab)
        tabs.addWidget(self.files_tab)
        tabs.addWidget(self.git_tab)
        tabs.addStretch(1)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tab_group.addButton(self.session_tab, self.PAGE_SESSION)
        self.tab_group.addButton(self.activity_tab, self.PAGE_ACTIVITY)
        self.tab_group.addButton(self.files_tab, self.PAGE_FILES)
        self.tab_group.addButton(self.git_tab, self.PAGE_GIT)
        layout.addLayout(tabs)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_session_page())
        self.stack.addWidget(self._build_activity_page())
        self.stack.addWidget(self._build_files_page())
        self.stack.addWidget(self._build_git_page())
        self.tab_group.idClicked.connect(self._on_tab_clicked)
        layout.addWidget(self.stack, 1)
        self._apply_tool_tabs()

    _STAT_FIELDS = (
        ("session", "SESSION"),
        ("messages", "MESSAGES"),
        ("provider", "PROVIDER"),
        ("model", "MODEL"),
        ("window", "CONTEXT LIMIT"),
        ("total_tokens", "TOTAL TOKENS"),
        ("percent", "USAGE"),
        ("input_tokens", "INPUT TOKENS"),
        ("output_tokens", "OUTPUT TOKENS"),
        ("reasoning_tokens", "REASONING TOKENS"),
        ("cache", "CACHE TOKENS"),
        ("user_messages", "USER MESSAGES"),
        ("assistant_messages", "ASSISTANT MESSAGES"),
        ("tool_calls", "TOOL CALLS"),
        ("created_at", "SESSION CREATED"),
        ("last_activity", "LAST ACTIVITY"),
    )

    _SUMMARY_FIELDS = (
        ("objective", "OBJECTIVE"),
        ("done", "DONE"),
        ("current", "CURRENT"),
        ("next", "NEXT"),
    )

    def _build_session_page(self):
        scroll = QScrollArea()
        scroll.setObjectName("PanelSessionScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)

        page = QWidget()
        page.setObjectName("PanelSessionPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addLayout(self._build_stats_grid(page))
        self._summary_separator = self._separator(page)
        self._summary_separator.setVisible(False)
        layout.addWidget(self._summary_separator)
        layout.addWidget(self._build_summary_block(page))
        layout.addStretch(1)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def _separator(parent) -> QFrame:
        line = QFrame(parent)
        line.setObjectName("PanelSeparator")
        line.setFixedHeight(1)
        return line

    def _build_summary_block(self, parent):
        block = QWidget(parent)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(6)
        caption = QLabel("SUMMARY")
        caption.setObjectName("PanelSection")
        header.addWidget(caption)
        header.addStretch(1)
        self.summary_updated = QLabel("")
        self.summary_updated.setObjectName("PanelStatLabel")
        header.addWidget(self.summary_updated)
        layout.addLayout(header)
        self._summary_sections = {}
        for key, title in self._SUMMARY_FIELDS:
            section = QWidget(block)
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(2)
            section_caption = QLabel(title)
            section_caption.setObjectName("PanelSummaryCaption")
            body = QLabel("")
            body.setObjectName("PanelSummaryText")
            body.setWordWrap(True)
            body.setTextFormat(Qt.TextFormat.RichText)
            body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            section_layout.addWidget(section_caption)
            section_layout.addWidget(body)
            layout.addWidget(section)
            self._summary_sections[key] = (section, body)
        block.setVisible(False)
        self._summary_block = block
        return block

    def _build_stats_grid(self, parent):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self._stat_values = {}
        for position, (key, caption) in enumerate(self._STAT_FIELDS):
            row, column = divmod(position, 2)
            cell = QWidget(parent)
            policy = cell.sizePolicy()
            policy.setHeightForWidth(True)
            cell.setSizePolicy(policy)
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            caption_label = QLabel(caption)
            caption_label.setObjectName("PanelStatLabel")
            caption_label.setWordWrap(True)
            value_label = QLabel("—")
            value_label.setObjectName("PanelStatValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            cell_layout.addWidget(caption_label)
            cell_layout.addWidget(value_label)
            grid.addWidget(cell, row, column)
            self._stat_values[key] = value_label
        self._stat_values["cache"].setToolTip(
            "Cache read tokens / cache write tokens")
        return grid

    def _build_activity_page(self):
        scroll = QScrollArea()
        scroll.setObjectName("PanelActivityScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)

        page = QWidget()
        page.setObjectName("PanelActivityPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        self.activity_empty = QLabel(
            "No tools, skills or files used in this session.")
        self.activity_empty.setObjectName("PanelEmpty")
        self.activity_empty.setWordWrap(True)
        layout.addWidget(self.activity_empty)
        (self._skills_block,
         self.skills_caption,
         self._skills_text) = self._build_activity_text_block(page)
        layout.addWidget(self._skills_block)
        (self._tools_block,
         self.tools_caption,
         self._tools_text) = self._build_activity_text_block(page)
        layout.addWidget(self._tools_block)
        self._attachments_block = self._build_attachments_block(page)
        layout.addWidget(self._attachments_block)
        layout.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def _build_activity_text_block(self, parent):
        block = QWidget(parent)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        caption = QLabel("")
        caption.setObjectName("PanelSection")
        body = QLabel("")
        body.setObjectName("PanelSummaryText")
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(caption)
        layout.addWidget(body)
        block.setVisible(False)
        return block, caption, body

    def _build_attachments_block(self, parent):
        block = QWidget(parent)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.attachments_caption = QLabel("ATTACHMENTS")
        self.attachments_caption.setObjectName("PanelSection")
        layout.addWidget(self.attachments_caption)
        self._attachments_list = QWidget(block)
        self._attachments_layout = QVBoxLayout(self._attachments_list)
        self._attachments_layout.setContentsMargins(0, 0, 0, 0)
        self._attachments_layout.setSpacing(2)
        self._attachments_list.setVisible(False)
        layout.addWidget(self._attachments_list)
        block.setVisible(False)
        return block

    def set_session_data(self, data: dict):
        """Fills the Session and Activity tabs; every value is optional."""
        data = data or {}
        title = str(data.get("title") or "").strip()
        self._set_stat_values(data)
        summary = data.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        if summary != self._last_summary or title != self._last_title:
            self._render_summary(summary, title)
            self._last_summary = dict(summary)
            self._last_title = title
        skills = [record for record in data.get("skills") or []
                  if isinstance(record, dict)]
        tools = [record for record in data.get("tools") or []
                 if isinstance(record, dict)]
        records = [record for record in data.get("attachments") or []
                   if isinstance(record, dict)]
        if (skills != self._last_skills or tools != self._last_tools
                or records != self._last_attachments):
            self._render_activity(skills, tools, records)
            self._last_skills = skills
            self._last_tools = tools
            self._last_attachments = records
        if (self._mode == "terminal"
                and self.stack.currentIndex() == self.PAGE_GIT
                and not self.collapsed and self.isVisible()):
            self._git_live_timer.start()

    def _set_stat_values(self, data: dict):
        values = self._stat_values

        def number(key):
            try:
                return max(0, int(data.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        window = number("window")
        try:
            percent = float(data.get("percent") or 0.0)
        except (TypeError, ValueError):
            percent = 0.0

        def set_value(key, text):
            label = values.get(key)
            if label is None:
                return
            label.setText(text)
            if key != "cache":
                label.setToolTip(text if len(text) > 28 else "")

        set_value("session", str(data.get("title") or "").strip()
                  or "New session")
        set_value("messages", f"{number('messages'):,}")
        set_value("provider", str(data.get("provider") or "").strip() or "—")
        set_value("model", str(data.get("model") or "").strip() or "—")
        set_value("window", f"{window:,}" if window else "—")
        set_value("total_tokens", f"{number('total_tokens'):,}")
        set_value("percent", f"{percent:.0f}%" if window else "—")
        set_value("input_tokens", f"{number('input_tokens'):,}")
        set_value("output_tokens", f"{number('output_tokens'):,}")
        set_value("reasoning_tokens", f"{number('reasoning_tokens'):,}")
        set_value("cache",
                  f"{number('cache_read'):,} / {number('cache_write'):,}")
        set_value("user_messages", f"{number('user_messages'):,}")
        set_value("assistant_messages", f"{number('assistant_messages'):,}")
        set_value("tool_calls", f"{number('tool_calls'):,}")
        set_value("created_at", self._format_ts(data.get("created_at")))
        set_value("last_activity", self._format_ts(data.get("last_activity")))

    def _render_summary(self, summary: dict, title: str):
        title_norm = " ".join(str(title or "").split())
        visible = False
        for key, _ in self._SUMMARY_FIELDS:
            section, body = self._summary_sections[key]
            text = str(summary.get(key) or "").strip()
            if key == "objective" and " ".join(text.split()) == title_norm:
                text = ""
            if text.lower() in ("- none.", "none", "(not set)"):
                text = ""
            section.setVisible(bool(text))
            if text:
                body.setText(self._summary_html(text))
                visible = True
        updated = self._format_short_ts(summary.get("updated_at"))
        self.summary_updated.setText(f"Updated {updated}" if updated else "")
        self.summary_updated.setToolTip(
            self._summary_source_tooltip(summary.get("source")))
        self._summary_block.setVisible(visible)
        self._summary_separator.setVisible(visible)

    @staticmethod
    def _summary_html(text: str) -> str:
        lines = []
        for raw in str(text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(("- ", "* ")):
                line = "• " + line[2:]
            lines.append(html.escape(line.replace("**", "")))
        return "<br>".join(lines)

    @staticmethod
    def _summary_source_tooltip(source) -> str:
        source = str(source or "")
        if source == "journal":
            return "Derived from this session's journal."
        if source == "llm":
            return "Updated by the model from the session journal."
        if source == "handoff":
            return "Copied from the previous session when the mode changed."
        if source == "fallback":
            return ("Deterministic fallback; the model summary "
                    "updates automatically.")
        return "Rolling summary of this session."

    def _render_activity(self, skills: list, tools: list, records: list):
        self._render_activity_block(
            self._skills_block, self.skills_caption, self._skills_text,
            "SKILLS", skills)
        self._render_activity_block(
            self._tools_block, self.tools_caption, self._tools_text,
            "TOOLS", tools)
        self._render_attachments(records)
        self.activity_empty.setVisible(not (skills or tools or records))

    def _render_activity_block(self, block, caption, body, title, records):
        lines = []
        for record in records:
            name = str(record.get("name") or "").strip()
            if not name:
                continue
            try:
                count = max(1, int(record.get("count") or 1))
            except (TypeError, ValueError):
                count = 1
            lines.append(f"- {name}" + (f" ×{count}" if count > 1 else ""))
        caption.setText(f"{title} · {len(lines)}" if lines else title)
        body.setText(self._summary_html("\n".join(lines)))
        block.setVisible(bool(lines))

    def _render_attachments(self, records: list):
        while self._attachments_layout.count():
            item = self._attachments_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        colors = _theme_colors(self.theme_manager)
        for record in records:
            self._attachments_layout.addWidget(
                self._attachment_button(record, colors))
        self.attachments_caption.setText(
            f"ATTACHMENTS · {len(records)}" if records else "ATTACHMENTS")
        self._attachments_block.setVisible(bool(records))
        self._attachments_list.setVisible(bool(records))

    def _attachment_button(self, record: dict, colors: dict) -> QPushButton:
        kind = str(record.get("kind") or "file")
        status = str(record.get("status") or "ok")
        name = str(record.get("name") or "attachment")
        path = str(record.get("path") or "")
        try:
            size = int(record.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        details = []
        if kind != "folder" and size:
            details.append(format_size(size))
        if status and status != "ok":
            details.append(status)
        label = name + ("  ·  " + " · ".join(details) if details else "")
        button = QPushButton()
        button.setObjectName("PanelAttachment")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        icon_name = {"image": "fa5s.image",
                     "folder": "fa5s.folder"}.get(kind, "fa5s.file-alt")
        button.setIcon(qta.icon(icon_name, color=colors["muted"]))
        button.setIconSize(QSize(14, 14))
        button.ensurePolished()
        button.setText(button.fontMetrics().elidedText(
            label, Qt.TextElideMode.ElideMiddle, self.EXPANDED_WIDTH - 64))
        tooltip = path or name
        if status and status != "ok":
            tooltip = f"{tooltip}\nStatus: {status}"
        button.setToolTip(tooltip)
        button.setEnabled(bool(path))
        button.setSizePolicy(QSizePolicy.Policy.Ignored,
                             QSizePolicy.Policy.Fixed)
        button.clicked.connect(
            lambda _checked=False, target=path: self._open_attachment(target))
        return button

    def _open_attachment(self, path: str):
        if path:
            self.attachment_open_requested.emit(path)

    @staticmethod
    def _format_short_ts(value) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[11:16] if "T" in text else text[:16]
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone()
            now = datetime.now(stamp.tzinfo)
        else:
            now = datetime.now()
        if stamp.date() == now.date():
            return stamp.strftime("%H:%M")
        return stamp.strftime("%b %d, %H:%M")

    @staticmethod
    def _format_ts(value) -> str:
        text = str(value or "").strip()
        if not text:
            return "—"
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:16].replace("T", " ")
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone()
        return stamp.strftime("%b %d, %Y, %H:%M")

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

    def _build_git_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(4, 4, 4, 4)
        page_layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.git_branch_icon = QLabel()
        self.git_branch_icon.setObjectName("PanelGitIcon")
        header.addWidget(self.git_branch_icon)
        self.git_branch_label = QLabel("—")
        self.git_branch_label.setObjectName("PanelGitBranch")
        self.git_branch_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addWidget(self.git_branch_label, 1)
        self.git_refresh_button = QPushButton()
        self.git_refresh_button.setObjectName("PanelIconButton")
        self.git_refresh_button.setFixedSize(24, 24)
        self.git_refresh_button.setToolTip("Refresh Git status")
        self.git_refresh_button.clicked.connect(self.refresh_git_status)
        header.addWidget(self.git_refresh_button)
        page_layout.addLayout(header)

        self.git_meta_label = QLabel("")
        self.git_meta_label.setObjectName("PanelStatLabel")
        self.git_meta_label.setWordWrap(True)
        self.git_meta_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        page_layout.addWidget(self.git_meta_label)

        scroll = QScrollArea()
        scroll.setObjectName("PanelGitScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)

        content = QWidget()
        content.setObjectName("PanelGitPage")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        self.git_empty = QLabel("")
        self.git_empty.setObjectName("PanelEmpty")
        self.git_empty.setWordWrap(True)
        self.git_empty.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        content_layout.addWidget(self.git_empty)
        self._git_groups_widget = QWidget(content)
        self._git_groups_layout = QVBoxLayout(self._git_groups_widget)
        self._git_groups_layout.setContentsMargins(0, 0, 0, 0)
        self._git_groups_layout.setSpacing(10)
        content_layout.addWidget(self._git_groups_widget)
        self._git_history_widget = QWidget(content)
        self._git_history_layout = QVBoxLayout(self._git_history_widget)
        self._git_history_layout.setContentsMargins(0, 0, 0, 0)
        self._git_history_layout.setSpacing(2)
        content_layout.addWidget(self._git_history_widget)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        page_layout.addWidget(scroll, 1)
        self._render_git({})
        return page

    # -- git ------------------------------------------------------------

    def refresh_git_status(self):
        """Reads the repository state in the background (read-only)."""
        root = self._root
        if not root or not os.path.isdir(root):
            self._render_git({
                "error": "Open a project to see Git status.",
                "error_code": "no_project",
            })
            return
        if self._git_worker is not None:
            self._git_refresh_pending = True
            return
        worker = _GitStatusWorker(root, self)
        worker.status_ready.connect(self._on_git_status_ready)
        worker.finished.connect(self._on_git_worker_finished)
        self._git_worker = worker
        worker.start()

    def _on_git_status_ready(self, root: str, status: dict):
        if root == self._root:
            self._render_git(status)

    def _on_git_worker_finished(self):
        worker = self.sender()
        if not isinstance(worker, QThread):
            worker = self._git_worker
        if worker is not self._git_worker:
            # A stale worker finished after a newer one was started.
            if worker is not None:
                worker.deleteLater()
            return
        self._git_worker = None
        if worker is not None:
            worker.deleteLater()
        if self._git_refresh_pending:
            self._git_refresh_pending = False
            self.refresh_git_status()

    def _render_git(self, status: dict):
        self._last_git_status = dict(status or {})
        status = self._last_git_status
        colors = _theme_colors(self.theme_manager)
        available = bool(status.get("available"))
        branch = str(status.get("branch") or "").strip()
        commit = str(status.get("commit") or "")
        label = branch or "—"
        tooltip = []
        if available:
            if branch:
                tooltip.append(f"Branch: {branch}")
            if commit:
                tooltip.append(f"Commit: {commit[:12]}")
            if status.get("upstream"):
                tooltip.append(f"Upstream: {status['upstream']}")
        self.git_branch_label.setText(
            self.git_branch_label.fontMetrics().elidedText(
                label, Qt.TextElideMode.ElideMiddle,
                self.EXPANDED_WIDTH - 92))
        self.git_branch_label.setToolTip("\n".join(tooltip))

        self.git_meta_label.setText(self._git_meta_text(status))
        self.git_meta_label.setVisible(self.git_meta_label.text() != "")

        self._clear_git_groups()
        has_entries = False
        if available:
            for key, title in _GIT_GROUPS:
                entries = [entry for entry in status.get(key) or []
                           if isinstance(entry, dict)]
                if not entries:
                    continue
                self._add_git_group(title, entries, colors)
                has_entries = True
        commits = [commit for commit in status.get("commits") or []
                   if isinstance(commit, dict)]
        self._render_git_history(commits if available else [])
        self._git_groups_widget.setVisible(has_entries)
        self.git_empty.setVisible(not has_entries)
        if has_entries:
            self.git_empty.setText("")
        elif available:
            self.git_empty.setText("Working tree clean.")
        else:
            self.git_empty.setText(
                str(status.get("error") or "Git status is unavailable."))
        if not available:
            self.git_branch_label.setText("—")
            self.git_branch_label.setToolTip("")

    def _git_meta_text(self, status: dict) -> str:
        if not status.get("available"):
            return ""
        upstream = str(status.get("upstream") or "").strip()
        ahead = int(status.get("ahead") or 0)
        behind = int(status.get("behind") or 0)
        if upstream:
            parts = [upstream]
            if ahead:
                parts.append(f"ahead {ahead}")
            if behind:
                parts.append(f"behind {behind}")
            if not ahead and not behind:
                parts.append("up to date")
            return " · ".join(parts)
        if status.get("detached"):
            commit = str(status.get("commit") or "")
            return f"at {commit[:7]}" if commit else "No branch checked out"
        return "No upstream branch"

    def _clear_git_groups(self):
        while self._git_groups_layout.count():
            item = self._git_groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Hide first: deleteLater leaves the old rows painted for a
                # frame and the fresh rows would overlap them.
                widget.hide()
                widget.deleteLater()

    def _add_git_group(self, title: str, entries: list, colors: dict):
        block = QWidget(self._git_groups_widget)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        caption = QLabel(f"{title} · {len(entries)}")
        caption.setObjectName("PanelSection")
        layout.addWidget(caption)
        for entry in entries:
            layout.addWidget(self._git_file_row(entry, colors))
        self._git_groups_layout.addWidget(block)

    def _git_file_row(self, entry: dict, colors: dict) -> QWidget:
        row = QWidget(self._git_groups_widget)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        code = str(entry.get("code") or "?")
        badge = QLabel(html.escape(code))
        badge.setObjectName("PanelGitBadge")
        badge.setFixedWidth(13)
        badge.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        badge.setStyleSheet(
            f"color: {self._git_status_color(code)}; font-weight: 700;")
        layout.addWidget(badge)

        display = str(entry.get("display") or entry.get("path") or "")
        button = QPushButton()
        button.setObjectName("PanelGitFile")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.ensurePolished()
        button.setText(button.fontMetrics().elidedText(
            display, Qt.TextElideMode.ElideMiddle, self.EXPANDED_WIDTH - 92))
        absolute = str(entry.get("absolute") or "")
        exists = bool(absolute) and os.path.exists(absolute)
        tooltip = [absolute or display, f"Status: {entry.get('label') or code}"]
        if entry.get("original"):
            tooltip.append(f"Renamed from: {entry['original']}")
        if not exists:
            tooltip.append("The file no longer exists.")
        button.setToolTip("\n".join(tooltip))
        button.setEnabled(exists)
        button.setSizePolicy(QSizePolicy.Policy.Ignored,
                             QSizePolicy.Policy.Fixed)
        if exists:
            button.clicked.connect(
                lambda _checked=False, target=absolute:
                editor_launcher.open_path(target))
        layout.addWidget(button, 1)
        return row

    def _render_git_history(self, commits: list):
        self._clear_git_history()
        if not commits:
            self._git_history_widget.setVisible(False)
            return
        caption = QLabel(f"HISTORY · {len(commits)}")
        caption.setObjectName("PanelSection")
        self._git_history_layout.addWidget(caption)
        for commit in commits:
            self._git_history_layout.addWidget(self._git_commit_row(commit))
        self._git_history_widget.setVisible(True)

    def _clear_git_history(self):
        while self._git_history_layout.count():
            item = self._git_history_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _git_commit_row(self, commit: dict) -> QWidget:
        row = QWidget(self._git_history_widget)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(8)

        short = str(commit.get("short") or "")[:7]
        hash_label = QLabel(html.escape(short))
        hash_label.setObjectName("PanelGitHash")
        hash_label.setFixedWidth(52)
        hash_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hash_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(hash_label)

        subject_text = str(commit.get("subject") or "")
        subject = QLabel()
        subject.setObjectName("PanelGitSubject")
        subject.ensurePolished()
        subject.setText(subject.fontMetrics().elidedText(
            subject_text, Qt.TextElideMode.ElideRight,
            self.EXPANDED_WIDTH - 118))
        subject.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(subject, 1)

        author = str(commit.get("author") or "")
        relative = str(commit.get("relative") or "")
        date = str(commit.get("date") or "")
        full = str(commit.get("hash") or "")
        meta = " · ".join(part for part in (author, relative, date) if part)
        tooltip = [subject_text, meta, full or short]
        refs = str(commit.get("refs") or "").strip()
        if refs:
            tooltip.append(refs)
        text = "\n".join(part for part in tooltip if part)
        row.setToolTip(text)
        hash_label.setToolTip(text)
        subject.setToolTip(text)
        return row

    def _git_status_color(self, code: str) -> str:
        theme = "dark" if getattr(
            self.theme_manager, "current_theme", "dark") == "dark" else "light"
        return _GIT_STATUS_COLORS[theme].get(code, _GIT_STATUS_COLORS[theme]["?"])

    def _on_tab_clicked(self, index: int):
        self.stack.setCurrentIndex(index)
        if index == self.PAGE_GIT:
            self.refresh_git_status()

    # -- state ----------------------------------------------------------

    def set_mode(self, mode: str):
        """Session, Activity and Files stay in every mode; Git is Terminal."""
        self._mode = str(mode or "chat")
        if (self._mode != "terminal"
                and self.stack.currentIndex() == self.PAGE_GIT):
            self._select_page(self.PAGE_SESSION)
        self._apply_tool_tabs()
        if self._mode == "terminal" and not self.collapsed:
            self.refresh_git_status()

    def _apply_tool_tabs(self):
        self.activity_tab.setVisible(not self.collapsed)
        self.files_tab.setVisible(not self.collapsed)
        self.git_tab.setVisible(not self.collapsed and self._mode == "terminal")

    def _select_page(self, index: int):
        button = self.tab_group.button(index)
        if button is not None:
            button.setChecked(True)
        self.stack.setCurrentIndex(index)

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
            self._render_git({
                "error": "Open a project to see Git status.",
                "error_code": "no_project",
            })
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
        if self._mode == "terminal" and not self.collapsed:
            self.refresh_git_status()

    def toggle_collapsed(self):
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, collapsed: bool):
        self.collapsed = bool(collapsed)
        self.setFixedWidth(
            self.COLLAPSED_WIDTH if self.collapsed else self.EXPANDED_WIDTH)
        for widget in (self.title_label, self.project_label,
                       self.session_tab, self.stack):
            widget.setVisible(not self.collapsed)
        if (not self.collapsed and self._mode != "terminal"
                and self.stack.currentIndex() == self.PAGE_GIT):
            self._select_page(self.PAGE_SESSION)
        self._apply_tool_tabs()
        self.collapse_button.setToolTip(
            "Expand panel" if self.collapsed else "Collapse panel")
        self._apply_style()
        if not self.collapsed and self._mode == "terminal":
            self.refresh_git_status()

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
        if self._last_attachments:
            self._render_attachments(self._last_attachments)
        self.git_branch_icon.setPixmap(qta.icon(
            "fa5s.code-branch", color=colors["muted"]).pixmap(13, 13))
        self.git_refresh_button.setIcon(
            qta.icon("fa5s.sync", color=colors["muted"]))
        self.git_refresh_button.setIconSize(QSize(13, 13))
        self._render_git(self._last_git_status or {})
        self.setStyleSheet(f"""
            QWidget#RightPanel {{
                background: {colors['panel']};
                border-left: 1px solid {colors['border']};
            }}
            QScrollArea#PanelSessionScroll {{ background: transparent; border: none; }}
            QWidget#PanelSessionPage {{ background: transparent; }}
            QScrollArea#PanelActivityScroll {{ background: transparent; border: none; }}
            QWidget#PanelActivityPage {{ background: transparent; }}
            QScrollArea#PanelGitScroll {{ background: transparent; border: none; }}
            QWidget#PanelGitPage {{ background: transparent; }}
            QFrame#PanelSeparator {{ background: {colors['border']}; }}
            QLabel#PanelSummaryCaption {{
                color: {colors['muted']};
                font-size: 10px;
                font-weight: 600;
            }}
            QLabel#PanelSummaryText {{
                color: {colors['text']};
                font-size: 12px;
            }}
            QPushButton#PanelAttachment, QPushButton#PanelGitFile {{
                border: none;
                background: transparent;
                color: {colors['text']};
                text-align: left;
                padding: 4px 6px;
                border-radius: 6px;
            }}
            QPushButton#PanelAttachment:hover, QPushButton#PanelGitFile:hover {{
                background: {colors['hover']};
            }}
            QPushButton#PanelAttachment:disabled, QPushButton#PanelGitFile:disabled {{
                background: transparent;
                color: {colors['muted']};
            }}
            QLabel#PanelGitBranch {{
                color: {colors['text']};
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel#PanelGitBadge {{
                font-size: 11px;
            }}
            QLabel#PanelGitHash {{
                color: {colors['muted']};
                font-size: 11px;
                font-family: Consolas, "Courier New", monospace;
            }}
            QLabel#PanelGitSubject {{
                color: {colors['text']};
                font-size: 12px;
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
            QLabel#PanelStatLabel {{
                color: {colors['muted']};
                font-size: 11px;
                font-weight: 600;
            }}
            QLabel#PanelStatValue {{
                color: {colors['text']};
                font-size: 13px;
                font-weight: 600;
            }}
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
