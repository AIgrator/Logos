"""
This module provides an AttachmentChip widget for displaying attachments compactly.
"""
import os

from PySide6.QtCore import QFileInfo, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QFileIconProvider, QHBoxLayout, QLabel, QPushButton, QWidget

from src.ui.utils import IconHoverFilter

IMAGE_TILE_SIZE = 64
IMAGE_TILE_RADIUS = 10


def _rounded_square(pixmap: QPixmap) -> QPixmap:
    """Center-crop an image into a rounded square thumbnail."""
    scaled = pixmap.scaled(
        IMAGE_TILE_SIZE, IMAGE_TILE_SIZE,
        Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    result = QPixmap(IMAGE_TILE_SIZE, IMAGE_TILE_SIZE)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    clip = QPainterPath()
    clip.addRoundedRect(
        QRectF(0, 0, IMAGE_TILE_SIZE, IMAGE_TILE_SIZE),
        IMAGE_TILE_RADIUS, IMAGE_TILE_RADIUS)
    painter.setClipPath(clip)
    x = (scaled.width() - IMAGE_TILE_SIZE) // 2
    y = (scaled.height() - IMAGE_TILE_SIZE) // 2
    painter.drawPixmap(-x, -y, scaled)
    painter.end()
    return result


class AttachmentChip(QWidget):
    """
    A compact widget to display a file or image attachment with an icon,
    name, and a remove button. Images are shown as a small square preview
    (the name is available as a tooltip); other files keep the name label.
    """
    remove_requested = Signal(QWidget)
    preview_requested = Signal(QWidget)

    def __init__(self, theme_manager, file_path=None, pixmap=None, display_name="image", is_folder=False, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self._file_path = file_path
        self._preview_pixmap = (
            pixmap if pixmap is not None and not pixmap.isNull() else None)
        self._name = (
            display_name if display_name and display_name != "image"
            else os.path.basename(file_path or "Image"))
        self._image_tile = self._preview_pixmap is not None

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setToolTip(self._name)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 2, 2)
        self._layout.setSpacing(4)

        if self._image_tile:
            self._build_image_tile()
        else:
            self._build_file_chip()

        self._build_remove_button()
        if self._image_tile:
            self.remove_button.move(
                IMAGE_TILE_SIZE - self.remove_button.width() - 2, 2)
            self.remove_button.setCursor(Qt.ArrowCursor)
            self.setCursor(Qt.PointingHandCursor)
            # Clicks anywhere on the tile (except Remove) open the preview.
            self.thumbnail_label.setAttribute(
                Qt.WA_TransparentForMouseEvents, True)
        else:
            self._layout.addWidget(self.remove_button)

        self.theme_manager.theme_changed.connect(self._apply_styles)
        self._apply_styles()

    def _build_image_tile(self):
        self.setFixedSize(IMAGE_TILE_SIZE, IMAGE_TILE_SIZE)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.thumbnail_label = QLabel(self)
        self.thumbnail_label.setFixedSize(IMAGE_TILE_SIZE, IMAGE_TILE_SIZE)
        self.thumbnail_label.setPixmap(_rounded_square(self._preview_pixmap))
        self._layout.addWidget(self.thumbnail_label)

    def _build_file_chip(self):
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(16, 16)
        self.icon_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        if self._file_path:
            provider = QFileIconProvider()
            q_file_info = QFileInfo(self._file_path)
            icon = provider.icon(q_file_info)
            self.icon_label.setPixmap(icon.pixmap(self.icon_label.size()))

        self.name_label = QLabel(self._name)
        self.name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.name_label.setToolTip(self._name)

        self._layout.addWidget(self.icon_label)
        self._layout.addWidget(self.name_label)

    def _build_remove_button(self):
        self.remove_button = QPushButton(self)
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

    def preview_pixmap(self):
        return self._preview_pixmap

    def attachment_name(self) -> str:
        return self._name

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.LeftButton
                and self._preview_pixmap is not None
                and self.rect().contains(event.position().toPoint())):
            self.preview_requested.emit(self)
        super().mouseReleaseEvent(event)

    def _remove_overlay_style(self) -> str:
        theme_name = ""
        try:
            theme_name = self.theme_manager.get_current_theme_name()
        except Exception:
            pass
        background = ("rgba(255, 255, 255, 210)" if theme_name == "light"
                      else "rgba(20, 20, 20, 180)")
        return (
            "QPushButton { background-color: " + background + "; border: none; "
            "border-radius: 8px; padding: 0px; }"
            "QPushButton:hover { background-color: rgba(210, 50, 50, 235); }"
        )

    def _apply_styles(self):
        """Applies styles from the theme manager."""
        if self._image_tile:
            self.setStyleSheet("AttachmentChip { background: transparent; }")
            self.remove_button.setStyleSheet(self._remove_overlay_style())
            return
        self.setStyleSheet(self.theme_manager.get_style("ATTACHMENT_CHIP_STYLE"))
        self.name_label.setStyleSheet(self.theme_manager.get_style("ATTACHMENT_CHIP_LABEL_STYLE"))
        self.remove_button.setStyleSheet(self.theme_manager.get_style("ATTACHMENT_CHIP_BUTTON_STYLE"))
