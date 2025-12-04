"""
Represents a single, editable action block within a TaskBlock.
"""
from PySide6 import QtCore, QtGui, QtWidgets
import qtawesome as qta

from .dialogs import ActionDialog

# Constants from the original file, can be moved to a central config
PADDING = 10
TITLE_DESC_SPACING = 6
TITLE_FONT = QtGui.QFont("Segoe UI", 11, QtGui.QFont.Bold)
DESC_FONT = QtGui.QFont("Segoe UI", 10)


class ActionBlock(QtWidgets.QGraphicsWidget):
    # Signal emitted when the user edits and saves the action data
    data_changed = QtCore.Signal(dict)
    # Signal to request deletion from the parent task
    delete_requested = QtCore.Signal(object)

    def __init__(self, action_data, block_width, parent=None):
        super().__init__(parent)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, False) # Drag/drop handled by parent
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

        self.action_data = action_data
        self.title = self.action_data.get("name", "Unknown")
        self.description = self.action_data.get("arguments", {}).get("description", "")

        self._block_width = block_width
        self._cached_height = -1
        self._close_rect = None

        # Icon setup
        try:
            self.close_icon = qta.icon('fa.times', color='#ffffff')
        except Exception:
            try:
                self.close_icon = qta.icon('fa5s.trash', color='#ffffff')
            except Exception:
                style = QtWidgets.QApplication.instance().style()
                if style:
                    self.close_icon = style.standardIcon(
                        QtWidgets.QStyle.StandardPixmap.SP_TabCloseButton
                    )
                else:
                    self.close_icon = None

    def mousePressEvent(self, event):
        if self._close_rect and self._close_rect.contains(event.pos()):
            self.delete_requested.emit(self)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent):
        arg_values = self.action_data.get("arguments", {})
        parent_view = self.scene().views()[0] if self.scene() and self.scene().views() else None

        dlg = ActionDialog(parent=parent_view.window(), selected_action_name=self.title, arg_values=arg_values)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            new_action_data = dlg.get_action()
            if new_action_data:
                self.action_data = new_action_data
                self.title = self.action_data.get("name", self.title)
                self.description = self.action_data.get("arguments", {}).get("description", self.description)

                # Invalidate cache and notify parent of the change
                self._cached_height = -1
                self.prepareGeometryChange()
                self.data_changed.emit(self.action_data)
        super().mouseDoubleClickEvent(event)

    def set_width(self, new_width):
        """Sets a new width for the block."""
        if self._block_width == new_width:
            return
        self._block_width = new_width
        self._cached_height = -1 # Invalidate height cache
        self.prepareGeometryChange()
        self.update()

    @staticmethod
    def _text_layout_height(text: str, font: QtGui.QFont, width: float) -> float:
        if not text or width <= 0:
            return 0

        layout = QtGui.QTextLayout(text, font)
        opt = QtGui.QTextOption(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        opt.setWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(opt)

        layout.beginLayout()
        height = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QtCore.QPointF(0, height))
            height += line.height()
        layout.endLayout()
        return height

    def calc_height_for_width(self, width: float) -> float:
        content_w = max(0.0, width - 2 * PADDING)
        title_h = self._text_layout_height(self.title, TITLE_FONT, content_w)
        desc_h = self._text_layout_height(self.description, DESC_FONT, content_w)
        total = PADDING + title_h + (TITLE_DESC_SPACING if self.description else 0) + desc_h + PADDING
        return total

    def boundingRect(self) -> QtCore.QRectF:
        if self._cached_height < 0:
            self._cached_height = self.calc_height_for_width(self._block_width)
        return QtCore.QRectF(0, 0, self._block_width, self._cached_height)

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        r = self.boundingRect()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        # Main block background
        painter.setBrush(QtGui.QColor(30, 30, 30))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(r, 8, 8)

        # Header bar
        header_rect = QtCore.QRectF(r.left()+6, r.top()+6, r.width()-12, 28)
        painter.setBrush(QtGui.QColor(55, 55, 55))
        painter.drawRoundedRect(header_rect, 12, 12)

        # Close icon
        if self.close_icon:
            icon_size = 16
            margin = 4
            x = header_rect.right() - icon_size - margin
            y = header_rect.top() + (header_rect.height() - icon_size) / 2
            padding = 1
            bg_rect = QtCore.QRectF(x - padding, y - padding, icon_size + 2 * padding, icon_size + 2 * padding)
            painter.setBrush(QtGui.QColor(255, 255, 255))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRoundedRect(bg_rect, 6, 6)
            self._close_rect = bg_rect

            icon_rect = QtCore.QRectF(x, y, icon_size, icon_size)
            try:
                red_icon = qta.icon('fa5s.times', color='#c83030')
            except Exception:
                red_icon = qta.icon('fa5s.trash', color='#c83030')
            red_icon.paint(painter, icon_rect.toRect())

        # Text content
        content_w = r.width() - 2 * PADDING
        title_h = self._text_layout_height(self.title, TITLE_FONT, content_w)
        title_rect = QtCore.QRectF(PADDING, PADDING, content_w, title_h)

        desc_top = title_rect.bottom() + (TITLE_DESC_SPACING if self.description else 0)
        desc_h = self._text_layout_height(self.description, DESC_FONT, content_w)
        desc_rect = QtCore.QRectF(PADDING, desc_top, content_w, desc_h)

        text_opt = QtGui.QTextOption(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        text_opt.setWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)

        painter.setFont(TITLE_FONT)
        painter.setPen(QtGui.QColor(220, 220, 220))
        painter.drawText(title_rect, self.title, text_opt)

        painter.setFont(DESC_FONT)
        painter.setPen(QtGui.QColor(235, 235, 235))
        painter.drawText(desc_rect, self.description, text_opt)

        # Selection border
        if option.state & QtWidgets.QStyle.State_Selected:
            pen = QtGui.QPen(QtGui.QColor(120, 170, 255), 1.5)
        else:
            pen = QtGui.QPen(QtGui.QColor(45, 45, 45), 1.0)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(pen)
        painter.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

    def get_data(self):
        return self.action_data
