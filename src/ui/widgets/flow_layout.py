"""
This module provides a FlowLayout class for arranging widgets with wrapping.
"""
from PySide6.QtCore import Qt, QPoint, QRect, QSize
from PySide6.QtWidgets import QLayout

class FlowLayout(QLayout):
    """
    A layout that arranges items horizontally and wraps them to a new line
    when the available width is exhausted.
    """
    def __init__(self, parent=None, margin=0, spacing=-1):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._item_list = []

    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item):
        self._item_list.append(item)

    def count(self):
        return len(self._item_list)

    def itemAt(self, index):
        if 0 <= index < len(self._item_list):
            return self._item_list[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._item_list):
            return self._item_list.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        height = self._do_layout(QRect(0, 0, width, 0), True)
        return height

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        # The minimum size is the height for a very narrow width, which forces all items to wrap.
        # A width of 1 is a good approximation.
        height = self.heightForWidth(1)

        # The width can be the size of the largest item.
        max_width = 0
        for item in self._item_list:
            max_width = max(max_width, item.minimumSize().width())

        top_margin, _, bottom_margin, _ = self.getContentsMargins()

        return QSize(max_width, height + top_margin + bottom_margin)

    def _do_layout(self, rect, test_only):
        left_margin, top_margin, _, _ = self.getContentsMargins()
        x = rect.x() + left_margin
        y = rect.y() + top_margin
        line_height = 0
        spacing = self.spacing()

        for item in self._item_list:
            next_x = x + item.sizeHint().width() + spacing
            if next_x - spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + spacing
                next_x = x + item.sizeHint().width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = next_x
            line_height = max(line_height, item.sizeHint().height())
        return y + line_height - rect.y()
