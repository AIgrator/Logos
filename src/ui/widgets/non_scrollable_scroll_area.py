from PySide6.QtWidgets import QScrollArea
from PySide6.QtGui import QWheelEvent

class NonScrollableScrollArea(QScrollArea):
    """
    A QScrollArea subclass that completely ignores wheel events when the vertical
    scrollbar is not visible, preventing any "phantom" or "micro" scrolling.
    """
    def wheelEvent(self, event: QWheelEvent):
        if not self.verticalScrollBar().isVisible():
            event.ignore()
            return
        super().wheelEvent(event)
