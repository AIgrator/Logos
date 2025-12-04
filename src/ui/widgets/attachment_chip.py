"""
This module provides an AttachmentChip widget for displaying attachments compactly.
"""
import os

from PySide6.QtCore import Qt, QFileInfo, QSize, Signal
from PySide6.QtWidgets import QFileIconProvider, QHBoxLayout, QLabel, QPushButton, QWidget

from src.ui.utils import IconHoverFilter

class AttachmentChip(QWidget):
    """
    A compact widget to display a file or image attachment with an icon,
    name, and a remove button.
    """
    remove_requested = Signal(QWidget)

    def __init__(self, theme_manager, file_path=None, pixmap=None, display_name="image", is_folder=False, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self._file_path = file_path

        self.setAttribute(Qt.WA_StyledBackground, True)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 2, 2)
        self._layout.setSpacing(4)

        # Icon
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(16, 16)
        if pixmap and not pixmap.isNull():
            self.icon_label.setPixmap(pixmap.scaled(
                self.icon_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            ))
        elif file_path:
            provider = QFileIconProvider()
            q_file_info = QFileInfo(file_path)
            icon = provider.icon(q_file_info)
            self.icon_label.setPixmap(icon.pixmap(self.icon_label.size()))

        # Name Label
        name = display_name if display_name and display_name != "image" else os.path.basename(file_path or "Image")
        self.name_label = QLabel(name)
        self.name_label.setToolTip(file_path or "Pasted image")

        # Remove Button
        self.remove_button = QPushButton()
        self.remove_button.setFixedSize(QSize(16, 16))
        self.remove_button.setIconSize(QSize(12, 12))
        self.remove_button.setToolTip("Remove")
        self._remove_button_hover = IconHoverFilter(
            self.theme_manager,
            'fa5s.times-circle',
            parent=self.remove_button
        )
        self.remove_button.installEventFilter(self._remove_button_hover)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))

        self._layout.addWidget(self.icon_label)
        self._layout.addWidget(self.name_label)
        self._layout.addWidget(self.remove_button)

        self.theme_manager.theme_changed.connect(self._apply_styles)
        self._apply_styles()

    def _apply_styles(self):
        """Applies styles from the theme manager."""
        styles = self.theme_manager.get_style("ATTACHMENT_CHIP_STYLE")
        self.setStyleSheet(styles)
        self.name_label.setStyleSheet(self.theme_manager.get_style("ATTACHMENT_CHIP_LABEL_STYLE"))
        self.remove_button.setStyleSheet(self.theme_manager.get_style("ATTACHMENT_CHIP_BUTTON_STYLE"))
