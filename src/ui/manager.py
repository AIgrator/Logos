"""
This module is responsible for managing all visual elements of the assistant.
It is a "dumb" view, controlled by the AppController.
"""
import logging
import os
import sys
import qtawesome as qta
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import (QBuffer, QIODevice, QEasingCurve, QMimeData, QPropertyAnimation,
                            QRect, QSize, Qt, Signal, QTimer, Slot)
from PySide6.QtGui import (QActionGroup, QColor, QIcon, QImage, QPainter, QPen, QPixmap,
                           QTextCursor, QTextOption)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QMenu, QPushButton, QSizeGrip, QStackedWidget,
                               QTextEdit, QVBoxLayout, QWidget)

from . import styles
from .utils import CustomToolTipManager, IconHoverFilter
from .widgets.attachment_chip import AttachmentChip
from .widgets.chat_widget import ChatWidget
from .widgets.custom_title_bar import CustomTitleBar
from .widgets.flow_layout import FlowLayout
from .widgets.non_scrollable_scroll_area import NonScrollableScrollArea

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
        if self.size_grip.isVisible():
            grip_size = self.size_grip.size()
            self.size_grip.move(self.width() - grip_size.width(), self.height() - grip_size.height())
            self.size_grip.raise_()

    def closeEvent(self, event):
        logging.info("MainWindow closeEvent received. Emitting closing signal for graceful shutdown.")
        self.closing.emit()
        event.ignore()  # Let the controller handle the actual exit.

    @Slot(bool)
    def set_visibility_for_screenshot(self, visible: bool):
        """A dedicated slot to control window opacity for thread-safe screenshotting."""
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

        # Fallback to default behavior for text or other data types
        super().insertFromMimeData(source)

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
    plan_approved = Signal()
    regenerate_plan_clicked = Signal()
    edit_plan_clicked = Signal()
    plan_cancel_clicked = Signal()
    screenshot_button_clicked = Signal()
    select_files_button_clicked = Signal()
    settings_button_clicked = Signal()
    add_file_clicked = Signal()
    add_folder_clicked = Signal()
    chat_mode_selected = Signal()
    terminal_mode_selected = Signal()
    vision_mode_selected = Signal()
    browser_mode_selected = Signal()
    attachment_removed = Signal(int)
    cancel_requested = Signal()
    toggle_fullscreen_button_clicked = Signal()
    file_search_requested = Signal(str, str)
    path_selected_for_attachment = Signal(str)

    # Signals for new pilot controls
    pause_resume_clicked = Signal()
    pause_for_feedback_clicked = Signal()
    stop_clicked = Signal()
    next_step_clicked = Signal()
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
        self.main_window.setWindowIcon(QIcon("assets/ico.ico"))

        self.full_mode_geometry = QRect(100, 100, 400, 600)
        self._update_compact_mode_geometry()

        self.main_window.setGeometry(self.full_mode_geometry)
        self.central_widget = QWidget()
        self.main_window.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)
        self.layout.setContentsMargins(11, 11, 11, 11)
        self.layout.setSpacing(0)

        self.custom_title_bar = CustomTitleBar(self.controller.theme_manager)
        self.custom_title_bar.setVisible(False)
        self.layout.addWidget(self.custom_title_bar)

        self.chat_widget = ChatWidget(self.controller.theme_manager)
        self.layout.addWidget(self.chat_widget, 1)

        self.attachment_scroll_area = NonScrollableScrollArea()
        self.attachment_scroll_area.setWidgetResizable(True)
        self.attachment_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.attachment_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.attachment_widget = QWidget()
        self.attachment_layout = FlowLayout(self.attachment_widget, spacing=6)
        self.attachment_scroll_area.setWidget(self.attachment_widget)
        self.layout.addWidget(self.attachment_scroll_area)
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

        self.settings_button = QPushButton()
        self.settings_button.setIconSize(QSize(20, 20))
        self.settings_button.setFixedSize(QSize(36, 36))
        self.tooltip_manager.install(self.settings_button, "Settings")
        self.settings_button.installEventFilter(
            IconHoverFilter(self.controller.theme_manager, 'fa5s.cog', parent=self.settings_button)
        )

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
        self.tooltip_manager.install(self.attachment_button, "Attach context")
        self.attachment_button.installEventFilter(
            IconHoverFilter(self.controller.theme_manager, 'fa5s.paperclip', parent=self.attachment_button)
        )

        self.attachment_menu = QMenu(self.attachment_button)
        self.add_file_action = self.attachment_menu.addAction("Add File...")
        self.add_folder_action = self.attachment_menu.addAction("Add Folder...")
        self.attachment_menu.addSeparator()
        self.screenshot_action = self.attachment_menu.addAction("Take Screenshot")
        self.attachment_button.setMenu(self.attachment_menu)
        self.add_folder_action.setVisible(False)

        self.mode_button = QPushButton("Chat")
        self.mode_button.setFixedSize(120, 36)
        self.spinner_animation = qta.Spin(self.mode_button)
        self.mode_menu = QMenu(self.mode_button)
        self.vision_mode_spinner = qta.Spin(self.mode_menu)
        self.mode_action_group = QActionGroup(self)
        self.mode_action_group.setExclusive(True)
        self.chat_mode_action = self.mode_action_group.addAction("Chat Mode")
        self.chat_mode_action.setCheckable(True)
        self.chat_mode_action.setChecked(True)
        self.mode_menu.addAction(self.chat_mode_action)
        self.terminal_mode_action = self.mode_action_group.addAction("Terminal Mode")
        self.terminal_mode_action.setCheckable(True)
        self.mode_menu.addAction(self.terminal_mode_action)
        self.vision_mode_action = self.mode_action_group.addAction("Vision Mode")
        self.vision_mode_action.setCheckable(True)
        self.vision_mode_action.setEnabled(False)
        self.vision_mode_action.setToolTip("Initializing vision module...")
        self.mode_menu.addAction(self.vision_mode_action)
        self.browser_mode_action = self.mode_action_group.addAction("Browser Mode")
        self.browser_mode_action.setCheckable(True)
        self.mode_menu.addAction(self.browser_mode_action)
        self.mode_button.setMenu(self.mode_menu)

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

        self.plan_send_button = QPushButton()
        self.plan_send_button.setObjectName("SendButton")
        self.plan_send_button.setIconSize(QSize(20, 20))
        self.plan_send_button.setFixedSize(QSize(36, 36))
        self.tooltip_manager.install(self.plan_send_button, "Send Feedback")
        self.plan_send_button.installEventFilter(
            IconHoverFilter(self.controller.theme_manager, 'fa5s.paper-plane', parent=self.plan_send_button)
        )

        self.cancel_button = QPushButton()
        self.cancel_button.setObjectName("CancelButton")
        self.cancel_button.setIconSize(QSize(20, 20))
        self.cancel_button.setFixedSize(QSize(36, 36))
        self.tooltip_manager.install(self.cancel_button, "Cancel")
        self.cancel_button.installEventFilter(
            IconHoverFilter(self.controller.theme_manager, 'fa5s.stop', parent=self.cancel_button)
        )

        self.action_button_stack = QStackedWidget()
        self.action_button_stack.addWidget(self.send_button)
        self.action_button_stack.addWidget(self.cancel_button)
        self.action_button_stack.setFixedSize(QSize(36, 36))

        self.bottom_toolbar_layout.addWidget(self.settings_button)
        self.bottom_toolbar_layout.addWidget(self.toggle_button)
        self.bottom_toolbar_layout.addWidget(self.attachment_button)
        self.bottom_toolbar_layout.addWidget(self.mode_button)
        self.bottom_toolbar_layout.addStretch()
        self.bottom_toolbar_layout.addWidget(self.action_button_stack)

        self.layout.addWidget(self.input_container)
        self.layout.addWidget(self.bottom_toolbar_container)

        self.approve_plan_button = QPushButton("Run")
        self.regenerate_plan_button = QPushButton("Regenerate")
        self.edit_plan_button = QPushButton("Edit")
        self.plan_cancel_button = QPushButton("Cancel")

        self._plan_buttons_container = QWidget()
        self._plan_buttons_layout = QHBoxLayout(self._plan_buttons_container)
        self._plan_buttons_layout.setContentsMargins(0, 8, 0, 0)
        self._plan_buttons_layout.setSpacing(8)
        self._plan_buttons_layout.addWidget(self.approve_plan_button)
        self._plan_buttons_layout.addWidget(self.regenerate_plan_button)
        self._plan_buttons_layout.addWidget(self.edit_plan_button)
        self._plan_buttons_layout.addWidget(self.plan_cancel_button)
        self._plan_buttons_layout.addStretch()
        self._plan_buttons_layout.addWidget(self.plan_send_button)
        self.layout.addWidget(self._plan_buttons_container)
        self._plan_buttons_container.setVisible(False)

        self.send_button.clicked.connect(self._on_send_clicked)
        self.plan_send_button.clicked.connect(self._on_send_clicked)
        self.user_input.return_pressed.connect(self._on_send_clicked)
        self.user_input.escape_pressed.connect(self._hide_file_search_popup)
        self.user_input.up_arrow_pressed.connect(self._select_previous_suggestion)
        self.user_input.down_arrow_pressed.connect(self._select_next_suggestion)
        self.user_input.tab_pressed.connect(self._on_tab_pressed)
        self.user_input.image_pasted.connect(self._handle_pasted_image)
        self.user_input.files_pasted.connect(self._handle_pasted_files)
        self.approve_plan_button.clicked.connect(self.plan_approved.emit)
        self.regenerate_plan_button.clicked.connect(self.regenerate_plan_clicked.emit)
        self.edit_plan_button.clicked.connect(self.edit_plan_clicked.emit)
        self.plan_cancel_button.clicked.connect(self.plan_cancel_clicked.emit)
        self.settings_button.clicked.connect(self.settings_button_clicked.emit)
        self.toggle_button.clicked.connect(self.toggle_mode)
        self.add_file_action.triggered.connect(self.add_file_clicked.emit)
        self.add_folder_action.triggered.connect(self.add_folder_clicked.emit)
        self.screenshot_action.triggered.connect(self.screenshot_button_clicked.emit)
        self.chat_mode_action.triggered.connect(self._on_chat_mode_selected)
        self.terminal_mode_action.triggered.connect(self._on_terminal_mode_selected)
        self.vision_mode_action.triggered.connect(self._on_vision_mode_selected)
        self.browser_mode_action.triggered.connect(self._on_browser_mode_selected)
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
        self._update_icon_colors()
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
        input_pos = self.user_input.pos()
        container_pos = self.input_container.pos()

        x = container_pos.x() + input_pos.x()
        y = container_pos.y() + input_pos.y() - popup_height - 2

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

        first_chip = self.attachment_thumbnails[0]
        single_row_height = first_chip.sizeHint().height()
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
        self.icon_spinner = qta.icon('fa5s.spinner', color=icon_color, animation=self.spinner_animation)
        self.icon_spinner_vision = qta.icon('fa5s.spinner', color=icon_color, animation=self.vision_mode_spinner)
        current_toggle_icon = self.toggle_icon_maximize if self.is_compact else self.toggle_icon_minimize
        self.toggle_button.setIcon(current_toggle_icon)

    def on_settings_changed(self, new_settings):
        if 'compact_mode' in new_settings:
            self._update_compact_mode_geometry()
            if self.is_compact:
                self.animate_geometry(self.compact_mode_geometry)

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

    def _on_chat_mode_selected(self):
        self.mode_button.setText("Chat")
        self.chat_mode_selected.emit()

    def _on_terminal_mode_selected(self):
        self.mode_button.setText("Terminal")
        self.terminal_mode_selected.emit()

    def _on_vision_mode_selected(self):
        self.mode_button.setText("Vision")
        self.vision_mode_selected.emit()

    def _on_browser_mode_selected(self):
        self.mode_button.setText("Browser")
        self.browser_mode_selected.emit()

    def set_active_mode(self, mode: str):
        is_terminal = (mode == 'terminal')
        self.add_folder_action.setVisible(is_terminal)

        if mode == 'chat':
            self.chat_mode_action.setChecked(True)
            self.mode_button.setText("Chat")
            self.screenshot_action.setEnabled(True)
            self.screenshot_action.setToolTip("")
            self.set_paused(False)
        elif mode == 'terminal':
            self.terminal_mode_action.setChecked(True)
            self.mode_button.setText("Terminal")
            self.screenshot_action.setEnabled(False)
            self.screenshot_action.setToolTip("Screenshots are not available in Terminal Mode")
            self.set_paused(False)
        elif mode == 'vision':
            self.vision_mode_action.setChecked(True)
            self.mode_button.setText("Vision")
            self.screenshot_action.setEnabled(True)
            self.screenshot_action.setToolTip("")
        elif mode == 'browser':
            self.browser_mode_action.setChecked(True)
            self.mode_button.setText("Browser")
            self.screenshot_action.setEnabled(False)
            self.screenshot_action.setToolTip("Screenshots are not available in Browser Mode")

    def set_vision_mode_loading(self, is_loading: bool):
        if is_loading:
            self.vision_mode_action.setIcon(self.icon_spinner_vision)
            self.vision_mode_action.setEnabled(False)
            self.vision_mode_action.setToolTip("Initializing vision module...")
        else:
            self.vision_mode_action.setIcon(QIcon())
            self.vision_mode_action.setEnabled(True)
            self.vision_mode_action.setToolTip("")

    def _update_send_button_state(self):
        has_text = bool(self.user_input.toPlainText().strip())
        has_attachments = bool(self.attachment_thumbnails)
        is_enabled = has_text or has_attachments
        self.send_button.setEnabled(is_enabled)
        self.plan_send_button.setEnabled(has_text)
        self.tooltip_manager.install(self.send_button, "Send" if is_enabled else "Message text cannot be empty")

    def _on_send_clicked(self):
        if self.file_search_popup.isVisible():
            self._handle_suggestion_selection(self.file_search_popup.currentItem())
            return

        is_plan_feedback_mode = self._plan_buttons_container.isVisible()
        button_to_check = self.plan_send_button if is_plan_feedback_mode else self.send_button
        if button_to_check.isEnabled():
            self.user_command_submitted.emit(self.user_input.toPlainText())
            self.user_input.clear()

    def set_step_controls_enabled(self, enabled: bool):
        pass

    def set_paused(self, is_paused: bool):
        self.custom_title_bar.set_paused(is_paused)

    def set_main_window_title(self, text: str):
        self.main_window.setWindowTitle(text)

    def set_title_bar_text(self, text: str):
        self.custom_title_bar.set_title(text)

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
            IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp']
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
        self.attachment_layout.addWidget(chip)
        self.attachment_thumbnails.append(chip)
        QTimer.singleShot(0, self._update_attachment_area_height)
        self._update_send_button_state()

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

    def update_attachment_menu_state(self, image_count: int, total_count: int):
        """Updates the enabled state of attachment menu actions based on current counts."""
        can_add_images = image_count < 3
        can_add_files = total_count < 50

        self.screenshot_action.setEnabled(can_add_images)
        self.add_file_action.setEnabled(can_add_files)
        self.add_folder_action.setEnabled(can_add_files)

    def add_error_message(self, message, force_scroll=True):
        error_style = self.controller.theme_manager.get_style("ERROR_MESSAGE_STYLE")
        self.add_message(f'<span style="{error_style}">{message}</span>', force_scroll=force_scroll)

    def add_formatted_message(self, content: str, prefix_html: str = ""):
        """
        Processes raw text content, detects if it's Markdown, converts it to HTML,
        and adds it to the chat widget, optionally prepending a raw HTML prefix.
        """
        import html
        import markdown

        is_markdown = (
            "###" in content or
            "**" in content or
            "```" in content or
            "\n-" in content or
            "\n*" in content or
            "\n1." in content
        )

        if is_markdown:
            processed_content = markdown.markdown(content, extensions=['fenced_code'])
        else:
            # Just escape, don't wrap in <p> yet.
            processed_content = html.escape(content)

        if prefix_html:
            # Combine prefix and content, then wrap the whole thing in a single <p>
            # This ensures they stay on the same line.
            final_html = f'<p style="white-space: pre-wrap;">{prefix_html}{processed_content}</p>'
        else:
            # No prefix. Wrap in <p> only if it was plain text.
            # Markdown content already has its own block-level tags (p, ul, etc.)
            if not is_markdown:
                final_html = f'<p style="white-space: pre-wrap;">{processed_content}</p>'
            else:
                final_html = processed_content

        self.add_message(final_html, force_scroll=True)

    def add_message(self, html_content, force_scroll=False):
        if self.is_compact:
            force_scroll = True
        self.chat_widget.add_message(html_content, force_scroll=force_scroll)

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

    def finalize_stream(self, final_html_content: str):
        self.chat_widget.finalize_stream(final_html_content)

    def show_thinking_ui(self, show: bool, message: str = "Thinking...", actor: str = None):
        self.user_input.setDisabled(show)
        self.action_button_stack.setCurrentIndex(1 if show else 0)
        self.chat_widget.show_thinking_ui(show, message, actor=actor)
        if not show:
            self._update_send_button_state()

    def update_timer(self, seconds: int):
        self.chat_widget.update_timer(seconds)

    def set_approve_button_visible(self, visible):
        self.approve_plan_button.setVisible(visible)

    def set_regenerate_button_visible(self, visible: bool):
        self.regenerate_plan_button.setVisible(visible)

    def set_edit_button_visible(self, visible: bool):
        self.edit_plan_button.setVisible(visible)

    def set_plan_controls_visible(self, visible: bool):
        self._plan_buttons_container.setVisible(visible)
        if self.is_compact:
            self.bottom_toolbar_container.setVisible(False)
        else:
            self.bottom_toolbar_container.setVisible(not visible)

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
        self.main_window.show()
        logging.info(f"show_main_window called, took {time.perf_counter() - t0:.3f}s to return")

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
        self.is_compact = True
        self.compact_position = self.settings_manager.get("compact_mode", {}).get("position", "bottom_center")
        self.attachment_scroll_area.setVisible(False)
        self.input_container.setVisible(False)
        self.bottom_toolbar_container.setVisible(False)
        self.custom_title_bar.setVisible(True)
        self.main_window.size_grip.setVisible(True)
        self.layout.setContentsMargins(5, 0, 5, 5)
        self.main_window.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool | Qt.FramelessWindowHint)
        self.main_window.setMinimumHeight(self.compact_mode_geometry.height())
        self.main_window.show()
        self.animate_geometry(self.compact_mode_geometry)
        self.tooltip_manager.install(self.toggle_button, "Maximize")
        self.toggle_button_hover_filter.set_icon_names('fa5s.expand')

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
        self.is_compact = False
        self.custom_title_bar.setVisible(False)
        self.main_window.size_grip.setVisible(False)
        if self.attachment_thumbnails:
            self.attachment_scroll_area.setVisible(True)
        self.layout.setContentsMargins(11, 11, 11, 11)
        self.input_container.setVisible(True)
        self.bottom_toolbar_container.setVisible(True)
        self.user_input.set_compact_mode(False)
        self.main_window.setMinimumSize(0, 0)
        self.main_window.setMaximumSize(16777215, 16777215)
        self.main_window.setWindowFlags(Qt.WindowFlags())
        self.main_window.show()
        self.animate_geometry(self.full_mode_geometry, self.chat_widget.scroll_to_last_user_message)
        self.tooltip_manager.install(self.toggle_button, "Minimize")
        self.toggle_button_hover_filter.set_icon_names('fa5s.compress')

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

    def open_plan_editor(self, plan):
        try:
            from .plan_editor import open_plan_editor as _open
            return _open(self.main_window, plan)
        except Exception as e:
            logging.error(f"Failed to open plan editor: {e}")
            return None
