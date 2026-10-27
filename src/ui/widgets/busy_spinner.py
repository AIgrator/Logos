"""Indeterminate busy indicator: a rotating arc, no third-party assets."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QPalette, QPen
from PySide6.QtWidgets import QWidget


class BusySpinner(QWidget):
    """Theme-accent rotating arc shown while content loads."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._running = False
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._advance)
        self.setFixedSize(48, 48)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def start(self):
        self._running = True
        if self.isVisible():
            self._timer.start()

    def stop(self):
        self._running = False
        self._timer.stop()

    def showEvent(self, event):
        if self._running:
            self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _advance(self):
        self._angle = (self._angle + 8) % 360
        self.update()

    def paintEvent(self, event):
        side = min(self.width(), self.height())
        if side <= 4:
            return
        pen_width = max(2.0, side / 12.0)
        inset = pen_width / 2.0 + 1.0
        rect = self.rect().toRectF().adjusted(inset, inset, -inset, -inset)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(self.palette().color(QPalette.ColorRole.Highlight), pen_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, -self._angle * 16, 270 * 16)
        painter.end()
