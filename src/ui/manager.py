"""
This module is responsible for managing all visual elements of the assistant.
It is a "dumb" view, controlled by the AppController.
"""
import logging
import os
import sys
from urllib.parse import quote
import qtawesome as qta
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import (QBuffer, QEvent, QIODevice, QEasingCurve, QMimeData, QPoint,
                            QPropertyAnimation, QRect, QSize, Qt, Signal, QTimer, QUrl, Slot)
from PySide6.QtGui import (QColor, QCursor, QDesktopServices, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap, QShortcut, QTextCursor, QTextDocument,
                           QTextOption)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QMenu, QPushButton, QSizeGrip,
                               QTextEdit, QVBoxLayout, QWidget)

from app_paths import resource_path
from attachments import IMAGE_EXTENSIONS, resolve_path
from llm.custom_providers import provider_is_configured
from llm.model_config_manager import ModelConfigManager
from llm.mode_capabilities import ModeCapabilities
from llm.reasoning_effort import effective as effective_effort
from llm.reasoning_effort import label as effort_label
from llm.reasoning_effort import levels as effort_levels
from states import ControllerState
from src.suggestions import normalize_suggestions
from src.computer.terminal_policy import ACCESS_MODE_LABELS, normalize_mode as normalize_access_mode

from . import editor_launcher, image_thumbs, styles
from .provider_icons import provider_icon
from .utils import CustomToolTipManager, IconHoverFilter
from .widgets.attachment_chip import AttachmentChip
from .widgets.capability_bar import CapabilityBar
from .widgets.image_preview_dialog import ImagePreviewDialog
from .text_render import render_reasoning, render_text, render_user_message
from .widgets.chat_widget import ChatWidget
from .widgets.model_switch_popup import ModelSwitchPopup, build_effort_menu
from .widgets.plan_panel import PlanPanel
from .widgets.job_panel import JobPanel
from .widgets.output_preview import OutputPreview
from .widgets.terminal_access_popup import TerminalAccessPopup
from .widgets.custom_title_bar import CustomTitleBar
from .widgets.flow_layout import FlowLayout
from .widgets.non_scrollable_scroll_area import NonScrollableScrollArea
from .navigation import HomePage, ProjectSidebar, _theme_colors
from .right_panel import RightPanel
from .context_popover import ContextPopover

class MainWindow(QMainWindow):
    """Custom MainWindow to ensure the application quits on close."""
    closing = Signal()

    def __init__(self, ui_manager, parent=None):
        super().__init__(parent)
        self.ui_manager = ui_manager
        self.size_grip = QSizeGrip(self)
        self.size_grip.setFixedSize(16, 16)
        self.size_grip.setVisible(False)

        self.setAcceptDrops(True) # Enable drag and drop for the main window

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())
        if paths:
            self.ui_manager.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def resizeEvent(self, event):
        """Override resizeEvent to keep the size grip in the bottom-right corner."""
        super().resizeEvent(event)
        self.ui_manager._update_file_search_popup_geometry()
        self.ui_manager._schedule_toolbar_reflow()
        if self.size_grip.isVisible():
            grip_size = self.size_grip.size()
            self.size_grip.move(self.width() - grip_size.width(), self.height() - grip_size.height())
            self.size_grip.raise_()

    def closeEvent(self, event):
        logging.info("MainWindow closeEvent received. Emitting closing signal for graceful shutdown.")
        try:
            self.ui_manager.save_window_state()
        except Exception as e:
            logging.warning(f"Could not remember window state on close: {e}")
        self.closing.emit()
        event.ignore()  # Let the controller handle the actual exit.

    @Slot(bool)
    def set_visibility_for_screenshot(self, visible: bool):
        """A dedicated slot to control window opacity for thread-safe screenshotting.

        Opacity (not hide/show): the window stays mapped, so there is no
        taskbar flicker, no focus loss, and no native-window recreation —
        DWM simply composites nothing for fully transparent pixels.
        """
        logging.info(f"screenshot opacity -> {'visible' if visible else 'hidden'}")
        self.setWindowOpacity(1.0 if visible else 0.0)

class ExpandingTextEdit(QTextEdit):
    return_pressed = Signal()
    escape_pressed = Signal()
    up_arrow_pressed = Signal()
    down_arrow_pressed = Signal()
    tab_pressed = Signal()
    image_pasted = Signal(QImage)
    files_pasted = Signal(list)

    def __init__(self, parent=None, target_height=36):
        super().__init__(parent)
        self._is_compact_mode = False
        self.file_search_popup = None
        font_metrics = self.fontMetrics()
        self.line_height = font_metrics.height()
        self.max_height = int(self.line_height * 5.5)
        self.target_height = target_height
        self.textChanged.connect(self.update_height)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        QTimer.singleShot(0, self.update_height)

    def set_file_search_popup(self, popup):
        self.file_search_popup = popup

    def insertFromMimeData(self, source: QMimeData):
        """
        Overrides the default paste behavior to handle images and files first.
        """
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage):
                self.image_pasted.emit(image)
                return
        elif source.hasUrls():
            urls = source.urls()
            file_paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
            if file_paths:
                self.files_pasted.emit(file_paths)
                return

        # Insert text as plain text: the default handler honours text/html and
        # would carry inline colours and backgrounds copied from other apps
        # into this themed composer.
        text = source.text()
        if not text and source.hasHtml():
            document = QTextDocument()
            document.setHtml(source.html())
            text = document.toPlainText()
        self.insertPlainText(text)

    def set_compact_mode(self, compact: bool):
        """Switches the text edit's behavior between compact and expanding modes."""
        self._is_compact_mode = compact
        self.setWordWrapMode(QTextOption.NoWrap if compact else QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.update_height()

    def update_height(self):
        if self._is_compact_mode:
            self.setFixedHeight(self.target_height)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        else:
            doc_height = self.document().size().height()
            margins = self.contentsMargins()
            content_height = int(doc_height + margins.top() + margins.bottom())

            new_height = max(self.target_height, content_height)

            if new_height < self.max_height:
                self.setFixedHeight(new_height)
                self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            else:
                self.setFixedHeight(self.max_height)
                self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def keyPressEvent(self, event):
        key = event.key()
        is_popup_visible = self.file_search_popup and self.file_search_popup.isVisible()

        if key == Qt.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return

        if is_popup_visible:
            if key == Qt.Key_Up:
                self.up_arrow_pressed.emit()
                event.accept()
                return
            if key == Qt.Key_Down:
                self.down_arrow_pressed.emit()
                event.accept()
                return

        is_enter = key in (Qt.Key_Return, Qt.Key_Enter)

        if self._is_compact_mode and is_enter:
            event.accept()
            self.return_pressed.emit()
            return

        if is_enter and not (event.modifiers() & Qt.ShiftModifier):
            event.accept()
            self.return_pressed.emit()
        elif key == Qt.Key_Tab:
            self.tab_pressed.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

class OverlayWindow(QWidget):
    def __init__(self, highlight_rect):
        super().__init__()
        self.highlight_rect = highlight_rect
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setGeometry(QApplication.primaryScreen().geometry())
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
        pen = QPen(QColor(255, 0, 0, 200), 3)
        painter.setPen(pen)
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(self.highlight_rect, Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.drawRect(self.highlight_rect)

class HighlightOverlay(QWidget):
    def __init__(self, highlight_rect):
        super().__init__()
        self.highlight_rect = highlight_rect
        self.crosshair_color = QColor(255, 0, 0, 200)
        self.border_color = QColor(255, 255, 0, 220)
        self.overlay_color = QColor(0, 0, 0, 120)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(QApplication.primaryScreen().geometry())

    def paintEvent(self, event):
        painter = QPainter(self)

        # Dimming overlay
        painter.fillRect(self.rect(), self.overlay_color)

        # "Spotlight" effect
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(self.highlight_rect, Qt.transparent)

        # Border and crosshair
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        # Border
        pen = QPen(self.border_color, 3)
        painter.setPen(pen)
        painter.drawRect(self.highlight_rect)

        # Crosshair
        pen.setColor(self.crosshair_color)
        pen.setWidth(2)
        painter.setPen(pen)
        center = self.highlight_rect.center()
        painter.drawLine(center.x() - 10, center.y(), center.x() + 10, center.y())
        painter.drawLine(center.x(), center.y() - 10, center.x(), center.y() + 10)

class UIManager(QWidget):
    user_command_submitted = Signal(str)
    screenshot_button_clicked = Signal()
    select_files_button_clicked = Signal()
    settings_button_clicked = Signal()
    add_file_clicked = Signal()
    add_folder_clicked = Signal()
    chat_mode_selected = Signal()
    terminal_mode_selected = Signal()
    vision_mode_selected = Signal()
    browser_mode_selected = Signal()
    capability_detach_requested = Signal(str)
    model_selected = Signal(str, str)  # provider_id, model_id (remembered per session)
    reasoning_effort_changed = Signal(str, str, str)  # provider_id, model_id, level
    continue_requested = Signal()
    retry_requested = Signal()
    job_stop_requested = Signal(int)
    job_detach_requested = Signal()
    attachment_removed = Signal(int)
    cancel_requested = Signal()
    toggle_fullscreen_button_clicked = Signal()
    file_search_requested = Signal(str, str)
    path_selected_for_attachment = Signal(str)

    # Project switching (emitted by the sidebar / title bar button)
    project_switch_requested = Signal(str)
    project_open_requested = Signal()
    project_renamed = Signal(str, str)
    project_removed = Signal(str, bool)
    session_open_requested = Signal(str)
    session_renamed = Signal(str, str)
    session_deleted = Signal(str)
    session_journal_requested = Signal(str)
    new_session_requested = Signal()
    context_requested = Signal()
    context_compact_requested = Signal()
    home_requested = Signal()

    # Signals for new pilot controls
    pause_resume_clicked = Signal()
    pause_for_feedback_clicked = Signal()
    stop_clicked = Signal()
    warning_confirmed = Signal()
    shutdown_requested = Signal()
    image_pasted = Signal(bytes)
    files_pasted = Signal(list)
    files_dropped = Signal(list)

    def __init__(self, controller):
        import time
        init_start = time.perf_counter()
        super().__init__()
        self.controller = controller
        self.settings_manager = controller.settings_manager
        self.is_compact = False
        self.compact_position = 'bottom'
        self.overlay = None
        self.msg_box = None
        self.attachment_thumbnails = []
        self.current_search_context = None # To store position for file path insertion
        self.main_window = MainWindow(self)
        self.current_file_search_results: list[str] = []
        self.displayed_file_search_count: int = 0
        self.FILE_SEARCH_LOAD_CHUNK_SIZE: int = 30
        self.main_window.closing.connect(self.shutdown_requested.emit)
        self.tooltip_manager = CustomToolTipManager(self.main_window)
        self.main_window.setWindowTitle("Logos AI Assistant")
        self.window_icon = QIcon(resource_path("assets", "ico.ico"))
        self.main_window.setWindowIcon(self.window_icon)
        # Settings page is built on first use: it pulls in every settings
        # section (providers, MCP, skills), which would slow down startup.
        self.settings_page = None

        self.full_mode_geometry = QRect(100, 100, 400, 640)
        self._last_full_frame = None  # (QRect, maximized) captured when leaving full mode
        self._update_compact_mode_geometry()

        self.main_window.setGeometry(self.full_mode_geometry)
        self.central_widget = QWidget()
        self.main_window.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)
        self.layout.setContentsMargins(11, 11, 11, 11)
        self.layout.setSpacing(0)

        self.custom_title_bar = CustomTitleBar(self.controller.theme_manager)
        self.custom_title_bar.setVisible(False)
        self.custom_title_bar.project_clicked.connect(self._show_project_menu)
        self.layout.addWidget(self.custom_title_bar)

        # Start screen: shown when no project is selected.
        self.home_page = HomePage(
            getattr(self.controller, "project_manager", None),
            self.controller.theme_manager,
        )
        self.home_page.setVisible(False)
        self.layout.addWidget(self.home_page, 1)

        # Workspace: collapsible project sidebar + chat/input column.
        self.workspace_container = QWidget()
        self.workspace_layout = QHBoxLayout(self.workspace_container)
        self.workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_layout.setSpacing(0)
        self.sidebar = ProjectSidebar(
            getattr(self.controller, "project_manager", None),
            self.controller.theme_manager,
        )
        self.workspace_layout.addWidget(self.sidebar)
        self.workspace_column = QWidget()
        self.workspace_column_layout = QVBoxLayout(self.workspace_column)
        self.workspace_column_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_column_layout.setSpacing(0)
        # Collapsing a panel or hiding it changes the width of this column
        # without resizing the window, and the column cap follows that width:
        # reflow the composer whenever the column itself changes size.
        self.workspace_column.installEventFilter(self)
        self.workspace_layout.addWidget(self.workspace_column, 1)
        self.right_panel = RightPanel(self.controller.theme_manager)
        self.right_panel.attachment_open_requested.connect(
            self._open_session_attachment)
        self.right_panel.context_clicked.connect(self._show_context_popover)
        self.workspace_layout.addWidget(self.right_panel)
        self._restore_right_panel_state()
        self._update_right_panel_visibility()
        self.layout.addWidget(self.workspace_container, 1)

        self.home_page.project_selected.connect(self.project_switch_requested.emit)
        self.home_page.session_selected.connect(self.session_open_requested.emit)
        self.home_page.open_project_requested.connect(self.project_open_requested.emit)
        self.home_page.new_session_requested.connect(self.new_session_requested.emit)
        self.home_page.settings_requested.connect(self.settings_button_clicked.emit)
        self.home_page.project_renamed.connect(self.project_renamed.emit)
        self.home_page.project_removed.connect(self.project_removed.emit)
        self.home_page.session_renamed.connect(self.session_renamed.emit)
        self.home_page.session_deleted.connect(self.session_deleted.emit)
        self.home_page.session_journal_requested.connect(
            self.session_journal_requested.emit)
        self.sidebar.home_requested.connect(self.home_requested.emit)
        self.sidebar.project_selected.connect(self.project_switch_requested.emit)
        self.sidebar.session_selected.connect(self.session_open_requested.emit)
        self.sidebar.new_session_requested.connect(self.new_session_requested.emit)
        self.sidebar.settings_requested.connect(self.settings_button_clicked.emit)
        self.sidebar.project_renamed.connect(self.project_renamed.emit)
        self.sidebar.project_removed.connect(self.project_removed.emit)
        self.sidebar.session_renamed.connect(self.session_renamed.emit)
        self.sidebar.session_deleted.connect(self.session_deleted.emit)
        self.sidebar.session_journal_requested.connect(
            self.session_journal_requested.emit)
        self._restore_sidebar_state()

        self.chat_widget = ChatWidget(self.controller.theme_manager)
        self.chat_widget.is_compact_fn = lambda: self.is_compact
        self.chat_widget.mode_fn = lambda: getattr(
            self, "_active_mode_key", "chat")
        # Single source of truth for the column cap so the chat text and the
        # input/toolbar block always share the exact same width.
        self.chat_widget.content_max_width_fn = self._content_max_width
        self.chat_widget.retry_requested.connect(self.retry_requested.emit)
        self.chat_widget.continue_requested.connect(self.continue_requested.emit)
        self.chat_widget.job_stop_requested.connect(self.job_stop_requested.emit)
        self.chat_widget.job_detach_requested.connect(
            self.job_detach_requested.emit)
        # Ctrl+B moves a running foreground command to the background, the same
        # action as the "Move to background" link on its step row. Window-scoped
        # so it works while the composer has focus.
        self.job_detach_shortcut = QShortcut(QKeySequence("Ctrl+B"),
                                            self.main_window)
        self.job_detach_shortcut.setContext(Qt.WindowShortcut)
        self.job_detach_shortcut.activated.connect(
            self.job_detach_requested.emit)
        self.chat_widget.attachment_preview_requested.connect(
            self._open_attachment_path)
        self.chat_widget.suggestion_selected.connect(
            self.user_command_submitted.emit)
        self.workspace_column_layout.addWidget(self.chat_widget, 1)

        self.attachment_scroll_area = NonScrollableScrollArea()
        self.attachment_scroll_area.setWidgetResizable(True)
        self.attachment_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.attachment_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.attachment_widget = QWidget()
        self.attachment_layout = FlowLayout(self.attachment_widget, spacing=6)
        self.attachment_scroll_area.setWidget(self.attachment_widget)
        self.attachment_scroll_area.setVisible(False)

        # --- File Search Popup ---
        self.file_search_popup = QListWidget(self.central_widget)
        self.file_search_popup.setVisible(False)
        # Calculate item height for dynamic resizing
        dummy_item = QListWidgetItem("Dummy Item")
        self.file_search_popup.addItem(dummy_item)
        self._file_search_item_height = self.file_search_popup.sizeHintForRow(0)
        self.file_search_popup.clear() # Remove dummy item
        self._file_search_popup_max_h = self._file_search_item_height * 10
        self.file_search_popup.setSpacing(0)
        self.file_search_popup.setSizeAdjustPolicy(QListWidget.AdjustToContents)
        self.file_search_popup.itemClicked.connect(self._on_file_suggestion_selected)
        # -------------------------

        self.input_container = QWidget()
        self.input_layout = QHBoxLayout(self.input_container)
        self.input_layout.setContentsMargins(0, 8, 0, 0)
        self.user_input = ExpandingTextEdit(target_height=36)
        self.user_input.set_file_search_popup(self.file_search_popup)
        self.user_input.setPlaceholderText("Type your message here...")
        self.input_layout.addWidget(self.user_input)

        self.bottom_toolbar_container = QWidget()
        self.bottom_toolbar_layout = QHBoxLayout(self.bottom_toolbar_container)
        self.bottom_toolbar_layout.setSpacing(10)
        self.bottom_toolbar_layout.setContentsMargins(0, 8, 0, 0)

        # Second row (above the icon toolbar, below the input) dedicated to
        # the capability controls + model selection, so long provider/model
        # names are not clipped.
        self.mode_model_container = QWidget()
        self.mode_model_layout = QHBoxLayout(self.mode_model_container)
        self.mode_model_layout.setSpacing(10)
        self.mode_model_layout.setContentsMargins(0, 8, 0, 0)
        # None until first layout pass; True = single-row (wide), False = two rows.
        self._toolbar_single_row = None
        # Coalesces reflow requests so the layout is never rebuilt synchronously
        # inside a resize event (re-entrant layout passes can crash Qt).
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow_toolbar)

        self.toggle_button = QPushButton()
        self.toggle_button.setIconSize(QSize(20, 20))
        self.toggle_button.setFixedSize(QSize(36, 36))
        self.tooltip_manager.install(self.toggle_button, "Minimize")
        self.toggle_button_hover_filter = IconHoverFilter(
            self.controller.theme_manager, 'fa5s.compress', parent=self.toggle_button
        )
        self.toggle_button.installEventFilter(self.toggle_button_hover_filter)
        self.toggle_button.setVisible(False)

        self.attachment_button = QPushButton()
        self.attachment_button.setIconSize(QSize(20, 20))
        self.attachment_button.setFixedSize(QSize(36, 36))
        self.tooltip_manager.install(self.attachment_button, "Add attachment")
        self.attachment_button.installEventFilter(
            IconHoverFilter(self.controller.theme_manager, 'fa5s.paperclip', parent=self.attachment_button)
        )
        self.attachment_button.clicked.connect(self._show_attachment_menu)

        self.attachment_menu = QMenu(self.attachment_button)
        self.add_file_action = self.attachment_menu.addAction("Add File...")
        self.add_folder_action = self.attachment_menu.addAction("Add Folder...")
        self.attachment_menu.addSeparator()
        self.screenshot_action = self.attachment_menu.addAction("Take Screenshot")
        self.add_folder_action.setVisible(False)

        self.capability_bar = CapabilityBar(self.controller.theme_manager, self)

        self.model_button = QPushButton("...")
        self.model_button.setMinimumSize(140, 36)
        self.model_button.setMaximumWidth(420)
        self.model_button.setIconSize(QSize(16, 16))
        self.tooltip_manager.install(self.model_button, "Switch model (saved for this session)")
        self.model_button.clicked.connect(self._show_model_switch_popup)
        self.model_switch_popup = None

        # Current model's reasoning effort, mirrored from the model picker.
        self.model_effort_badge = QLabel("", self)
        self.model_effort_badge.setObjectName("ModelEffortBadge")
        self.model_effort_badge.setFixedHeight(24)
        self.model_effort_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_effort_badge.setCursor(Qt.PointingHandCursor)
        self.model_effort_badge.setToolTip(
            "Reasoning effort for the current model. Click to change.")
        self.model_effort_badge.setVisible(False)
        self.model_effort_badge.installEventFilter(self)

        self.pause_icon = None
        self.play_icon = None

        self.send_button = QPushButton()
        self.send_button.setObjectName("SendButton")
        self.send_button.setIconSize(QSize(20, 20))
        self.send_button.setFixedSize(QSize(36, 36))
        self.tooltip_manager.install(self.send_button, "Send")
        self.send_button.installEventFilter(
            IconHoverFilter(self.controller.theme_manager, 'fa5s.paper-plane', parent=self.send_button)
        )

        self.cancel_button = QPushButton()
        self.cancel_button.setObjectName("CancelButton")
        self.cancel_button.setIconSize(QSize(20, 20))
        self.cancel_button.setFixedSize(QSize(36, 36))
        self.tooltip_manager.install(self.cancel_button, "Cancel")
        self.cancel_button.installEventFilter(
            IconHoverFilter(self.controller.theme_manager, 'fa5s.stop', parent=self.cancel_button)
        )

        self.context_indicator_button = QPushButton()
        self.context_indicator_button.setObjectName("ContextIndicator")
        self.context_indicator_button.setFixedHeight(22)
        self.context_indicator_button.setMinimumWidth(44)
        self.context_indicator_button.setCursor(Qt.PointingHandCursor)
        self.context_indicator_button.setFocusPolicy(Qt.NoFocus)
        self.context_indicator_button.setVisible(False)
        self.context_indicator_button.clicked.connect(
            lambda: self._show_context_popover(self.context_indicator_button))
        self.tooltip_manager.install(
            self.context_indicator_button,
            "Context window usage. Click to open Session context.")
        self._context_indicator_stats = None

        # Terminal access permission chip (shown in Terminal mode only).
        self.access_mode_button = QPushButton()
        self.access_mode_button.setObjectName("AccessModeChip")
        self.access_mode_button.setFixedHeight(24)
        self.access_mode_button.setCursor(Qt.PointingHandCursor)
        self.access_mode_button.setFocusPolicy(Qt.NoFocus)
        self.access_mode_button.setVisible(False)
        self.access_mode_button.clicked.connect(self._show_access_mode_popup)
        self.tooltip_manager.install(
            self.access_mode_button,
            "Terminal access permissions. Click to change.")

        self.bottom_toolbar_layout.addWidget(self.toggle_button)
        self.bottom_toolbar_layout.addWidget(self.attachment_button)
        self.bottom_toolbar_layout.addWidget(self.access_mode_button)

        self.mode_model_layout.addWidget(self.capability_bar)
        self.mode_model_layout.addWidget(self.model_button, 1)
        self.mode_model_layout.addWidget(
            self.model_effort_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.pause_task_button = QPushButton()
        self.pause_task_button.setIconSize(QSize(20, 20))
        self.pause_task_button.setFixedSize(QSize(36, 36))
        self.tooltip_manager.install(self.pause_task_button, "Pause/resume task (Ctrl+Alt+P)")
        self.pause_task_hover_filter = IconHoverFilter(
            self.controller.theme_manager, 'fa5s.pause', parent=self.pause_task_button
        )
        self.pause_task_button.installEventFilter(self.pause_task_hover_filter)
        self.pause_task_button.clicked.connect(self._on_pause_task_clicked)
        self.pause_task_button.setVisible(False)
        self.bottom_toolbar_layout.addStretch()
        # One morphing action button: Stop while thinking with empty input,
        # Send otherwise (sending cancels the in-flight response).
        self._thinking_active = False
        self.bottom_toolbar_layout.addWidget(self.context_indicator_button)
        self.bottom_toolbar_layout.addWidget(self.pause_task_button)
        self.bottom_toolbar_layout.addWidget(self.send_button)
        self.bottom_toolbar_layout.addWidget(self.cancel_button)
        self.cancel_button.setVisible(False)

        # The input + toolbar block lives in a centered, width-capped column so
        # on wide windows it lines up with the centered chat text instead of
        # stretching edge-to-edge (opencode-style). Narrow windows: full width.
        self.bottom_center_column = QWidget()
        self.bottom_center_column.setMaximumWidth(self._content_max_width())
        self.bottom_center_layout = QVBoxLayout(self.bottom_center_column)
        self.bottom_center_layout.setContentsMargins(0, 0, 0, 0)
        self.bottom_center_layout.setSpacing(0)
        # Live background jobs are machine state, not transcript content: a
        # pinned strip keeps them reachable (live time, Stop, Open log) even
        # when the turn scrolls away from the block that launched one.
        self.job_panel = JobPanel(self.controller.theme_manager,
                                 self.bottom_center_column)
        self.job_panel.job_stop_requested.connect(self.job_stop_requested.emit)
        self.job_panel.job_hovered.connect(self._on_job_output_hovered)
        self.job_panel.job_unhovered.connect(self._on_output_preview_unhovered)
        self.bottom_center_layout.addWidget(self.job_panel)
        # A running command's output lives in a card that follows the pointer
        # instead of a permanent strip: the transcript can hold only one grey
        # progress line (one QTextDocument, no scroll area), but a strip above
        # the composer took that slice of the chat all the time.
        self.output_preview = OutputPreview(self.controller.theme_manager, self)
        self.output_preview.stop_requested.connect(self._on_stop_clicked)
        self.output_preview.job_stop_requested.connect(
            self.job_stop_requested.emit)
        self.output_preview.open_log_requested.connect(self._open_output_log)
        self.output_preview.closed.connect(self._on_output_preview_closed)
        self.chat_widget.live_step_hovered.connect(self._on_live_step_hovered)
        self.chat_widget.live_step_unhovered.connect(
            self._on_output_preview_unhovered)
        self._preview_job_id = None
        self._preview_offset = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(600)
        self._preview_timer.timeout.connect(self._pump_output_preview)
        try:
            QApplication.instance().applicationStateChanged.connect(
                self._on_application_state_changed)
        except Exception:
            pass
        self.main_window.installEventFilter(self)
        # The live plan belongs to the composer, not the transcript: it sits
        # directly above the input while a turn runs and vanishes with it.
        self.plan_panel = PlanPanel(self.controller.theme_manager,
                                    self.bottom_center_column)
        self.bottom_center_layout.addWidget(self.plan_panel)
        # Attachments belong to the composer: keep them inside the same
        # centered, width-capped column as the input and chat text instead of
        # stretching across the whole workspace.
        self.bottom_center_layout.addWidget(self.attachment_scroll_area)
        self.bottom_center_layout.addWidget(self.input_container)
        self.bottom_center_layout.addWidget(self.bottom_toolbar_container)
        self.bottom_center_layout.addWidget(self.mode_model_container)

        self.bottom_center_row = QWidget()
        self.bottom_center_row_layout = QHBoxLayout(self.bottom_center_row)
        self.bottom_center_row_layout.setContentsMargins(0, 0, 0, 0)
        self.bottom_center_row_layout.setSpacing(0)
        # Cap the bottom block (input + toolbars) to the readable column width
        # and let the surrounding stretches center it, aligned with the chat
        # text. Only a *maximum* is set, so the window can still shrink freely.
        self.bottom_center_row_layout.addStretch(1)
        self.bottom_center_row_layout.addWidget(self.bottom_center_column, 1000)
        self.bottom_center_row_layout.addStretch(1)
        self.workspace_column_layout.addWidget(self.bottom_center_row)

        self.send_button.clicked.connect(self._on_send_clicked)
        self.user_input.return_pressed.connect(self._on_send_clicked)
        self.user_input.escape_pressed.connect(self._hide_file_search_popup)
        self.user_input.up_arrow_pressed.connect(self._select_previous_suggestion)
        self.user_input.down_arrow_pressed.connect(self._select_next_suggestion)
        self.user_input.tab_pressed.connect(self._on_tab_pressed)
        self.user_input.image_pasted.connect(self._handle_pasted_image)
        self.user_input.files_pasted.connect(self._handle_pasted_files)
        self.toggle_button.clicked.connect(self.toggle_mode)
        self.add_file_action.triggered.connect(self.add_file_clicked.emit)
        self.add_folder_action.triggered.connect(self.add_folder_clicked.emit)
        self.screenshot_action.triggered.connect(self.screenshot_button_clicked.emit)
        self.capability_bar.presentation_requested.connect(self._on_capability_opened)
        self.capability_bar.detach_requested.connect(self.capability_detach_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)

        self.custom_title_bar.pause_resume_clicked.connect(self.pause_resume_clicked.emit)
        self.custom_title_bar.pause_for_feedback_clicked.connect(self.pause_for_feedback_clicked.emit)
        self.custom_title_bar.stop_clicked.connect(self._on_stop_clicked)
        self.custom_title_bar.close_clicked.connect(self.main_window.close)

        self.user_input.textChanged.connect(self._update_send_button_state)
        self.user_input.textChanged.connect(self._handle_text_changed_for_file_search)
        self._update_send_button_state()

        self.controller.vision_module_ready.connect(self.on_vision_module_ready)
        self.controller.files_found_signal.connect(self._on_files_found)
        self.settings_manager.settings_changed.connect(self.on_settings_changed)
        self.controller.theme_manager.theme_changed.connect(self._update_icon_colors)
        self.controller.theme_manager.theme_changed.connect(
            self._refresh_model_effort_badge)
        self.controller.theme_manager.theme_changed.connect(
            self._refresh_model_button_icon)
        self.controller.theme_manager.theme_changed.connect(
            self._refresh_access_mode_chip)
        self._update_icon_colors()
        self._refresh_access_mode_chip()
        self._reflow_toolbar()
        init_duration = time.perf_counter() - init_start
        logging.info(f"UIManager.__init__ completed in {init_duration:.3f}s")

    def _handle_pasted_image(self, image: QImage):
        """Converts a pasted QImage to bytes and emits a signal."""
        if image.isNull():
            return

        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        # Save in WEBP format for efficiency
        image.save(buffer, "WEBP", quality=85)
        buffer.close()

        self.image_pasted.emit(buffer.data())

    def _handle_pasted_files(self, file_paths: list):
        """Emits a signal with the list of pasted file paths."""
        if file_paths:
            self.files_pasted.emit(file_paths)

    def _get_current_search_query(self):
        """Checks if the cursor is inside a word starting with @ and returns the query and base_path."""
        cursor = self.user_input.textCursor()
        text = self.user_input.toPlainText()
        pos = cursor.position()

        start = text.rfind('@', 0, pos)
        if start == -1:
            return None, None, -1, -1

        # Ensure there's no non-space character before @
        if start > 0 and not text[start - 1].isspace():
            return None, None, -1, -1

        end = text.find(' ', start)
        if end == -1:
            end = len(text)

        if start < pos <= end:
            full_query = text[start + 1:pos]
            base_path = ""
            if '/' in full_query:
                base_path = full_query[:full_query.rfind('/') + 1]
            return full_query, base_path, start, pos
        return None, None, -1, -1

    def _handle_text_changed_for_file_search(self):
        query, base_path, start, pos = self._get_current_search_query()
        if query is not None:
            # Store context for insertion
            self.current_search_context = {'start': start, 'pos': pos}
            self.file_search_requested.emit(query, base_path)
        else:
            self.current_search_context = None
            self.file_search_popup.setVisible(False)

    @Slot(list)
    def _on_files_found(self, files: list):
        if self.current_search_context is None:
            self.file_search_popup.setVisible(False)
            return

        self.current_file_search_results = list(files)
        self.displayed_file_search_count = 0
        self.file_search_popup.clear()

        if not self.current_file_search_results:
            self.file_search_popup.setVisible(False)
            return

        self._load_more_file_suggestions()
        self._show_file_search_popup()

        # Connect scroll event for dynamic loading
        self.file_search_popup.verticalScrollBar().valueChanged.connect(self._on_file_search_scroll)

    def _load_more_file_suggestions(self):
        start_index = self.displayed_file_search_count
        end_index = min(start_index + self.FILE_SEARCH_LOAD_CHUNK_SIZE, len(self.current_file_search_results))

        if start_index >= end_index:
            return # No more items to load

        for i in range(start_index, end_index):
            self.file_search_popup.addItem(self.current_file_search_results[i])

        self.displayed_file_search_count = end_index

    def _on_file_search_scroll(self, value):
        scrollbar = self.file_search_popup.verticalScrollBar()
        if scrollbar.maximum() - value <= 5: # User scrolled near the bottom
            self._load_more_file_suggestions()

    def _update_file_search_popup_geometry(self):
        """Updates the position and width of the file search popup."""
        if not self.file_search_popup.isVisible():
            return

        # Reset height constraint to allow the widget to grow freely
        self.file_search_popup.setFixedHeight(9999)
        self.file_search_popup.adjustSize()

        ideal_height = self.file_search_popup.sizeHint().height()
        popup_height = min(ideal_height, self._file_search_popup_max_h)

        # Position the popup ABOVE the input field
        input_top_left = self.user_input.mapTo(self.central_widget, self.user_input.rect().topLeft())

        x = input_top_left.x()
        y = input_top_left.y() - popup_height - 2

        self.file_search_popup.move(x, y)
        self.file_search_popup.setFixedWidth(self.user_input.width())
        self.file_search_popup.setFixedHeight(popup_height)

    def _update_attachment_area_height(self):
        if not self.attachment_thumbnails:
            if self.attachment_scroll_area.isVisible():
                self.attachment_scroll_area.setVisible(False)
            return

        if not self.attachment_scroll_area.isVisible():
            self.attachment_scroll_area.setVisible(True)

        ideal_height = self.attachment_layout.heightForWidth(self.attachment_widget.width())

        single_row_height = max(
            chip.sizeHint().height() for chip in self.attachment_thumbnails)
        margins = self.attachment_layout.contentsMargins()
        spacing = self.attachment_layout.spacing()
        max_height = (single_row_height * 3) + (spacing * 2) + margins.top() + margins.bottom()

        logging.debug(f"Attachment area update: ideal_height={ideal_height}, max_height={max_height}")

        if ideal_height > max_height:
            # --- SCROLLING MODE ---
            self.attachment_scroll_area.setFixedHeight(max_height)
            self.attachment_widget.setFixedHeight(ideal_height)
            if self.attachment_scroll_area.verticalScrollBarPolicy() != Qt.ScrollBarAsNeeded:
                self.attachment_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        else:
            # --- RESIZING MODE (NO SCROLL) ---
            if self.attachment_scroll_area.verticalScrollBarPolicy() != Qt.ScrollBarAlwaysOff:
                self.attachment_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

            # Lock both viewport and content to the same height to prevent scrolling
            self.attachment_scroll_area.setFixedHeight(ideal_height)
            self.attachment_widget.setFixedHeight(ideal_height)

    def resizeEvent(self, event):
        """Handle the resize event for the UIManager widget."""
        super().resizeEvent(event)
        self._update_file_search_popup_geometry()
        # Schedule the height update. This allows the layout to settle first.
        QTimer.singleShot(0, self._update_attachment_area_height)

    def _show_file_search_popup(self):
        if self.file_search_popup.count() == 0:
            self.file_search_popup.setVisible(False)
            return

        self.file_search_popup.setVisible(True)
        self._update_file_search_popup_geometry()
        self.file_search_popup.setCurrentRow(0)
        self.file_search_popup.raise_()

    def _hide_file_search_popup(self):
        self.file_search_popup.setVisible(False)
        self.current_search_context = None

    def _navigate_to_dir(self, dir_path: str):
        if self.current_search_context is None:
            return

        start = self.current_search_context['start']

        text = self.user_input.toPlainText()
        # Find the end of the current @-mention
        end_pos = text.find(' ', start)
        if end_pos == -1:
            end_pos = len(text)

        cursor = self.user_input.textCursor()
        cursor.setPosition(start)
        # Select the text from @ to the end of the word to be replaced
        cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, end_pos - start)

        replacement_text = f"@{dir_path}"
        cursor.insertText(replacement_text)

        # The cursor is now automatically at the end of the inserted text.
        self.user_input.setTextCursor(cursor)

    def _handle_suggestion_selection(self, item: QListWidgetItem):
        if not item:
            return

        selected_path = item.text()

        # Get the text of the current @-mention
        text = self.user_input.toPlainText()
        if self.current_search_context is None:
            return
        start = self.current_search_context['start']

        end = text.find(' ', start)
        if end == -1:
            end = len(text)

        # This is the full text of the mention currently in the input field
        current_mention_text = text[start:end]
        # This is the text that *would* be in the input field if we selected the item
        expected_mention_text = f"@{selected_path}"

        # If the user selects a directory that is already fully typed,
        # treat it as a confirmation and insert the path.
        if current_mention_text == expected_mention_text and selected_path.endswith('/'):
            self._insert_file_path(selected_path)
            return

        # Otherwise, navigate or insert as usual
        if selected_path.endswith('/'):
            self._navigate_to_dir(selected_path)
        else:
            self._insert_file_path(selected_path)

    def _on_file_suggestion_selected(self, item: QListWidgetItem):
        self._handle_suggestion_selection(item)

    def _on_tab_pressed(self):
        if self.file_search_popup.isVisible():
            self._handle_suggestion_selection(self.file_search_popup.currentItem())

    def _select_previous_suggestion(self):
        if not self.file_search_popup.isVisible() or self.file_search_popup.count() == 0:
            return
        current = self.file_search_popup.currentRow()
        current = (current - 1 + self.file_search_popup.count()) % self.file_search_popup.count()
        self.file_search_popup.setCurrentRow(current)

    def _select_next_suggestion(self):
        if not self.file_search_popup.isVisible() or self.file_search_popup.count() == 0:
            return
        current = self.file_search_popup.currentRow()
        current = (current + 1) % self.file_search_popup.count()
        self.file_search_popup.setCurrentRow(current)

    def _insert_file_path(self, file_path: str):
        if self.current_search_context is None:
            return

        start = self.current_search_context['start']
        text = self.user_input.toPlainText()

        # Find the end of the word/mention from the start position
        end_pos = text.find(' ', start)
        if end_pos == -1:
            end_pos = len(text)

        # Use QTextCursor to replace the text robustly
        cursor = self.user_input.textCursor()
        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, end_pos - start)

        replacement_text = f"@{file_path} "
        cursor.insertText(replacement_text)

        self.user_input.setTextCursor(cursor)
        self.file_search_popup.setVisible(False)
        self.current_search_context = None

        # Emit a signal so the controller can attach the file/folder
        self.path_selected_for_attachment.emit(file_path)

    def _update_icon_colors(self):
        icon_color = self.controller.theme_manager.get_style("ICON_COLOR")
        self.toggle_icon_minimize = qta.icon('fa5s.compress', color=icon_color)
        self.toggle_icon_maximize = qta.icon('fa5s.expand', color=icon_color)
        self.icon_robot = qta.icon('fa5s.robot', color=icon_color)
        self.pause_icon = qta.icon('fa5s.pause', color=icon_color)
        self.play_icon = qta.icon('fa5s.play', color=icon_color)
        current_toggle_icon = self.toggle_icon_maximize if self.is_compact else self.toggle_icon_minimize
        self.toggle_button.setIcon(current_toggle_icon)
        self._style_context_indicator()

    def set_context_indicator(self, stats: dict | None):
        """Shows the model-facing context fill next to Send; None hides it."""
        if not stats:
            self._context_indicator_stats = None
            popover = getattr(self, "_context_popover_widget", None)
            if popover is not None:
                popover.hide()
            self.context_indicator_button.setVisible(False)
            self._schedule_toolbar_reflow()
            return
        self._context_indicator_stats = dict(stats)
        used = int(stats.get("context_tokens") or 0)
        window = int(stats.get("window") or 0)
        percent = float(stats.get("percent") or 0.0)
        source = str(stats.get("context_source") or "estimated")
        source_label = {
            "actual": "provider-reported",
            "payload": "model payload estimate",
            "estimated": "journal estimate",
        }.get(source, source)
        approximate = "" if source == "actual" else "~"
        cache_bits = []
        latest_cache_read = int(stats.get("latest_cache_read") or 0)
        latest_cache_write = int(stats.get("latest_cache_write") or 0)
        if latest_cache_read:
            share = stats.get("latest_cache_share")
            share_text = f" ({float(share) * 100:.0f}% of prompt)" if share else ""
            cache_bits.append(f"read {latest_cache_read:,}{share_text}")
        if latest_cache_write:
            cache_bits.append(f"written {latest_cache_write:,}")
        cache_line = (
            "\nLast request cache: " + " · ".join(cache_bits)
            if cache_bits else "")
        self.context_indicator_button.setText(f"{percent:.0f}%")
        self.tooltip_manager.install(
            self.context_indicator_button,
            f"Context {approximate}{percent:.0f}% full\n"
            f"{approximate}{used:,} / {window:,} tokens · {source_label}"
            f"{cache_line}\n"
            "Click for the context breakdown")
        self._style_context_indicator(percent)
        was_hidden = self.context_indicator_button.isHidden()
        self.context_indicator_button.setVisible(True)
        if was_hidden:
            self._schedule_toolbar_reflow()
        popover = getattr(self, "_context_popover_widget", None)
        if popover is not None and popover.isVisible():
            self._style_context_popover(popover)
            popover.set_stats(stats)

    def _style_context_indicator(self, percent: float | None = None):
        if percent is None:
            stats = self._context_indicator_stats or {}
            percent = float(stats.get("percent") or 0.0)
        theme = self.controller.theme_manager
        if percent >= 95.0:
            color = theme.get_style("ERROR_COLOR")
        elif percent >= 80.0:
            color = theme.get_style("WARNING_COLOR")
        else:
            color = theme.get_style("COLLAPSIBLE_META")
        hover = theme.get_style("ICON_COLOR")
        self.context_indicator_button.setStyleSheet(f"""
            QPushButton#ContextIndicator {{
                background: transparent;
                border: 1px solid {color};
                border-radius: 9px;
                color: {color};
                font-size: 11px;
                padding: 0px 7px;
            }}
            QPushButton#ContextIndicator:hover {{
                border-color: {hover};
                color: {hover};
            }}
        """)

    def _context_popover(self) -> ContextPopover:
        popover = getattr(self, "_context_popover_widget", None)
        if popover is None:
            popover = ContextPopover(self.main_window)
            popover.compress_requested.connect(
                self.context_compact_requested.emit)
            popover.details_requested.connect(self.context_requested.emit)
            self._context_popover_widget = popover
        return popover

    def _style_context_popover(self, popover: ContextPopover):
        theme = self.controller.theme_manager
        popover.set_theme(
            _theme_colors(theme),
            getattr(theme, "current_theme", "dark") == "dark",
            theme.get_style("SUCCESS_COLOR"),
            theme.get_style("WARNING_COLOR"),
            theme.get_style("ERROR_COLOR"),
        )

    def _show_context_popover(self, anchor=None):
        """Detail card above the context fill pill or the panel gauge."""
        stats = getattr(self, "_context_indicator_stats", None)
        if not stats:
            return
        popover = self._context_popover()
        self._style_context_popover(popover)
        popover.set_stats(stats)
        popover.adjustSize()
        target = anchor if isinstance(anchor, QWidget) else None
        if target is None or not target.isVisible():
            target = self.context_indicator_button
        if target is None or not target.isVisible():
            return
        top_left = target.mapToGlobal(QPoint(0, 0))
        x = top_left.x() + target.width() - popover.width()
        y = top_left.y() - popover.height() - 8
        screen = QApplication.screenAt(top_left) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 6,
                    min(x, area.right() - popover.width() - 6))
            if y < area.top() + 6:
                y = top_left.y() + target.height() + 8
        popover.move(x, y)
        popover.show()
        popover.raise_()

    # -- terminal access mode ---------------------------------------------

    def _access_mode(self) -> str:
        terminal = self.settings_manager.get("terminal", {}) or {}
        return normalize_access_mode(terminal.get("access_mode"))

    def _refresh_access_mode_chip(self):
        """Mirrors terminal.access_mode on the chip and repaints for the theme."""
        mode = self._access_mode()
        theme = self.controller.theme_manager
        error = theme.get_style("ERROR_COLOR")
        muted = theme.get_style("COLLAPSIBLE_META")
        hover = theme.get_style("ICON_COLOR")
        color = error if mode == "full" else muted
        icons = {
            "ask": "fa5s.shield-alt",
            "auto": "fa5s.play-circle",
            "full": "fa5s.exclamation-triangle",
        }
        self.access_mode_button.setIcon(
            qta.icon(icons.get(mode, "fa5s.shield-alt"), color=color))
        self.access_mode_button.setIconSize(QSize(13, 13))
        self.access_mode_button.setText(f"{ACCESS_MODE_LABELS[mode]}  ▾")
        hover_color = color if mode == "full" else hover
        self.access_mode_button.setStyleSheet(f"""
            QPushButton#AccessModeChip {{
                background: transparent;
                border: 1px solid {color};
                border-radius: 9px;
                color: {color};
                font-size: 11px;
                padding: 0 8px;
            }}
            QPushButton#AccessModeChip:hover {{
                border-color: {hover_color};
                color: {hover_color};
            }}
        """)

    def _access_popup(self) -> TerminalAccessPopup:
        popup = getattr(self, "_access_popup_widget", None)
        if popup is None:
            popup = TerminalAccessPopup(self.main_window)
            popup.mode_selected.connect(self._set_access_mode)
            self._access_popup_widget = popup
        return popup

    def _show_access_mode_popup(self):
        popup = self._access_popup()
        theme = self.controller.theme_manager
        popup.set_theme(
            _theme_colors(theme),
            getattr(theme, "current_theme", "dark") == "dark",
            theme.get_style("ERROR_COLOR"),
        )
        popup.set_current(self._access_mode())
        popup.open_near(self.access_mode_button)

    def _set_access_mode(self, mode: str):
        mode = normalize_access_mode(mode)
        terminal = dict(self.settings_manager.get("terminal", {}) or {})
        if terminal.get("access_mode") == mode:
            return
        terminal["access_mode"] = mode
        self.settings_manager.set_many({"terminal": terminal})

    def on_settings_changed(self, new_settings):
        if 'compact_mode' in new_settings:
            self._update_compact_mode_geometry()
            if self.is_compact:
                self.animate_geometry(self.compact_mode_geometry)
        if 'terminal' in new_settings:
            self._refresh_access_mode_chip()

    def _update_compact_mode_geometry(self):
        settings = self.settings_manager.get("compact_mode", {"width": 700, "height": 120, "position": "bottom_center"})
        width = settings.get("width", 700)
        height = settings.get("height", 120)
        position = settings.get("position", "bottom_center")
        screen_rect = QApplication.primaryScreen().geometry()
        margin = 40
        y = (screen_rect.height() - height) // 2
        if "top" in position:
            y = margin
        elif "bottom" in position:
            y = screen_rect.height() - height - margin
        x = (screen_rect.width() - width) // 2
        if "left" in position:
            x = margin
        elif "right" in position:
            x = screen_rect.width() - width - margin
        self.compact_mode_geometry = QRect(x, y, width, height)

    def on_vision_module_ready(self):
        self.set_vision_mode_loading(False)

    def _on_stop_clicked(self):
        self.stop_clicked.emit()
        if self.is_compact:
            self.switchToFullMode()

    def _on_pause_task_clicked(self):
        self.pause_resume_clicked.emit()

    def _on_capability_opened(self, name: str):
        """A capability control was clicked: open that panel."""
        emit = {
            "chat": self.chat_mode_selected.emit,
            "terminal": self.terminal_mode_selected.emit,
            "browser": self.browser_mode_selected.emit,
            "vision": self.vision_mode_selected.emit,
        }.get(str(name or "").lower())
        if emit is not None:
            emit()

    def set_capabilities(self, grants) -> None:
        """Mirrors the session's grants on the capability controls."""
        self.capability_bar.set_attached(grants)

    def refresh_model_button(self, title: str):
        """Updates the toolbar model button (called on switch/settings change).

        The button leads with the provider's own brand glyph instead of the old
        generic bolt: it says who answers. A custom endpoint has no bundled
        glyph, so it simply gets no icon rather than a meaningless flash.
        """
        self.model_button.setText(title)
        self._refresh_model_button_icon()
        self._refresh_model_effort_badge()

    def _refresh_model_button_icon(self):
        """Re-tints the model button's provider glyph for the current theme."""
        button = getattr(self, "model_button", None)
        if button is None:
            return
        provider_id = self.settings_manager.get("active_provider") or ""
        color = self.controller.theme_manager.get_style("ICON_COLOR") or "#d4d4d4"
        icon = provider_icon(provider_id, color, 16)
        button.setIcon(icon if icon is not None else QIcon())

    def _active_effort_fields(self):
        """(provider_id, model_id, levels, current) for the active model."""
        provider_id = self.settings_manager.get("active_provider") or ""
        providers = self.settings_manager.get("llm_providers", {}) or {}
        provider = providers.get(provider_id) or {}
        model_id = provider.get("model") or ""
        available = effort_levels(provider_id, model_id) if provider_id and model_id else []
        if not available:
            return provider_id, model_id, [], ""
        saved = provider.get("reasoning_efforts")
        selected = str(saved.get(model_id) or "") if isinstance(saved, dict) else ""
        return (provider_id, model_id, available,
                effective_effort(provider_id, model_id, selected))

    def _refresh_model_effort_badge(self):
        """Mirrors the active model's reasoning effort next to the model button."""
        _pid, _mid, _levels, effort = self._active_effort_fields()
        if not effort:
            self.model_effort_badge.clear()
            self.model_effort_badge.setVisible(False)
            self._schedule_toolbar_reflow()
            return
        color = self.controller.theme_manager.get_style("COLLAPSIBLE_META")
        self.model_effort_badge.setStyleSheet(
            "QLabel#ModelEffortBadge {"
            f" border: 1px solid {color}; border-radius: 4px;"
            f" color: {color}; font-size: 11px; padding: 0px 8px; }}"
            "QLabel#ModelEffortBadge:hover { background-color: rgba(255, 255, 255, 0.08); }")
        self.model_effort_badge.setText(effort_label(effort))
        self.model_effort_badge.setVisible(True)
        self._schedule_toolbar_reflow()

    def _show_effort_badge_menu(self):
        provider_id, model_id, levels, current = self._active_effort_fields()
        if not levels:
            return
        menu = build_effort_menu(
            self, levels, current,
            lambda level: self.reasoning_effort_changed.emit(provider_id, model_id, level))
        menu.exec(QCursor.pos())

    def eventFilter(self, obj, event):
        if (obj is getattr(self, "main_window", None)
                and event.type() == QEvent.Type.WindowStateChange
                and self.main_window.isMinimized()):
            self.hide_output_preview()
            return super().eventFilter(obj, event)
        if (obj is getattr(self, "workspace_column", None)
                and event.type() == QEvent.Type.Resize):
            # The text column and the composer share one cap derived from the
            # workspace width, so a panel collapsing or appearing (no window
            # resize involved) must refresh that cap too.
            self._schedule_toolbar_reflow()
            return super().eventFilter(obj, event)
        badge = getattr(self, "model_effort_badge", None)
        if (badge is not None and obj is badge
                and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton):
            self._show_effort_badge_menu()
            return True
        return super().eventFilter(obj, event)

    def _show_model_switch_popup(self):
        if self.model_switch_popup is None:
            self.model_switch_popup = ModelSwitchPopup(self.main_window)
            self.model_switch_popup.model_selected.connect(self.model_selected.emit)
            self.model_switch_popup.reasoning_effort_changed.connect(
                self.reasoning_effort_changed.emit)
        self.model_switch_popup.set_sections(self._build_model_sections())
        self.model_switch_popup.open_near(self.model_button)

    @staticmethod
    def _effort_fields(provider_id: str, model_id: str, saved: dict) -> dict:
        """Picker fields for the per-model effort control (no levels = unsupported)."""
        available = effort_levels(provider_id, model_id)
        current = ""
        if available:
            selected = str(saved.get(model_id) or "") if isinstance(saved, dict) else ""
            current = effective_effort(provider_id, model_id, selected)
        return {"effort_levels": available, "effort_current": current}

    def _build_model_sections(self) -> list:
        """Builds popup sections: provider headers + models, keyed providers only.

        Models are filtered by the capabilities of the active mode, so e.g.
        Vision only offers native computer-use models.
        """
        sections = []
        active_mode = getattr(self, "_active_mode_key", "chat") or "chat"
        static_config = ModelConfigManager.PROVIDER_CONFIG or {}
        user_providers = self.settings_manager.get("llm_providers", {}) or {}
        current_pid = self.settings_manager.get("active_provider")
        current_model = (user_providers.get(current_pid) or {}).get("model")
        for pid, info in static_config.items():
            if not provider_is_configured(self.settings_manager.all(), pid):
                continue
            display = info.get("display_name") or pid.replace("_", " ").replace("-", " ").title()
            model_labels = info.get("model_labels") or {}
            saved_efforts = (user_providers.get(pid) or {}).get("reasoning_efforts") or {}
            if pid == "openrouter":
                groups = info.get("models_by_provider") or {}
                group_entries = []
                for group, models in groups.items():
                    items = []
                    for model_name in sorted(models or []):
                        stored_id = f"{group.lower()}/{model_name.lower().replace(' ', '-')}"
                        if not ModeCapabilities.supports(pid, stored_id, active_mode):
                            continue
                        if pid == current_pid and stored_id == current_model:
                            is_current = True
                        else:
                            is_current = False
                        # Free endpoints must be unmistakable: the same base
                        # name without ":free" is the PAID variant (real charges!).
                        label = (f"{model_name} [free]" if model_name.endswith(":free")
                                 else model_name)
                        items.append({"kind": "model", "label": label,
                                      "provider_id": pid, "model_id": stored_id,
                                      "provider_display": display,
                                      "is_current": is_current,
                                      **self._effort_fields(pid, stored_id, saved_efforts)})
                    if items:
                        group_entries.append((group, items))
                if not group_entries:
                    continue
                sections.append({"kind": "header", "label": display})
                for group, items in group_entries:
                    # Paid route must be unmistakable: a bare "Google" next to
                    # the direct Google provider already caused real charges.
                    sections.append({"kind": "subheader", "label": f"OpenRouter / {group}"})
                    sections.extend(items)
            else:
                models = sorted(m for m in (info.get("models") or [])
                                if ModeCapabilities.supports(pid, m, active_mode))
                if not models:
                    continue
                sections.append({"kind": "header", "label": display})
                for model_name in models:
                    sections.append({"kind": "model", "label": model_labels.get(model_name, model_name),
                                      "provider_id": pid, "model_id": model_name,
                                      "provider_display": display,
                                      "is_current": (pid == current_pid and model_name == current_model),
                                      **self._effort_fields(pid, model_name, saved_efforts)})
        return sections

    def set_active_mode(self, mode: str):
        # A mode switch cancels the running turn, so its live plan is gone.
        self.plan_panel.clear_plan()
        is_terminal = (mode == 'terminal')
        self.add_folder_action.setVisible(is_terminal)
        self.access_mode_button.setVisible(is_terminal)
        if is_terminal:
            self._refresh_access_mode_chip()
        # Pause button only while an agent task actually runs: nothing
        # to pause in plain chat, and it hides again when the task ends.
        # (Vision has its own pause controls.)
        self._active_mode_key = mode
        panel = getattr(self, "right_panel", None)
        if panel is not None:
            panel.set_mode(mode)
        self._update_right_panel_visibility()

        if mode == 'chat':
            self.capability_bar.set_active('chat')
            self.screenshot_action.setEnabled(True)
            self.screenshot_action.setToolTip("")
            self.set_paused(False)
        elif mode == 'terminal':
            self.capability_bar.set_active('terminal')
            self.screenshot_action.setEnabled(False)
            self.screenshot_action.setToolTip("Screenshots are not available in the Terminal panel")
            self.set_paused(False)
        elif mode == 'vision':
            self.capability_bar.set_active('vision')
            self.screenshot_action.setEnabled(True)
            self.screenshot_action.setToolTip("")
        elif mode == 'browser':
            self.capability_bar.set_active('browser')
            self.screenshot_action.setEnabled(False)
            self.screenshot_action.setToolTip("Screenshots are not available in the Browser panel")

        self.update_pause_button()

    def update_pause_button(self):
        """Shows Pause/Resume only for a live terminal/browser/vision task."""
        mode = getattr(self, "_active_mode_key", "chat")
        handler = None
        try:
            if mode == "terminal":
                handler = self.controller.terminal_handler
            elif mode == "browser":
                handler = self.controller.browser_handler
            elif mode == "vision":
                handler = self.controller.vision_handler
        except Exception:
            handler = None
        active = False
        try:
            active = handler is not None and getattr(handler.state, "name", "IDLE") in ("EXECUTING", "PAUSED")
        except Exception:
            active = False
        self.pause_task_button.setVisible(active)
        self._schedule_toolbar_reflow()

    def _preferred_single_row_width(self) -> int:
        """Width the toolbar needs to fit everything on one row (no clipping)."""
        widgets = [
            self.toggle_button, self.attachment_button,
            self.capability_bar, self.model_button, self.send_button,
        ]
        if not self.access_mode_button.isHidden():
            widgets.append(self.access_mode_button)
        if not self.model_effort_badge.isHidden():
            widgets.append(self.model_effort_badge)
        if not self.context_indicator_button.isHidden():
            widgets.append(self.context_indicator_button)
        if not self.pause_task_button.isHidden():
            widgets.append(self.pause_task_button)
        if not self.cancel_button.isHidden():
            widgets.append(self.cancel_button)

        total = 0
        for w in widgets:
            total += min(w.sizeHint().width(), w.maximumWidth())
        spacing = self.bottom_toolbar_layout.spacing()
        total += spacing * (len(widgets) + 1)
        margins = self.layout.contentsMargins()
        total += margins.left() + margins.right()
        return total + 8  # small buffer so the last widget is never clipped

    def _workspace_available_width(self) -> int:
        """Width the chat/input column really has, in logical px.

        Zero before the first layout pass, while the workspace column still has
        no geometry; callers treat that as "use the plain ceiling".
        """
        column = getattr(self, "workspace_column", None)
        layout = getattr(self, "workspace_column_layout", None)
        if column is None or layout is None:
            return 0
        width = column.width()
        if width <= 0:
            return 0
        margins = layout.contentsMargins()
        return max(0, width - margins.left() - margins.right())

    def _content_max_width(self) -> int:
        """Cap of the centered content column, in logical (DPI-independent) px.

        The wider tier (960) kicks in only when the window really fills its own
        screen — maximised/full-screen, or at least WIDE_CONTENT_SCREEN_RATIO of
        the screen's available width. Using a *fraction* of the screen (not a
        fixed pixel number) keeps it correct under any display scaling.

        The tier is a ceiling, not the answer: the column also has to leave a
        gutter between itself and the side panels, so opening the project
        sidebar and the workspace panel narrows the text instead of pushing it
        against their edges.
        """
        cap = ChatWidget.MAX_CONTENT_WIDTH
        win = self.main_window
        try:
            if win.isFullScreen() or win.isMaximized():
                cap = ChatWidget.MAX_CONTENT_WIDTH_WIDE
            else:
                screen = win.screen()
                if screen is not None:
                    avail = screen.availableGeometry().width()
                    if avail > 0 and win.width() >= int(
                            avail * ChatWidget.WIDE_CONTENT_SCREEN_RATIO):
                        cap = ChatWidget.MAX_CONTENT_WIDTH_WIDE
        except Exception:
            pass
        return ChatWidget.fit_content_width(cap, self._workspace_available_width())

    def _schedule_toolbar_reflow(self):
        """Defer a toolbar reflow to the next event-loop tick.

        Called from resizeEvent and mode switches so the layout is rebuilt
        outside the resize pass; repeated calls within one tick are coalesced.
        """
        if getattr(self, "_reflow_timer", None) is not None:
            self._reflow_timer.start(0)
        else:
            self._reflow_toolbar()

    def _reflow_toolbar(self):
        """Single row when the window is wide enough, two rows otherwise.

        Called on every window resize and whenever the set of visible buttons
        changes (e.g. the Pause button appearing). In compact mode both rows
        are hidden, so we leave them alone.
        """
        if getattr(self, "is_compact", False):
            return
        if not hasattr(self, "pause_task_button"):
            return
        margins = self.layout.contentsMargins()
        window_available = (self.main_window.width() - margins.left()
                            - margins.right())
        # Two-tier cap for the centered bottom block: 960px on a maximised/very
        # wide window, 800px otherwise (matches the chat text column), fitted to
        # the workspace so the block keeps a gutter beside the side panels. Only
        # a maximum is set, so the window can still shrink freely.
        cap = self._content_max_width()
        column = getattr(self, "bottom_center_column", None)
        if column is not None:
            column.setMaximumWidth(cap)
        # The row split is decided for the width the toolbar really gets. With
        # both side panels open that column is much narrower than the window,
        # and measuring the window kept everything on one row that then clipped.
        available = min(window_available, cap) if cap > 0 else window_available
        single = available >= self._preferred_single_row_width()
        if single == self._toolbar_single_row:
            return
        self._toolbar_single_row = single

        extras = [self.access_mode_button, self.capability_bar,
                  self.model_button, self.model_effort_badge]
        for w in extras:
            self.bottom_toolbar_layout.removeWidget(w)
            self.mode_model_layout.removeWidget(w)

        anchor = self.bottom_toolbar_layout.indexOf(self.attachment_button)
        if anchor < 0:
            anchor = 1
        if single:
            # Keep the mode/model dropdowns next to the attach button (left
            # side), before the stretch that pushes Send to the right. The
            # model selector keeps its stretch so it fills the spare width
            # here too, exactly like in the two-row layout.
            self.bottom_toolbar_layout.insertWidget(anchor + 1, self.access_mode_button)
            self.bottom_toolbar_layout.insertWidget(anchor + 2, self.capability_bar)
            self.bottom_toolbar_layout.insertWidget(anchor + 3, self.model_button, 1)
            self.bottom_toolbar_layout.insertWidget(
                anchor + 4, self.model_effort_badge, 0, Qt.AlignmentFlag.AlignVCenter)
            self.mode_model_container.setVisible(False)
        else:
            self.bottom_toolbar_layout.insertWidget(anchor + 1, self.access_mode_button)
            self.mode_model_layout.addWidget(self.capability_bar)
            self.mode_model_layout.addWidget(self.model_button, 1)
            self.mode_model_layout.addWidget(
                self.model_effort_badge, 0, Qt.AlignmentFlag.AlignVCenter)
            self.mode_model_container.setVisible(True)

    def set_vision_mode_loading(self, is_loading: bool):
        self.capability_bar.set_vision_loading(bool(is_loading))

    def _update_send_button_state(self):
        has_text = bool(self.user_input.toPlainText().strip())
        has_attachments = bool(self.attachment_thumbnails)
        is_enabled = has_text or has_attachments
        self.send_button.setEnabled(is_enabled)
        self.tooltip_manager.install(self.send_button, "Send" if is_enabled else "Message text cannot be empty")
        self._refresh_action_buttons()

    def _refresh_action_buttons(self):
        """Morphs the action button: Stop while thinking with empty input,
        Send in all other cases (sending cancels the in-flight response)."""
        thinking = getattr(self, "_thinking_active", False)
        has_content = bool(self.user_input.toPlainText().strip()) or bool(self.attachment_thumbnails)
        if thinking and not has_content:
            self.send_button.setVisible(False)
            self.cancel_button.setVisible(True)
        else:
            self.cancel_button.setVisible(False)
            self.send_button.setVisible(True)
            if thinking:
                self.tooltip_manager.install(self.send_button, "Send (will be queued after the current reply)")

    def _on_send_clicked(self):
        if self.file_search_popup.isVisible():
            self._handle_suggestion_selection(self.file_search_popup.currentItem())
            return

        if self.send_button.isEnabled():
            self.user_command_submitted.emit(self.user_input.toPlainText())
            self.user_input.clear()

    def set_step_controls_enabled(self, enabled: bool):
        pass

    def set_paused(self, is_paused: bool):
        self.custom_title_bar.set_paused(is_paused)
        self.pause_task_hover_filter.set_icon_names('fa5s.play' if is_paused else 'fa5s.pause')

    def set_main_window_title(self, text: str):
        self.main_window.setWindowTitle(text)

    def set_title_bar_text(self, text: str):
        self.custom_title_bar.set_title(text)

    def set_project_name(self, name: str, path: str = ""):
        """Updates the compact title-bar chip and highlights the sidebar item."""
        display = name or "Select Project"
        if len(display) > 32:
            display = display[:29].rstrip() + "..."
        tooltip = path or "Switch project"
        self.custom_title_bar.set_project_name(display, tooltip)
        try:
            self.sidebar.set_active_project(
                path, getattr(self.controller, "session_file", ""))
        except Exception:
            pass

    def show_home(self):
        """Shows the start screen (project and session lists)."""
        if self.is_compact:
            self.switchToFullMode()
        # No sidebar here: keep the usual symmetric window frame.
        self.layout.setContentsMargins(11, 11, 11, 11)
        self.home_page.refresh()
        self.home_page.setVisible(True)
        self.workspace_container.setVisible(False)
        self.custom_title_bar.setVisible(False)
        self.home_page.search.setFocus()

    def show_workspace(self):
        """Shows the chat workspace (sidebar hidden in compact mode)."""
        self.home_page.setVisible(False)
        self.workspace_container.setVisible(True)
        self.sidebar.setVisible(not self.is_compact)
        self._update_right_panel_visibility()
        self._sync_right_panel_project()
        self.custom_title_bar.setVisible(self.is_compact)
        if not self.is_compact:
            # The sidebar rail is flush with the window's left edge; its own
            # equal left/right inset provides the visual padding.
            self.layout.setContentsMargins(0, 11, 11, 11)

    def show_settings_page(self):
        """Shows the full-window settings page in place of the workspace."""
        if self.is_compact:
            self.switchToFullMode()
        if self.settings_page is None:
            from src.ui.settings_window import SettingsPage
            page = SettingsPage(self.settings_manager, self.controller)
            page.back_requested.connect(self.hide_settings_page)
            page.setVisible(False)
            self.settings_page = page
            self.layout.addWidget(page, 1)
        self.home_page.setVisible(False)
        self.workspace_container.setVisible(False)
        self.custom_title_bar.setVisible(False)
        self.layout.setContentsMargins(0, 11, 11, 11)
        self.settings_page.setVisible(True)
        self.settings_page.raise_()

    def hide_settings_page(self):
        """Leaves settings and restores the workspace or the home screen."""
        if self.settings_page is None:
            return
        self.settings_page.setVisible(False)
        if getattr(self.controller, "project_root", ""):
            self.show_workspace()
        else:
            self.show_home()

    def refresh_sidebar(self):
        """Reloads the sidebar tree and restores the active project/session."""
        try:
            self.sidebar.refresh()
            manager = getattr(self.controller, "project_manager", None)
            if manager is not None:
                self.sidebar.set_active_project(
                    manager.get_active_root(),
                    getattr(self.controller, "session_file", ""))
        except Exception as e:
            logging.warning(f"Could not refresh sidebar: {e}")
        self._sync_right_panel_project()

    def refresh_project_views(self):
        """Reloads project/session lists in the sidebar and home screen."""
        self.refresh_sidebar()
        try:
            self.home_page.refresh()
        except Exception as e:
            logging.warning(f"Could not refresh home page: {e}")

    def _show_project_menu(self):
        """Popover listing recent projects plus open/create actions."""
        manager = getattr(self.controller, "project_manager", None)
        menu = QMenu(self.main_window)
        active = ""
        if manager is not None:
            active = manager.get_active_root()
            recents = manager.recent_projects()
            if recents:
                header = menu.addAction("Recent projects")
                header.setEnabled(False)
                for item in recents:
                    path = item.get("path", "")
                    label = item.get("name") or os.path.basename(path) or path
                    action = menu.addAction(label)
                    action.setToolTip(path)
                    if path == active:
                        action.setText(f"{label}  *")
                    action.triggered.connect(
                        lambda _checked=False, p=path: self.project_switch_requested.emit(p))
                menu.addSeparator()
        open_action = menu.addAction("Open Project Folder...")
        open_action.triggered.connect(self.project_open_requested.emit)
        anchor = self.custom_title_bar.project_button
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def clear_chat(self):
        self.plan_panel.clear_plan()
        self.job_panel.clear()
        self.hide_output_preview()
        self.chat_widget.clear_messages()

    def set_assistant_name(self, name: str):
        self.chat_widget.set_assistant_name(name)

    def add_attachment(self, file_path, is_folder=False):
        pixmap = None
        display_name = None

        if isinstance(file_path, Image.Image):
            if file_path.mode != 'RGBA':
                file_path = file_path.convert('RGBA')
            qimage = ImageQt(file_path)
            pixmap = QPixmap.fromImage(qimage)
            display_name = "Pasted Image"
            file_path = None
        elif isinstance(file_path, str):
            _, ext = os.path.splitext(file_path.lower())
            if ext in IMAGE_EXTENSIONS:
                pixmap = QPixmap(file_path)

        chip = AttachmentChip(
            theme_manager=self.controller.theme_manager,
            file_path=file_path,
            pixmap=pixmap,
            display_name=display_name,
            is_folder=is_folder
        )
        chip.remove_requested.connect(self._on_remove_thumbnail_requested)
        chip.preview_requested.connect(self._open_attachment_preview)
        self.attachment_layout.addWidget(chip)
        self.attachment_thumbnails.append(chip)
        QTimer.singleShot(0, self._update_attachment_area_height)
        self._update_send_button_state()

    def _open_attachment_preview(self, chip):
        self._show_image_preview(
            chip.preview_pixmap(), chip.attachment_name())

    def _open_attachment_path(self, path: str, name: str = ""):
        """Open a preview for an image already recorded in a chat message."""
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        self._show_image_preview(pixmap, name or os.path.basename(path))

    def _open_session_attachment(self, path: str):
        """Open an attachment recorded in the session journal."""
        if not path:
            return
        if not os.path.exists(path):
            QMessageBox.information(
                self.main_window, "Attachment",
                "This attachment is no longer on disk.")
            return
        if os.path.isdir(path):
            editor_launcher.reveal_path(path)
            return
        _, ext = os.path.splitext(path.lower())
        if ext in IMAGE_EXTENSIONS:
            self._open_attachment_path(path)
            return
        editor_launcher.open_path(path)

    def _show_image_preview(self, pixmap, title: str):
        if pixmap is None or pixmap.isNull():
            return
        dialog = ImagePreviewDialog(pixmap, title, self.main_window)
        dialog.exec()
        dialog.deleteLater()

    def _on_remove_thumbnail_requested(self, thumbnail_widget):
        if thumbnail_widget in self.attachment_thumbnails:
            index = self.attachment_thumbnails.index(thumbnail_widget)
            self.attachment_removed.emit(index)

    def remove_thumbnail(self, index):
        if 0 <= index < len(self.attachment_thumbnails):
            thumbnail_to_remove = self.attachment_thumbnails.pop(index)
            self.attachment_layout.removeWidget(thumbnail_to_remove)
            thumbnail_to_remove.deleteLater()
            self.attachment_layout.update()
            QTimer.singleShot(0, self._update_attachment_area_height)
            self._update_send_button_state()

    def clear_thumbnails(self):
        while self.attachment_thumbnails:
            self.remove_thumbnail(0)

    def _show_attachment_menu(self):
        """Open the attach menu above the button (which has no arrow of its own)."""
        menu = self.attachment_menu
        menu.adjustSize()
        anchor = self.attachment_button.mapToGlobal(QPoint(0, 0))
        menu.exec(QPoint(anchor.x(), anchor.y() - menu.sizeHint().height()))

    def update_attachment_menu_state(self, image_count: int, total_count: int):
        """Updates the enabled state of attachment menu actions based on current counts."""
        can_add_images = image_count < 3
        can_add_files = total_count < 50

        self.screenshot_action.setEnabled(can_add_images)
        self.add_file_action.setEnabled(can_add_files)
        self.add_folder_action.setEnabled(can_add_files)

    def _attachment_thumbnail_html(self, record: dict) -> str:
        """Small clickable image preview shown above a sent user message."""
        root = getattr(self.controller, "project_root", "") or ""
        path = resolve_path(record, root)
        if not path or not os.path.isfile(path):
            return ""
        thumb = image_thumbs.thumbnail(path)
        if thumb is None:
            return ""
        encoded, width, height = thumb
        payload = quote(f"{os.path.abspath(path)}|{record.get('name') or 'Image'}", safe="")
        return (
            '<p align="right" style="margin: 0 0 10px 0;">'
            f'<a href="attachment-preview:{payload}">'
            f'<img src="{encoded}" '
            f'width="{width}" height="{height}"></a></p>'
        )

    def build_user_message_html(self, content: str, queued: bool = False,
                                attachments=None) -> str:
        """Build a safe, right-aligned user prompt bubble."""
        theme = self.controller.theme_manager
        image_parts = []
        text_records = []
        for record in attachments or []:
            if (isinstance(record, dict) and record.get("kind") == "image"
                    and str(record.get("status") or "ok") == "ok"):
                preview = self._attachment_thumbnail_html(record)
                if preview:
                    image_parts.append(preview)
                    continue
            text_records.append(record)
        return render_user_message(
            content,
            background=theme.get_style("USER_MESSAGE_BG"),
            text_color=theme.get_style("USER_MESSAGE_TEXT"),
            meta_color=theme.get_style("COLLAPSIBLE_META"),
            queued=queued,
            attachments=text_records,
            attachment_bg=theme.get_style("COLLAPSIBLE_BG"),
            attachment_border=theme.get_style("COLLAPSIBLE_BORDER"),
            attachment_text=theme.get_style("COLLAPSIBLE_META"),
            warning_color=theme.get_style("WARNING_COLOR"),
            error_color=theme.get_style("ERROR_COLOR"),
            images_html="".join(image_parts),
        )

    def add_user_message(self, content: str, queued: bool = False,
                         attachments=None, ts=None):
        """Add the user's own prompt as a safe, readable card."""
        return self.add_message(
            self.build_user_message_html(
                content, queued=queued, attachments=attachments),
            force_scroll=True, role='user', ts=ts, user_text=content)

    def set_user_pending(self, handle, pending: bool = True):
        """Marks a user bubble as not yet delivered to the model."""
        return self.chat_widget.set_user_pending(handle, pending)

    def add_error_message(self, message, force_scroll=True):
        error_style = self.controller.theme_manager.get_style("ERROR_MESSAGE_STYLE")
        self.add_message(f'<span style="{error_style}">{message}</span>', force_scroll=force_scroll)

    def add_formatted_message(self, content: str, prefix_html: str = "",
                              copyable: bool = False, actor: str = None):
        """
        Processes raw text content, detects if it's Markdown, converts it to HTML,
        and adds it to the chat widget, optionally prepending a raw HTML prefix.
        Single pipeline (see ui.text_render), as before.

        The text is an assistant message: it carries the hover strip (model,
        time, copy) and, when ``copyable`` says this is the model's final
        answer, a copy button holding the plain text.
        """
        self.add_message(render_text(content, prefix_html, markdown_ok=True),
                         force_scroll=True, role='assistant',
                         copy_text=str(content or ''), copyable=copyable,
                         actor=actor)

    def add_suggestions(self, items):
        """Clickable next-step suggestions under an assistant answer."""
        items = normalize_suggestions(items)
        if not items:
            return None
        return self.add_message({'type': 'suggestions', 'items': items},
                                force_scroll=True)

    def add_text(self, content: str, prefix_html: str = ""):
        """Adds machine-generated text (errors, statuses, reports) via the
        unified pipeline with markdown detection OFF (deterministic escape)."""
        self.add_message(render_text(content, prefix_html, markdown_ok=False))

    def add_reasoning(self, content: str, force_scroll: bool = True):
        """Adds model reasoning as a muted, collapsed block."""
        color = self.controller.theme_manager.get_style("COLLAPSIBLE_META")
        self.add_message({
            'type': 'collapsible',
            'title': 'Reasoning',
            'content': render_reasoning(content, color),
            'content_is_html': True,
            'state': 'collapsed',
            'title_color': color,
        }, force_scroll=force_scroll)

    def add_message(self, html_content, force_scroll=False, keep_stream=False, role=None,
                    ts=None, user_text=None, copy_text=None, copyable=False,
                    actor=None):
        if self.is_compact:
            force_scroll = True
        return self.chat_widget.add_message(html_content, force_scroll=force_scroll,
                                            keep_stream=keep_stream, role=role,
                                            ts=ts, user_text=user_text,
                                            copy_text=copy_text, copyable=copyable,
                                            actor=actor)

    def add_notice(self, html_content, seconds=None, force_scroll=False):
        """Adds a status notice that leaves the chat after a few seconds."""
        if self.is_compact:
            force_scroll = True
        return self.chat_widget.add_notice(html_content, seconds=seconds,
                                           force_scroll=force_scroll)

    def update_collapsible(self, msg, title: str, content_html: str) -> bool:
        """Updates an existing collapsible block, preserving its state."""
        return self.chat_widget.update_collapsible(msg, title, content_html)

    def update_job_block(self, job_id: int, data: dict) -> bool:
        return self.chat_widget.update_job_block(job_id, data)

    # --- Activity blocks ---

    def add_activity_block(self, title: str = "Working", expanded: bool = True):
        """Starts one turn's activity block; returns its message handle."""
        return self.chat_widget.add_activity_block(
            title, expanded=expanded and not self.is_compact)

    def activity_add_thought(self, msg, text: str, expanded: bool = False) -> bool:
        return self.chat_widget.activity_add_thought(msg, text, expanded)

    def activity_add_note(self, msg, text: str) -> bool:
        return self.chat_widget.activity_add_note(msg, text)

    def update_plan_panel(self, steps) -> None:
        """Shows/refreshes the live plan strip above the composer."""
        self.plan_panel.set_plan(steps)

    def clear_plan_panel(self) -> None:
        """Drops the live plan; called whenever the turn ends or the UI resets."""
        self.plan_panel.clear_plan()

    def track_job(self, job: dict) -> None:
        """Pins a launched background job above the composer."""
        self.job_panel.track_job(job)

    def update_job_state(self, job_id: int, state: str,
                         status: str | None = None) -> None:
        self.job_panel.update_job_state(job_id, state, status)

    def finish_job(self, job_id: int) -> None:
        """Drops a finished job from the pinned strip."""
        self.job_panel.finish_job(job_id)

    def clear_jobs(self) -> None:
        self.job_panel.clear()

    # --- Output preview on hover ------------------------------------------

    def _on_job_output_hovered(self, job, row) -> None:
        """A background job row asked for its output card."""
        if self.is_compact or not isinstance(job, dict) or row is None:
            return
        try:
            anchor = QRect(row.mapToGlobal(QPoint(0, 0)), row.size())
        except Exception:
            return
        hint = str(job.get("ready_url") or job.get("cwd") or "")
        self._show_output_preview(job.get("job_id"), anchor, hint=hint,
                                  background=True)

    def _on_live_step_hovered(self, payload) -> None:
        """A running shell step in the transcript asked for its output card."""
        if self.is_compact or not isinstance(payload, dict):
            return
        rect = payload.get("rect")
        if not isinstance(rect, QRect) or rect.isNull():
            return
        job_id = payload.get("job_id")
        self._show_output_preview(job_id, rect, hint="",
                                  background=job_id is not None)

    def _on_output_preview_unhovered(self) -> None:
        preview = getattr(self, "output_preview", None)
        if preview is not None:
            preview.schedule_hide()

    def _show_output_preview(self, job_id, anchor, hint: str = "",
                             background: bool = False) -> None:
        if self.is_compact or anchor is None:
            return
        chunk = self.controller.read_shell_output(job_id)
        if chunk.get("status") not in ("running", "done"):
            return
        try:
            resolved = int(chunk.get("job_id"))
        except (TypeError, ValueError):
            return
        if (resolved == self._preview_job_id
                and self.output_preview.is_open()):
            return
        self._preview_job_id = resolved
        self._preview_offset = int(chunk.get("offset") or 0)
        if not self.output_preview.show_for(chunk, anchor, hint=hint,
                                            detached=background):
            return
        self.output_preview.feed(
            str(chunk.get("text") or ""), chunk.get("elapsed_seconds"),
            "running" if chunk.get("running")
            else str(chunk.get("state") or "done"))
        if chunk.get("running"):
            self._preview_timer.start()
        else:
            self._preview_timer.stop()

    def _pump_output_preview(self) -> None:
        """Appends what the previewed job printed since the last read."""
        preview = getattr(self, "output_preview", None)
        if preview is None or not preview.is_open() or self._preview_job_id is None:
            self._preview_timer.stop()
            return
        chunk = self.controller.read_shell_output(
            self._preview_job_id, offset=self._preview_offset)
        if chunk.get("status") not in ("running", "done"):
            # A foreground command is reaped with its result the moment it
            # ends; the card keeps what it already shows.
            preview.finish("done")
            self._preview_timer.stop()
            return
        self._preview_offset = int(
            chunk.get("offset") or self._preview_offset)
        preview.feed(
            str(chunk.get("text") or ""), chunk.get("elapsed_seconds"),
            "running" if chunk.get("running")
            else str(chunk.get("state") or "done"))
        if not chunk.get("running"):
            self._preview_timer.stop()

    def _on_output_preview_closed(self) -> None:
        self._preview_timer.stop()
        self._preview_job_id = None
        self._preview_offset = 0

    def hide_output_preview(self) -> None:
        preview = getattr(self, "output_preview", None)
        if preview is not None:
            preview.hide_now()

    def _on_application_state_changed(self, state) -> None:
        # The card floats over the app; it must not survive the window losing
        # the foreground (another app, a minimized window).
        if state != Qt.ApplicationActive:
            self.hide_output_preview()

    def _open_output_log(self, path: str) -> None:
        if not path:
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as e:
            logging.warning(f"Could not open the job log: {e}")

    def activity_add_step(self, msg, title: str, args: str = "",
                          detail_html: str | None = None, name: str = ""):
        return self.chat_widget.activity_add_step(
            msg, title, args=args, detail_html=detail_html, name=name)

    def activity_update_step(self, msg, row, **fields) -> bool:
        return self.chat_widget.activity_update_step(msg, row, **fields)

    def finish_activity(self, msg, title: str | None = None,
                        status: str = "ok", collapse: str | None = None) -> bool:
        return self.chat_widget.finish_activity(
            msg, title=title, status=status, collapse=collapse)

    def add_image_to_history(self, image_bytes: bytes):
        if not image_bytes:
            return
        try:
            import base64
            b64_string = base64.b64encode(image_bytes).decode('utf-8')
            html_content = f'<img src="data:image/webp;base64,{b64_string}" width="300">'
            self.add_message(html_content, force_scroll=True)
        except Exception as e:
            logging.error(f"Failed to add image to chat history: {e}")

    def append_to_stream(self, text_chunk):
        self.chat_widget.append_to_stream(text_chunk)

    def show_tool_progress(self, ticket: int, html_text: str):
        self.chat_widget.show_tool_progress(ticket, html_text)

    def hide_tool_progress(self, ticket: int):
        self.chat_widget.hide_tool_progress(ticket)

    def finalize_stream(self, final_html_content: str):
        self.chat_widget.finalize_stream(final_html_content)

    def add_queued_message(self, raw_html: str, display_html: str,
                           text: str = "", ts=None):
        """Adds a dimmed user message for the chat queue; returns its handle."""
        return self.chat_widget.add_queued_message(
            raw_html, display_html, text=text, ts=ts)

    def activate_queued_message(self, handle):
        """Un-dims a queued message once it becomes the active turn."""
        self.chat_widget.activate_queued_message(handle)

    def remove_queued_message(self, handle):
        """Removes an abandoned queued message from the chat."""
        self.chat_widget.remove_message(handle)

    def _input_locked_during_thinking(self) -> bool:
        """Whether typing is blocked while the model thinks.

        Only Vision acts on the user's screen and needs undivided focus.
        Chat/terminal/browser requests never touch it, so the input stays
        enabled there — a newly sent message cancels the in-flight one.
        """
        return getattr(self.controller, "state", None) == ControllerState.VISION

    def show_thinking_ui(self, show: bool, message: str = "Thinking"):
        self.user_input.setDisabled(show and self._input_locked_during_thinking())
        self._thinking_active = show
        self.chat_widget.show_thinking_ui(show, message)
        self._update_send_button_state()

    def update_timer(self, seconds: int):
        self.chat_widget.update_timer(seconds)

    def show_retry_countdown(self, attempt_num: int, max_attempts: int, wait_seconds: float):
        self.chat_widget.show_retry_countdown(attempt_num, max_attempts, wait_seconds)

    def update_retry_countdown(self, remaining: float):
        self.chat_widget.update_retry_countdown(remaining)

    def stop_retry_countdown(self):
        self.chat_widget.stop_retry_countdown()

    def display_confirmation_dialog(self, message):
        msg_box = QMessageBox(self.main_window)
        msg_box.setWindowTitle("Confirm Action")
        msg_box.setText(message)
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        msg_box.setDefaultButton(QMessageBox.Cancel)
        return msg_box.exec() == QMessageBox.Ok

    def show_main_window(self):
        import time
        t0 = time.perf_counter()
        saved = self.settings_manager.get("window_geometry", {}) or {}
        restored = self._restore_window_geometry(saved)
        if restored and saved.get("maximized"):
            self.main_window.showMaximized()
        elif restored:
            self.main_window.show()
        else:
            self.main_window.showMaximized()
        QTimer.singleShot(0, self._remember_full_geometry)
        self._schedule_window_icon_refresh()
        logging.info(f"show_main_window called, took {time.perf_counter() - t0:.3f}s to return")

    def _schedule_window_icon_refresh(self):
        """Re-applies the window icon once the shell can actually read it.

        Windows asks the window for its icon (WM_GETICON) when it creates the
        taskbar button. Startup keeps the GUI thread busy for seconds after the
        window is shown (session replay, provider warm-up), so that query times
        out and the shell falls back to the generic "unknown application" glyph
        and keeps it - the title bar shows the real icon, the taskbar does not.
        Setting the icon again when the thread is free makes the shell ask
        again, so a few attempts are queued instead of one that may land during
        the same stall.
        """
        def apply_icon():
            try:
                QApplication.setWindowIcon(self.window_icon)
                self.main_window.setWindowIcon(self.window_icon)
            except Exception as e:
                logging.debug(f"Window icon refresh failed: {e}")

        for delay_ms in (0, 1500, 5000, 12000):
            QTimer.singleShot(delay_ms, apply_icon)

    def _restore_window_geometry(self, saved: dict) -> bool:
        """Applies the remembered frame; False when there is nothing sane to restore."""
        try:
            width = int(saved.get("width") or 0)
            height = int(saved.get("height") or 0)
            x, y = int(saved.get("x") or 0), int(saved.get("y") or 0)
        except (TypeError, ValueError):
            return False
        if width < 400 or height < 300:
            return False
        rect = QRect(x, y, width, height)
        # A monitor may have been unplugged since the app was closed; dropping
        # an off-screen frame avoids an invisible window at startup.
        if not any(s.availableGeometry().intersects(rect) for s in QApplication.screens()):
            return False
        self.full_mode_geometry = rect
        self.main_window.setGeometry(rect)
        return True

    def _capture_full_frame(self):
        """Remembers the full-mode frame before the window turns compact."""
        try:
            win = self.main_window
            maximized = win.isMaximized()
            rect = win.normalGeometry() if maximized else win.geometry()
            self._last_full_frame = (QRect(rect), maximized)
        except Exception as e:
            logging.warning(f"Could not capture full-mode frame: {e}")

    def save_window_state(self):
        """Persists the full-mode frame and sidebar state for the next launch."""
        payload = {
            "sidebar_collapsed": bool(getattr(self.sidebar, "collapsed", False)),
            "right_panel_collapsed": bool(
                getattr(self.right_panel, "collapsed", False)),
        }
        try:
            if self.is_compact:
                rect, maximized = self._last_full_frame or (self.full_mode_geometry, False)
            else:
                win = self.main_window
                maximized = win.isMaximized()
                rect = win.normalGeometry() if maximized else win.geometry()
            payload["window_geometry"] = {
                "x": rect.x(),
                "y": rect.y(),
                "width": rect.width(),
                "height": rect.height(),
                "maximized": maximized,
            }
        except Exception as e:
            logging.warning(f"Could not capture window geometry: {e}")
        try:
            self.settings_manager.set_many(payload, emit=False)
        except Exception as e:
            logging.warning(f"Could not save window state: {e}")

    def _restore_sidebar_state(self):
        if self.settings_manager.get("sidebar_collapsed", False):
            self.sidebar.set_collapsed(True)

    def _restore_right_panel_state(self):
        if self.settings_manager.get("right_panel_collapsed", False):
            self.right_panel.set_collapsed(True)

    def _update_right_panel_visibility(self):
        """The workspace panel is available in every panel."""
        panel = getattr(self, "right_panel", None)
        if panel is None:
            return
        workspace = getattr(self, "workspace_container", None)
        visible = (
            not getattr(self, "is_compact", True)
            and workspace is not None
            and not workspace.isHidden()
        )
        panel.setVisible(visible)

    def set_session_panel(self, payload: dict):
        """Session and Activity details shown by the right panel in every panel."""
        panel = getattr(self, "right_panel", None)
        if panel is not None:
            panel.set_session_data(payload or {})

    def _sync_right_panel_project(self):
        manager = getattr(self.controller, "project_manager", None)
        root = manager.get_active_root() if manager is not None else ""
        self.right_panel.set_project_root(root)

    def _remember_full_geometry(self):
        if not self.is_compact and self.main_window.isMaximized():
            self.full_mode_geometry = self.main_window.geometry()

    def show_highlight(self, x1, y1, x2, y2):
        self.overlay = HighlightOverlay(QRect(x1, y1, x2-x1, y2-y1))
        self.overlay.show()

    def hide_highlight(self):
        if self.overlay:
            self.overlay.close()
            self.overlay = None

    def toggle_mode(self):
        if self.is_compact:
            self.switchToFullMode()
            self.toggle_fullscreen_button_clicked.emit()
        else:
            self.switchToCompactMode()

    def switchToCompactMode(self):
        logging.info("switchToCompactMode: enter")
        self._capture_full_frame()
        self.is_compact = True
        self.compact_position = self.settings_manager.get("compact_mode", {}).get("position", "bottom_center")
        self.attachment_scroll_area.setVisible(False)
        self.plan_panel.set_suppressed(True)
        self.job_panel.set_suppressed(True)
        self.hide_output_preview()
        self.input_container.setVisible(False)
        self.mode_model_container.setVisible(False)
        self.bottom_toolbar_container.setVisible(False)
        self._toolbar_single_row = None  # re-evaluate when returning to full mode
        self.custom_title_bar.setVisible(True)
        self.sidebar.setVisible(False)
        self._update_right_panel_visibility()
        self.main_window.size_grip.setVisible(True)
        self.layout.setContentsMargins(5, 0, 5, 5)
        self.main_window.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool | Qt.FramelessWindowHint)
        self.main_window.setMinimumHeight(self.compact_mode_geometry.height())
        self.main_window.show()
        self.animate_geometry(self.compact_mode_geometry)
        self.tooltip_manager.install(self.toggle_button, "Maximize")
        self.toggle_button_hover_filter.set_icon_names('fa5s.expand')
        logging.info("switchToCompactMode: done")

    def is_click_colliding(self, x: int, y: int) -> bool:
        if not self.is_compact:
            return False
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes.wintypes import HWND, RECT
                hwnd = self.main_window.winId().__int__()
                rect = RECT()
                ctypes.windll.user32.GetWindowRect(HWND(hwnd), ctypes.byref(rect))
                window_rect = QRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
                return window_rect.contains(x, y)
            except Exception as e:
                logging.warning(f"Could not use WinAPI to get window rect ({e}), falling back to frameGeometry.")
                return self.main_window.windowHandle().frameGeometry().contains(x, y)
        else:
            return self.main_window.windowHandle().frameGeometry().contains(x, y)

    def relocate_compact_window(self):
        current_pos = self.compact_position
        new_pos = ("bottom_center" if current_pos == "middle_center" else
                   current_pos.replace("top", "bottom") if "top" in current_pos else
                   current_pos.replace("bottom", "top"))
        self.compact_position = new_pos
        logging.info(f"Relocating compact window to '{new_pos}'.")
        width, height = self.main_window.width(), self.main_window.height()
        screen_rect = QApplication.primaryScreen().geometry()
        margin = 40
        if "top" in new_pos:
            y = margin
        elif "bottom" in new_pos:
            y = screen_rect.height() - height - margin
        else:
            y = (screen_rect.height() - height) // 2

        if "left" in new_pos:
            x = margin
        elif "right" in new_pos:
            x = screen_rect.width() - width - margin
        else:
            x = (screen_rect.width() - width) // 2
        self.main_window.setGeometry(QRect(x, y, width, height))

    def switchToFullMode(self):
        logging.info("switchToFullMode: enter")
        self.is_compact = False
        self.custom_title_bar.setVisible(False)
        self.sidebar.setVisible(True)
        self._update_right_panel_visibility()
        self.main_window.size_grip.setVisible(False)
        if self.attachment_thumbnails:
            self.attachment_scroll_area.setVisible(True)
        left_margin = 0 if self.workspace_container.isVisible() else 11
        self.layout.setContentsMargins(left_margin, 11, 11, 11)
        self.plan_panel.set_suppressed(False)
        self.job_panel.set_suppressed(False)
        self.input_container.setVisible(True)
        self.bottom_toolbar_container.setVisible(True)
        self._toolbar_single_row = None  # force single/two-row re-evaluation
        self._schedule_toolbar_reflow()
        self.user_input.set_compact_mode(False)
        self.main_window.setMinimumSize(0, 0)
        self.main_window.setMaximumSize(16777215, 16777215)
        self.main_window.setWindowFlags(Qt.WindowFlags())
        self.main_window.show()
        self.animate_geometry(self.full_mode_geometry, self.chat_widget.scroll_to_bottom)
        self.tooltip_manager.install(self.toggle_button, "Minimize")
        self.toggle_button_hover_filter.set_icon_names('fa5s.compress')
        logging.info("switchToFullMode: done")

    def animate_geometry(self, target_geometry, on_finished=None):
        self.animation = QPropertyAnimation(self.main_window, b"geometry")
        self.animation.setDuration(300)
        self.animation.setStartValue(self.main_window.geometry())
        self.animation.setEndValue(target_geometry)
        self.animation.setEasingCurve(QEasingCurve.InOutCubic)
        if on_finished:
            self.animation.finished.connect(on_finished)
        self.animation.start(QPropertyAnimation.DeleteWhenStopped)

    def show_warning_dialog(self, message: str) -> bool:
        msg_box = QMessageBox(self.main_window)
        msg_box.setWindowTitle("Warning")
        msg_box.setText(message)
        msg_box.setIcon(QMessageBox.Warning)
        apply_button = msg_box.addButton("Apply", QMessageBox.AcceptRole)
        msg_box.addButton(QMessageBox.Cancel)
        msg_box.setDefaultButton(apply_button)
        msg_box.setStyleSheet(styles.DARK_STYLE)
        msg_box.exec()
        return msg_box.clickedButton() == apply_button
