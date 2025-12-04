"""
Custom Title Bar widget for the compact, frameless window.
"""
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from src.ui.utils import IconHoverFilter


class CustomTitleBar(QWidget):
    """A custom title bar for the compact mode."""
    pause_resume_clicked = Signal()
    pause_for_feedback_clicked = Signal()
    stop_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.is_paused = False
        self.setFixedHeight(32)

        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(10, 0, 5, 0)
        self.layout.setSpacing(10)

        self.title_label = QLabel("Logos AI Assistant")

        # --- Create Buttons (icons will be set later) ---
        self.pause_resume_button = self._create_button("Pause")
        self.feedback_button = self._create_button("Pause and Input")
        self.stop_button = self._create_button("Stop")
        self.close_button = self._create_button("Close")

        # --- Install Theme-Aware Hover Filters ---
        self.pause_resume_hover_filter = IconHoverFilter(
            self.theme_manager,
            'fa5s.pause',
            parent=self.pause_resume_button
        )
        self.pause_resume_button.installEventFilter(self.pause_resume_hover_filter)

        self.feedback_button.installEventFilter(
            IconHoverFilter(
                self.theme_manager,
                'fa5s.pencil-alt',
                parent=self.feedback_button
            )
        )
        self.stop_button.installEventFilter(IconHoverFilter(self.theme_manager, 'fa5s.stop', parent=self.stop_button))
        self.close_button.installEventFilter(
            IconHoverFilter(
                self.theme_manager,
                'fa5s.times',
                parent=self.close_button
            )
        )

        # --- Assemble Layout ---
        self.layout.addWidget(self.title_label)
        self.layout.addStretch()
        self.layout.addWidget(self.pause_resume_button)
        self.layout.addWidget(self.feedback_button)
        self.layout.addWidget(self.stop_button)
        self.layout.addWidget(self.close_button)

        # --- Connect Signals ---
        self.pause_resume_button.clicked.connect(self.pause_resume_clicked.emit)
        self.feedback_button.clicked.connect(self.pause_for_feedback_clicked.emit)
        self.stop_button.clicked.connect(self.stop_clicked.emit)
        self.close_button.clicked.connect(self.close_clicked.emit)

        self.drag_position = None

        # --- Apply Initial Styles and Icons ---
        self._apply_styles()
        self._update_icon_colors() # Initial icon setup
        self.theme_manager.theme_changed.connect(self._apply_styles)
        self.theme_manager.theme_changed.connect(self._update_icon_colors)

    def _apply_styles(self):
        """Applies styles from the theme manager to the title bar widgets."""
        self.title_label.setStyleSheet("font-weight: bold;")
        button_style = self.theme_manager.get_style("TITLE_BAR_BUTTON_STYLE")
        for button in [self.pause_resume_button, self.feedback_button, self.stop_button, self.close_button]:
            button.setStyleSheet(button_style)

    def _update_icon_colors(self):
        """
        This method is called on theme change.
        It triggers an update in the IconHoverFilters, which will fetch the new colors.
        """
        # The filters are self-managing, but we need to update the pause/play icon
        # in case the state has changed.
        self.set_paused(self.is_paused, force_update=True)

    def set_title(self, title: str):
        """Sets the text of the title label."""
        self.title_label.setText(title)

    def _create_button(self, tooltip):
        button = QPushButton("")
        button.setFixedSize(QSize(24, 24))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tooltip)
        return button

    def set_paused(self, is_paused: bool, force_update: bool = False):
        """Updates the pause/resume button's icon and tooltip."""
        if self.is_paused == is_paused and not force_update:
            return

        self.is_paused = is_paused
        if is_paused:
            self.pause_resume_button.setToolTip("Resume")
            self.pause_resume_hover_filter.set_icon_names('fa5s.play')
        else:
            self.pause_resume_button.setToolTip("Pause")
            self.pause_resume_hover_filter.set_icon_names('fa5s.pause')

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.drag_position is not None:
            self.window().move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_position = None
        event.accept()
