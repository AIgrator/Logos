"""
Contains small, reusable QGraphicsWidget components for the plan editor,
such as editable text fields, insertion zones, and buttons.
"""
from PySide6 import QtCore, QtGui, QtWidgets

# Constants from the original file
INTER_BLOCK_GAP = 16
TITLE_FONT = QtGui.QFont("Segoe UI", 11, QtGui.QFont.Bold)
DESC_FONT = QtGui.QFont("Segoe UI", 10)


class ActionInsertZone(QtWidgets.QGraphicsRectItem):
    """A zone within a TaskBlock to insert a new action."""
    def __init__(self, width, task_block, insert_idx, parent=None):
        # Use a smaller gap for actions inside a task
        super().__init__(0, 0, width, INTER_BLOCK_GAP / 2, parent)
        self.setBrush(QtGui.QColor(60, 60, 80, 0)) # Transparent brush
        self.setPen(QtGui.QPen(QtCore.Qt.NoPen))
        self.setAcceptHoverEvents(True)
        self.task_block = task_block
        self.insert_idx = insert_idx
        self.setZValue(2)  # Ensure this is higher than ActionBlock's Z-value (1)
        self.button_proxy = None
        self.button = None

    def hoverEnterEvent(self, event):
        self.setBrush(QtGui.QColor(60, 60, 80, 80))
        if not self.button:
            btn = QtWidgets.QPushButton("+ Action")
            btn.setFixedSize(120, 26) # Match the size of the "+ Task" button
            btn.setStyleSheet("QPushButton{padding:2px;}") # Match the style
            proxy = QtWidgets.QGraphicsProxyWidget(self)
            proxy.setWidget(btn)
            w = self.rect().width()
            px = (w - btn.width()) / 2
            py = (self.rect().height() - btn.height()) / 2
            proxy.setPos(px, py)
            btn.clicked.connect(self._on_button_clicked)
            self.button = btn
            self.button_proxy = proxy
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setBrush(QtGui.QColor(60, 60, 80, 0)) # Transparent brush
        if self.button_proxy:
            try:
                scene = self.scene()
                if scene:
                    scene.removeItem(self.button_proxy)
            except Exception:
                pass
            self.button_proxy.setWidget(None)
            self.button_proxy = None
            self.button = None
        super().hoverLeaveEvent(event)

    def _on_button_clicked(self):
        # Use a timer to avoid issues with the dialog opening during the hover event
        if self.task_block:
            QtCore.QTimer.singleShot(0, lambda: self.task_block.add_new_action(at_index=self.insert_idx))


class EditableTextItem(QtWidgets.QGraphicsTextItem):
    """A QGraphicsTextItem that becomes editable on double-click."""
    editing_finished = QtCore.Signal(str)
    live_edited = QtCore.Signal()

    def __init__(self, text, parent=None, prefix="", font=None, color=None):
        super().__init__(parent)
        self.prefix = prefix
        self._editable_text = text
        self.setPlainText(self.prefix + self._editable_text)

        if font:
            self.setFont(font)
        if color:
            self.setDefaultTextColor(color)

        self.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
        self.document().contentsChanged.connect(self._on_doc_changed)

    def mouseDoubleClickEvent(self, event):
        if self.textInteractionFlags() == QtCore.Qt.NoTextInteraction:
            self.setPlainText(self._editable_text)
            self.setTextInteractionFlags(QtCore.Qt.TextEditorInteraction)
            self.setFocus(QtCore.Qt.MouseFocusReason)
            cursor = self.textCursor()
            cursor.movePosition(QtGui.QTextCursor.End)
            self.setTextCursor(cursor)
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event):
        self.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
        new_text = self.toPlainText()
        if self._editable_text != new_text:
            self._editable_text = new_text
            self.editing_finished.emit(new_text)
        self.setPlainText(self.prefix + self._editable_text)
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            if not (event.modifiers() & QtCore.Qt.ShiftModifier):
                self.clearFocus()
                return
        elif event.key() == QtCore.Qt.Key_Escape:
            self.setPlainText(self.prefix + self._editable_text)
            self.clearFocus()
            return
        super().keyPressEvent(event)

    def _on_doc_changed(self):
        self.prepareGeometryChange()
        self.live_edited.emit()


class CloseButtonItem(QtWidgets.QGraphicsRectItem):
    """
    Invisible clickable overlay for the TaskBlock close (delete) icon.
    """
    def __init__(self, parent_task_block):
        super().__init__(parent_task_block)
        self.parent_task_block = parent_task_block
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(QtCore.Qt.LeftButton)
        self.setPen(QtCore.Qt.NoPen)
        self.setBrush(QtCore.Qt.transparent)
        self.setZValue(1000)

    def hoverEnterEvent(self, event):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.PointingHandCursor)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        QtWidgets.QApplication.restoreOverrideCursor()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            scene = self.parent_task_block.scene()
            if scene and hasattr(scene, 'remove_task'):
                scene.remove_task(self.parent_task_block)
                event.accept()
                return
        super().mousePressEvent(event)


class InsertZone(QtWidgets.QGraphicsRectItem):
    """A zone between TaskBlocks to insert a new task."""
    def __init__(self, width, add_cb, idx, parent=None):
        super().__init__(0, 0, width, INTER_BLOCK_GAP, parent)
        self.setBrush(QtGui.QColor(60, 60, 80, 0))
        self.setPen(QtGui.QPen(QtCore.Qt.NoPen))
        self.setAcceptHoverEvents(True)
        self.callback_add = add_cb
        self.setZValue(0)
        self.button_proxy = None
        self.button = None
        self.idx = idx

    def hoverEnterEvent(self, event):
        self.setBrush(QtGui.QColor(60, 60, 80, 80))
        if not self.button:
            btn = QtWidgets.QPushButton("+ Task")
            btn.setFixedSize(120, 26)
            btn.setStyleSheet("QPushButton{padding:2px;}")
            proxy = QtWidgets.QGraphicsProxyWidget(self)
            proxy.setWidget(btn)
            w = self.rect().width()
            px = (w - btn.width()) / 2
            py = (self.rect().height() - btn.height()) / 2
            proxy.setPos(px, py)
            btn.clicked.connect(self._on_button_clicked)
            self.button = btn
            self.button_proxy = proxy
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setBrush(QtGui.QColor(60, 60, 80, 0))
        if self.button_proxy:
            try:
                scene = self.scene()
                if scene:
                    scene.removeItem(self.button_proxy)
            except Exception:
                pass
            self.button_proxy.setWidget(None)
            self.button_proxy = None
            self.button = None
        super().hoverLeaveEvent(event)

    def _on_button_clicked(self):
        if self.callback_add:
            parent = self.scene().views()[0].window() if self.scene() and self.scene().views() else None
            self.callback_add(idx=self.idx, parent=parent)
