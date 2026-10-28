"""In-app preview for image attachments."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout


class ImagePreviewDialog(QDialog):
    """Shows an attachment scaled to fit a large part of the screen."""

    def __init__(self, pixmap: QPixmap, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet("QDialog { background-color: #1e1e1e; }")

        screen = parent.screen() if parent is not None else QGuiApplication.primaryScreen()
        max_w, max_h = 1200, 800
        if screen is not None:
            available = screen.availableGeometry()
            max_w = max(320, int(available.width() * 0.8))
            max_h = max(240, int(available.height() * 0.8))

        scaled = pixmap
        if pixmap.width() > max_w or pixmap.height() > max_h:
            scaled = pixmap.scaled(
                max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        label.setPixmap(scaled)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label)
        self.resize(scaled.width(), scaled.height())
