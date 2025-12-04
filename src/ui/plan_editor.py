"""
Block-based Plan Editor adapted from tests/bl.py.

This editor provides a visual block list of steps with add/edit/delete/reorder
capabilities. On Save it returns a list of action dicts. If the last step is
not 'finish', the dialog will append a 'finish' action automatically to avoid
infinite execution loops.
"""
import json
import os
from copy import deepcopy

from PySide6 import QtCore, QtGui, QtWidgets
import qtawesome as qta

from . import styles
from .widgets.hotkey.hotkey_edit import HotkeyEdit
from .widgets.hotkey.single_key_edit import SingleKeyEdit


# Global store for actions, loaded once.
_ACTIONS = []

def _load_actions():
    """Loads action schemas from the cache file."""
    global _ACTIONS
    if _ACTIONS:
        return _ACTIONS

    # Determine cache path relative to this file's location
    try:
        # __file__ -> src/ui/plan_editor.py, so cache is ../../cache
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cache_file = os.path.join(base_dir, 'cache', 'vision', 'ui_tools.json')

        if os.path.exists(cache_file):
            with open(cache_file, encoding='utf-8') as f:
                _ACTIONS = json.load(f)
        else:
            # Fallback to an empty list if the cache file doesn't exist
            _ACTIONS = []
    except (OSError, json.JSONDecodeError):
        # In case of error, ensure actions is an empty list
        _ACTIONS = []
    return _ACTIONS


def _action_schema_by_name(name: str):
    actions = _load_actions()
    for a in actions:
        if a["name"] == name:
            return deepcopy(a)
    return {"name": name, "description": "", "arguments": {}}


class ActionDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, selected_action_name=None, arg_values=None):
        super().__init__(parent)
        self.setWindowTitle("Add / Edit Action")
        self.setMinimumWidth(520)
        self.selected_action = None
        self.arg_widgets = {}
        self.arg_values = arg_values or {}
        self.actions = _load_actions()

        main = QtWidgets.QHBoxLayout(self)

        # list of actions
        self.list_widget = QtWidgets.QListWidget()
        for a in self.actions:
            item = QtWidgets.QListWidgetItem(a["name"])
            item.setToolTip(a.get("description", ""))
            self.list_widget.addItem(item)
        self.list_widget.setMaximumWidth(180)
        main.addWidget(self.list_widget)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        self.lbl_desc = QtWidgets.QLabel("Select an action")
        self.lbl_desc.setWordWrap(True)
        self.lbl_desc.setMinimumHeight(40)
        rv.addWidget(self.lbl_desc)

        self.form_widget = QtWidgets.QWidget()
        self.form_layout = QtWidgets.QFormLayout(self.form_widget)
        rv.addWidget(self.form_widget)

        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        rv.addWidget(self.buttons)
        main.addWidget(right)

        self.list_widget.currentItemChanged.connect(self.on_action_selected)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        if self.list_widget.count() > 0:
            row = 0
            if selected_action_name:
                for i in range(self.list_widget.count()):
                    if self.list_widget.item(i).text() == selected_action_name:
                        row = i
                        break
                self.list_widget.setEnabled(False)
            self.list_widget.setCurrentRow(row)
            self.on_action_selected(self.list_widget.currentItem())

    def on_action_selected(self, current, previous=None):
        if not current:
            return
        name = current.text()
        act = next((x for x in self.actions if x["name"] == name), None)
        self.selected_action = act
        self.lbl_desc.setText(act.get("description", ""))

        # clear form
        while self.form_layout.rowCount():
            litem = self.form_layout.itemAt(0, QtWidgets.QFormLayout.LabelRole)
            fitem = self.form_layout.itemAt(0, QtWidgets.QFormLayout.FieldRole)
            for it in (litem, fitem):
                if it is not None:
                    w = it.widget()
                    if w is not None:
                        w.setParent(None)
                        w.deleteLater()
            self.form_layout.removeRow(0)
        for child in self.form_widget.findChildren(QtWidgets.QWidget, options=QtCore.Qt.FindDirectChildrenOnly):
            if child is not self.form_widget:
                child.setParent(None)
                child.deleteLater()
        self.arg_widgets.clear()

        for arg_name, hint in act.get("arguments", {}).items():
            widget = None
            if arg_name in ("x1", "x2", "y1", "y2", "timeout", "amount"):
                spin = QtWidgets.QSpinBox()
                spin.setRange(-1000000, 1000000)
                spin.setValue(0)
                widget = spin
            elif arg_name == "button":
                combo = QtWidgets.QComboBox()
                combo.addItems(["left", "middle", "right"])
                widget = combo
            elif arg_name == "direction":
                combo = QtWidgets.QComboBox()
                combo.addItems(["up", "down"])
                widget = combo
            elif arg_name == "key":
                widget = SingleKeyEdit()
            elif arg_name == "keys":
                widget = HotkeyEdit()
            elif arg_name == "description":
                te = QtWidgets.QTextEdit()
                te.setFixedHeight(90)
                te.setPlaceholderText(str(hint) if hint is not None else "")
                widget = te
            else:
                le = QtWidgets.QLineEdit()
                le.setPlaceholderText(str(hint) if hint is not None else "")
                widget = le
            label = QtWidgets.QLabel(arg_name)
            self.form_layout.addRow(label, widget)
            self.arg_widgets[arg_name] = widget

        for name, widget in self.arg_widgets.items():
            val = self.arg_values.get(name, None)
            if val is not None:
                if isinstance(widget, QtWidgets.QSpinBox):
                    widget.setValue(int(val))
                elif isinstance(widget, QtWidgets.QComboBox):
                    ix = widget.findText(str(val))
                    if ix >= 0:
                        widget.setCurrentIndex(ix)
                elif isinstance(widget, QtWidgets.QTextEdit):
                    widget.setPlainText(str(val))
                elif isinstance(widget, SingleKeyEdit | HotkeyEdit):
                    widget.set_key_sequence(val)
                elif isinstance(widget, QtWidgets.QLineEdit):
                    if isinstance(val, list):
                        widget.setText(",".join(str(x) for x in val))
                    else:
                        widget.setText(str(val))

    def get_action(self):
        if not self.selected_action:
            return None
        args = {}
        for name, widget in self.arg_widgets.items():
            if isinstance(widget, QtWidgets.QSpinBox):
                args[name] = int(widget.value())
            elif isinstance(widget, QtWidgets.QComboBox):
                args[name] = widget.currentText()
            elif isinstance(widget, QtWidgets.QTextEdit):
                args[name] = widget.toPlainText()
            elif isinstance(widget, SingleKeyEdit):
                args[name] = widget.get_key_sequence()
            elif isinstance(widget, HotkeyEdit):
                args[name] = widget.get_key_sequence()
            elif isinstance(widget, QtWidgets.QLineEdit):
                text = widget.text()
                if name == "keys":
                    keys = [k.strip() for k in text.split(",") if k.strip()]
                    args[name] = keys
                else:
                    args[name] = text
            else:
                args[name] = None
        return {"name": self.selected_action["name"], "arguments": args}


BLOCK_WIDTH = 420
PADDING = 10
TITLE_DESC_SPACING = 6
INTER_BLOCK_GAP = 16
TITLE_FONT = QtGui.QFont("Segoe UI", 11, QtGui.QFont.Bold)
DESC_FONT = QtGui.QFont("Segoe UI", 10)



class ActionBlock(QtWidgets.QGraphicsWidget):
    def __init__(self, title, description, parent=None):
        super().__init__(parent)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, False)
        self.setZValue(1)
        self.title = title
        self.description = description
        self._cached_width = -1
        self._cached_height = -1
        self.setAcceptHoverEvents(True)
        style = QtWidgets.QApplication.instance().style() if QtWidgets.QApplication.instance() else None
        # Prefer a FontAwesome v4 'times' icon, fallback to FA5 trash, then to system icon
        try:
            self.close_icon = qta.icon('fa.times', color='#ffffff')
        except Exception:
            try:
                self.close_icon = qta.icon('fa5s.trash', color='#ffffff')
            except Exception:
                if style:
                    self.close_icon = style.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_TabCloseButton)
                else:
                    self.close_icon = None
        self._close_rect = None

    def mousePressEvent(self, event):
        if self._close_rect and self._close_rect.contains(event.pos()):
            if self.parentItem() and isinstance(self.parentItem(), TaskBlock):
                self.parentItem().remove_action(self)
            return

        if event.button() == QtCore.Qt.LeftButton:
            self._drag_start_pos = event.pos()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() != QtCore.Qt.LeftButton:
            return super().mouseMoveEvent(event)

        if (
            event.screenPos() - event.buttonDownScreenPos(QtCore.Qt.LeftButton)
        ).manhattanLength() < QtWidgets.QApplication.startDragDistance():
            return super().mouseMoveEvent(event)

        drag = QtGui.QDrag(event.widget())
        mime = QtCore.QMimeData()
        # We need a way to identify this specific block. Let's use its index.
        parent_task = self.parentItem()
        if isinstance(parent_task, TaskBlock):
            try:
                idx = parent_task.action_blocks.index(self)
                mime.setText(f"action-drag:{idx}")
                drag.setMimeData(mime)
                drag.exec_(QtCore.Qt.MoveAction)
            except ValueError:
                pass

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self, '_dragging', False):
            self.setZValue(1)
            scene = self.scene()
            if isinstance(scene, BlockScene):
                new_y = self.pos().y() + self.boundingRect().height() / 2
                idxs = [b for b in scene.blocks if b is not self]
                insert_idx = 0
                for i, b in enumerate(idxs):
                    if new_y > b.pos().y() + b.boundingRect().height() / 2:
                        insert_idx = i + 1
                old_idx = None
                for i, b in enumerate(scene.blocks):
                    if b is self:
                        old_idx = i
                        break
                if old_idx is not None and old_idx != insert_idx:
                    block = scene.blocks.pop(old_idx)
                    scene.blocks.insert(insert_idx, block)
                    step = scene._steps.pop(old_idx)
                    scene._steps.insert(insert_idx, step)
                scene.relayout()
            self._dragging = False
            self._drag_start_pos = None
            return
        super().mouseReleaseEvent(event)

    @staticmethod
    def _text_layout_height(text: str, font: QtGui.QFont, width: float) -> float:
        if width <= 0:
            return 0
        layout = QtGui.QTextLayout(text, font)
        opt = QtGui.QTextOption()
        opt.setWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
        opt.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
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

    def sizeHint(self, which, constraint=QtCore.QSizeF()):
        if which in (QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Maximum):
            h = self.calc_height_for_width(BLOCK_WIDTH)
            return QtCore.QSizeF(BLOCK_WIDTH, h)
        return super().sizeHint(which, constraint)

    def setGeometry(self, rect: QtCore.QRectF):
        super().setGeometry(rect)
        self._cached_width = rect.width()
        self._cached_height = self.calc_height_for_width(rect.width())

    def boundingRect(self) -> QtCore.QRectF:
        if self._cached_width < 0:
            self._cached_width = BLOCK_WIDTH
            self._cached_height = self.calc_height_for_width(BLOCK_WIDTH)
        rect = QtCore.QRectF(0, 0, self._cached_width, self._cached_height)
        return rect

    def _rects_for_paint(self, rect: QtCore.QRectF):
        content_left = rect.left() + PADDING
        content_top = rect.top() + PADDING
        content_w = rect.width() - 2 * PADDING
        title_h = self._text_layout_height(self.title, TITLE_FONT, content_w)
        title_rect = QtCore.QRectF(content_left, content_top, content_w, title_h)
        desc_top = title_rect.bottom() + (TITLE_DESC_SPACING if self.description else 0)
        desc_h = self._text_layout_height(self.description, DESC_FONT, content_w)
        desc_rect = QtCore.QRectF(content_left, desc_top, content_w, desc_h)
        return title_rect, desc_rect

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        r = self.boundingRect()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setBrush(QtGui.QColor(30, 30, 30))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(r, 8, 8)
        header_rect = QtCore.QRectF(r.left()+6, r.top()+6, r.width()-12, 28)
        painter.setBrush(QtGui.QColor(55, 55, 55))
        painter.drawRoundedRect(header_rect, 12, 12)
        if self.close_icon:
            # Icon size 16px with a 2px white frame around it so the trash feels embedded
            icon_size = 16
            margin = 4
            x = header_rect.right() - icon_size - margin
            y = header_rect.top() + (header_rect.height() - icon_size) / 2
            padding = 1  # reduced to 1px frame (half the previous padding)
            bg_rect = QtCore.QRectF(x - padding, y - padding, icon_size + 2 * padding, icon_size + 2 * padding)
            painter.setBrush(QtGui.QColor(255, 255, 255))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRoundedRect(bg_rect, 6, 6)
            # store clickable rect (use white background)
            self._close_rect = bg_rect
            icon_rect = QtCore.QRectF(x, y, icon_size, icon_size)
            # draw the trash icon in a saturated red to stand out against white
            try:
                try:
                    red_icon = qta.icon('fa5s.times', color='#c83030')
                except Exception:
                    red_icon = qta.icon('fa5s.trash', color='#c83030')
                red_icon.paint(painter, icon_rect.toRect())
            except Exception:
                try:
                    pm = self.close_icon.pixmap(icon_size, icon_size)
                    painter.drawPixmap(icon_rect.topLeft(), pm)
                except Exception:
                    pass
        title_rect, desc_rect = self._rects_for_paint(r)
        opt = QtGui.QTextOption()
        opt.setWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
        opt.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        # Draw text only if proxies are not used
        if not getattr(self, 'desc_proxy', None):
            painter.setFont(TITLE_FONT)
            painter.setPen(QtGui.QColor(220, 220, 220))
            painter.drawText(title_rect, self.title, opt)
        if not getattr(self, 'out_proxy', None):
            painter.setFont(DESC_FONT)
            painter.setPen(QtGui.QColor(235, 235, 235))
            painter.drawText(desc_rect, self.description, opt)
        if option.state & QtWidgets.QStyle.State_Selected:
            pen = QtGui.QPen(QtGui.QColor(120, 170, 255))
            pen.setWidthF(1.5)
        else:
            pen = QtGui.QPen(QtGui.QColor(45, 45, 45))
            pen.setWidthF(1.0)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(pen)
        painter.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


    def mouseDoubleClickEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent):
        arg_values = self.get_data().get("arguments", {})
        parent = None
        scene = self.scene()
        if scene:
            views = scene.views() if hasattr(scene, 'views') else []
            if views:
                parent = views[0].window()
        dlg = ActionDialog(parent=parent, selected_action_name=self.title, arg_values=arg_values)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            action = dlg.get_action()
            if action:
                self.title = action.get("name", self.title)
                self.description = action.get("arguments", {}).get("description", self.description)
                self._cached_height = -1
                self.prepareGeometryChange()

                # Find the parent TaskBlock and ask it to update its layout
                if self.parentItem() and isinstance(self.parentItem(), TaskBlock):
                    self.parentItem().update_action_data(self, action)
                    self.parentItem().relayout_actions()
            return
        super().mouseDoubleClickEvent(event)

    def get_data(self):
        scene = self.scene()
        if isinstance(scene, BlockScene):
            for i, b in enumerate(scene.blocks):
                if b is self:
                    args = scene._steps[i].get("arguments", {})
                    return {"name": self.title, "arguments": dict(args)}
        return {"name": self.title, "arguments": {"description": self.description}}


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
        self.setZValue(0)
        self.button_proxy = None
        self.button = None

    def hoverEnterEvent(self, event):
        self.setBrush(QtGui.QColor(60, 60, 80, 80))
        if not self.button:
            btn = QtWidgets.QPushButton("+ Action")
            btn.setFixedSize(120, 22) # Smaller button
            btn.setStyleSheet("QPushButton{padding:1px; font-size: 9pt;}")
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
            try:
                self.button_proxy.widget().setParent(None)
            except Exception:
                pass
            try:
                self.button_proxy.deleteLater()
            except Exception:
                pass
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

        # Emit liveEdited on any document content change (for dynamic resize)
        try:
            self.document().contentsChanged.connect(self._on_doc_changed)
        except Exception:
            pass

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
        if event.key() == QtCore.Qt.Key_Return or event.key() == QtCore.Qt.Key_Enter:
            if not (event.modifiers() & QtCore.Qt.ShiftModifier):
                self.clearFocus()
                return
        elif event.key() == QtCore.Qt.Key_Escape:
            self.setPlainText(self.prefix + self._editable_text)
            self.clearFocus()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

    def _on_doc_changed(self):
        # Prepare for geometry change so boundingRect updates are seen immediately
        try:
            self.prepareGeometryChange()
        except Exception:
            pass
        # Emit liveEdited so parent relayouts during typing
        try:
            self.live_edited.emit()
        except Exception:
            pass


class CloseButtonItem(QtWidgets.QGraphicsRectItem):
    """
    Invisible clickable overlay for the TaskBlock close (delete) icon.
    Ensures clicks are captured regardless of child items overlapping.
    """
    def __init__(self, parent_task_block):
        super().__init__(parent_task_block)
        self.parent_task_block = parent_task_block
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(QtCore.Qt.LeftButton)
        self.setPen(QtCore.Qt.NoPen)
        self.setBrush(QtCore.Qt.transparent)
        self.setZValue(1000)  # ensure above header text

    def hoverEnterEvent(self, event):
        try:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.PointingHandCursor)
        except Exception:
            pass
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        try:
            QtWidgets.QApplication.restoreOverrideCursor()
        except Exception:
            pass
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            scene = self.parent_task_block.scene()
            if scene and isinstance(scene, BlockScene):
                scene.remove_task(self.parent_task_block)
                event.accept()
                return
        super().mousePressEvent(event)


class TaskBlock(QtWidgets.QGraphicsWidget):
    def __init__(self, task_data, parent=None):
        super().__init__(parent)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, True)
        # Ensure parent can filter child events (so clicks on child text reach parent)
        try:
            self.setFiltersChildEvents(True)
        except Exception:
            pass
        # Гарантируем, что сам TaskBlock принимает клики левой кнопкой
        try:
            self.setAcceptedMouseButtons(QtCore.Qt.LeftButton)
        except Exception:
            pass
        self.task_data = task_data
        self.action_blocks = []
        self.action_insert_zones = []
        self._cached_height = -1
        self._close_rect = None
        self._content_width = BLOCK_WIDTH - 35.0  # Store for width maintenance (reduced for scrollbar)
        style = QtWidgets.QApplication.instance().style() if QtWidgets.QApplication.instance() else None
        try:
            self.close_icon = qta.icon('fa.times', color='#ffffff')
        except Exception:
            try:
                self.close_icon = qta.icon('fa5s.trash', color='#ffffff')
            except Exception:
                if style:
                    self.close_icon = style.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_TabCloseButton)
                else:
                    self.close_icon = None

        # Create editable text items for header and footer
        self.description_text = EditableTextItem(
            text=self.task_data.get('task_description', 'No description'),
            prefix="Task: ",
            font=TITLE_FONT,
            color=QtGui.QColor(230, 230, 230),
            parent=self
        )
        self.outcome_text = EditableTextItem(
            text=self.task_data.get('expected_outcome', 'No outcome specified'),
            prefix="✅ Outcome: ",
            font=DESC_FONT,
            color=QtGui.QColor(200, 200, 200),
            parent=self
        )

        # Remove internal document margins so the text spans expected width
        try:
            self.description_text.document().setDocumentMargin(0)
        except Exception:
            pass
        try:
            self.outcome_text.document().setDocumentMargin(0)
        except Exception:
            pass

        # Ensure wrap mode uses full available width (no unexpected narrow layout)
        try:
            opt = QtGui.QTextOption()
            opt.setWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
            opt.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
            self.description_text.document().setDefaultTextOption(opt)
            self.outcome_text.document().setDefaultTextOption(opt)
        except Exception:
            pass

        self.description_text.editing_finished.connect(self._on_description_changed)
        self.outcome_text.editing_finished.connect(self._on_outcome_changed)
        # Live changes while typing (auto-resize header/footer)
        try:
            self.description_text.live_edited.connect(self._on_live_edited)
            self.outcome_text.live_edited.connect(self._on_live_edited)
        except Exception:
            pass

        # Replace header/outcome with QTextEdit via QGraphicsProxyWidget for full-width and auto-height
        try:
            # Header QTextEdit
            self.desc_edit = QtWidgets.QTextEdit()
            self.desc_edit.setObjectName("header_text_edit")
            self.desc_edit.setFrameStyle(QtWidgets.QFrame.NoFrame)
            self.desc_edit.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.desc_edit.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.desc_edit.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
            self.desc_edit.setFont(TITLE_FONT)
            self.desc_edit.setStyleSheet("QTextEdit { background: transparent; color: rgb(230,230,230); }")
            self.desc_edit.setAcceptRichText(False)
            # ensure horizontal expansion preference
            try:
                self.desc_edit.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            except Exception:
                pass
            self.desc_edit.setPlainText(self.task_data.get('task_description', 'No description'))
            self.desc_proxy = QtWidgets.QGraphicsProxyWidget(self)
            self.desc_proxy.setWidget(self.desc_edit)
            try:
                self.desc_proxy.setWidgetResizable(True)
                # Force the proxy to expand horizontally
                self.desc_proxy.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            except Exception:
                pass
            # Outcome QTextEdit
            self.out_edit = QtWidgets.QTextEdit()
            self.out_edit.setObjectName("outcome_text_edit")
            self.out_edit.setFrameStyle(QtWidgets.QFrame.NoFrame)
            self.out_edit.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.out_edit.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.out_edit.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
            self.out_edit.setFont(DESC_FONT)
            self.out_edit.setStyleSheet("QTextEdit { background: transparent; color: rgb(200,200,200); }")
            self.out_edit.setAcceptRichText(False)
            try:
                self.out_edit.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            except Exception:
                pass
            self.out_edit.setPlainText(self.task_data.get('expected_outcome', 'No outcome specified'))
            self.out_proxy = QtWidgets.QGraphicsProxyWidget(self)
            self.out_proxy.setWidget(self.out_edit)
            try:
                self.out_proxy.setWidgetResizable(True)
                # Force the proxy to expand horizontally
                self.out_proxy.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            except Exception:
                pass

            # Hide legacy QGraphicsTextItem so it doesn't interfere
            try:
                self.description_text.setVisible(False)
                self.outcome_text.setVisible(False)
            except Exception:
                pass

            # Connect to update data and relayout on each change
            self.desc_edit.textChanged.connect(self._on_desc_text_changed)
            self.out_edit.textChanged.connect(self._on_out_text_changed)

            # Ensure width is maintained during editing
            self.desc_edit.focusInEvent = self._make_focus_handler(self.desc_edit, self._content_width)
            self.out_edit.focusInEvent = self._make_focus_handler(self.out_edit, self._content_width)

            # Also prevent resizing during text changes
            self.desc_edit.textChanged.connect(lambda: self._maintain_width(self.desc_edit, self._content_width))
            self.out_edit.textChanged.connect(lambda: self._maintain_width(self.out_edit, self._content_width))

            # Enable QTextEdit proxy widgets for better width control
            self.use_proxies = True
            try:
                if self.desc_proxy:
                    self.desc_proxy.setVisible(True)
                if self.out_proxy:
                    self.out_proxy.setVisible(True)
            except Exception:
                pass
            try:
                self.description_text.setVisible(False)
                self.outcome_text.setVisible(False)
            except Exception:
                pass
        except Exception:
            # Fallback: ignore if cannot create proxies
            self.desc_edit = None
            self.desc_proxy = None
            self.out_edit = None
            self.out_proxy = None

        # Добавляем невидимый оверлей над иконкой удаления, чтобы гарантировать клик
        try:
            self._close_overlay = CloseButtonItem(self)
        except Exception:
            self._close_overlay = None

        # Create child ActionBlocks from the task data
        for action_data in self.task_data.get("actions", []):
            title = action_data.get("name", "")
            desc = action_data.get("arguments", {}).get("description", "")
            action_block = ActionBlock(title, desc, parent=self)
            self.action_blocks.append(action_block)

        self.setAcceptDrops(True)
        self.relayout_actions()

    def mousePressEvent(self, event):
        # Prefer cached rect if available; fallback to computed. Use item-local coords.
        if event.button() == QtCore.Qt.LeftButton:
            rect = self._close_rect or self._compute_close_rect()
            pos = event.pos()
            if rect and rect.contains(pos):
                if self.scene() and isinstance(self.scene(), BlockScene):
                    self.scene().remove_task(self)
                event.accept()
                return
        super().mousePressEvent(event)

    def _on_description_changed(self, new_text):
        self.task_data['task_description'] = new_text
        self.relayout_actions()

    def _on_outcome_changed(self, new_text):
        self.task_data['expected_outcome'] = new_text
        self.relayout_actions()

    def _on_live_edited(self):
        # Live relayout while typing in header/footer text items
        self.relayout_actions()

    def _on_desc_text_changed(self):
        try:
            self.task_data['task_description'] = self.desc_edit.toPlainText()
        except Exception:
            pass
        self._on_live_edited()

    def _on_out_text_changed(self):
        try:
            self.task_data['expected_outcome'] = self.out_edit.toPlainText()
        except Exception:
            pass
        self._on_live_edited()

    def _make_focus_handler(self, text_edit, content_width):
        """Create a focus handler that maintains QTextEdit width during editing."""
        def focus_handler(event):
            # Ensure the widget maintains its fixed width when focused
            try:
                text_edit.setFixedWidth(int(content_width))
                text_edit.setMinimumWidth(int(content_width))
                text_edit.setMaximumWidth(int(content_width))

                # Also ensure document width is set
                text_edit.document().setTextWidth(content_width)

                # Prevent the widget from resizing itself
                text_edit.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)

            except Exception:
                pass

            # Call the original focus handler
            return QtWidgets.QTextEdit.focusInEvent(text_edit, event)

        return focus_handler

    def _maintain_width(self, text_edit, content_width):
        """Ensure QTextEdit maintains its width during text changes."""
        try:
            text_edit.setFixedWidth(int(content_width))
            text_edit.setMinimumWidth(int(content_width))
            text_edit.setMaximumWidth(int(content_width))
            text_edit.document().setTextWidth(content_width)
        except Exception:
            pass

    def add_new_action(self, at_index=None):
        parent_widget = self.scene().views()[0] if self.scene() and self.scene().views() else None
        dlg = ActionDialog(parent=parent_widget)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            action_data = dlg.get_action()
            if action_data:
                actions_list = self.task_data.setdefault("actions", [])
                if at_index is None or at_index > len(actions_list):
                    actions_list.append(action_data)
                else:
                    actions_list.insert(at_index, action_data)

                # A full rebuild of action blocks is simpler and safer
                for block in self.action_blocks:
                    block.setParentItem(None)
                    if self.scene():
                        self.scene().removeItem(block)
                self.action_blocks.clear()

                for new_action_data in self.task_data.get("actions", []):
                    title = new_action_data.get("name", "")
                    desc = new_action_data.get("arguments", {}).get("description", "")
                    action_block = ActionBlock(title, desc, parent=self)
                    self.action_blocks.append(action_block)

                self.relayout_actions()

    def remove_action(self, action_block_to_remove):
        try:
            idx = self.action_blocks.index(action_block_to_remove)
            del self.task_data['actions'][idx]
            self.action_blocks.pop(idx)
            action_block_to_remove.setParentItem(None)
            if self.scene():
                self.scene().removeItem(action_block_to_remove)
            self.relayout_actions()
        except (ValueError, IndexError) as e:
            print(f"Error removing action: {e}")

    def relayout_actions(self):
        """Positions all child items and recalculates the total height."""
        # Reduce content width by 15px to eliminate horizontal scrollbar
        content_width = BLOCK_WIDTH - 35.0

        # CRITICAL FIX: Always limit EditableTextItem width to prevent scene overflow
        try:
            self.description_text.setTextWidth(content_width)
            self.outcome_text.setTextWidth(content_width)
        except Exception:
            pass


        # --- Header ---
        # Prefer QTextEdit proxy if available for full width + auto height
        if (
            getattr(self, 'use_proxies', False)
            and getattr(self, 'desc_edit', None)
            and getattr(self, 'desc_proxy', None)
        ):
            try:
                self.desc_edit.document().setTextWidth(content_width)
                self.desc_edit.document().adjustSize()
                header_doc_h = float(self.desc_edit.document().size().height())
            except Exception:
                header_doc_h = float(self.desc_edit.viewport().size().height())
            header_height = header_doc_h + PADDING
            # set explicit proxy geometry to prevent width collapse on first edit
            try:
                self.desc_proxy.setGeometry(QtCore.QRectF(10, PADDING / 2, content_width, header_doc_h))
                # clamp proxy/widget width so it cannot negotiate smaller width during editing
                try:
                    self.desc_proxy.setMinimumSize(QtCore.QSizeF(content_width, header_doc_h))
                    self.desc_proxy.setMaximumSize(QtCore.QSizeF(content_width, header_doc_h))
                    self.desc_proxy.setPreferredSize(QtCore.QSizeF(content_width, header_doc_h))
                except Exception:
                    pass
                try:
                    self.desc_edit.setMinimumWidth(int(content_width))
                    self.desc_edit.setMaximumWidth(int(content_width))
                    self.desc_edit.setFixedWidth(int(content_width))
                    self.desc_edit.setFixedHeight(int(header_doc_h))
                    # Ensure the widget resizes properly
                    self.desc_edit.updateGeometry()
                except Exception:
                    pass
            except Exception:
                self.desc_proxy.setPos(10, PADDING / 2)
                try:
                    self.desc_edit.setFixedWidth(int(content_width))
                    self.desc_edit.setFixedHeight(int(header_doc_h))
                    self.desc_edit.updateGeometry()
                except Exception:
                    pass
        else:
            # Fallback to QGraphicsTextItem
            self.description_text.setTextWidth(content_width)
            try:
                doc = self.description_text.document()
                doc.setTextWidth(content_width)
                doc.adjustSize()
                header_doc_h = float(doc.size().height())
            except Exception:
                header_doc_h = float(self.description_text.boundingRect().height())
            header_height = header_doc_h + PADDING
            self.description_text.setPos(10, PADDING / 2)

            # CRITICAL FIX: Ensure EditableTextItem respects width limits
            try:
                self.description_text.setTextWidth(content_width)
            except Exception:
                pass
        # cache for paint()
        self._header_h = header_height

        # Позиционируем оверлей поверх иконки удаления (в локальных координатах TaskBlock)
        try:
            if self._close_overlay is not None:
                icon_size = 16
                margin = 8
                padding = 1
                header_rect = QtCore.QRectF(1, 1, BLOCK_WIDTH - 2, header_height)
                x = header_rect.right() - icon_size - margin
                y_top = header_rect.top() + (header_rect.height() - icon_size) / 2
                bg_rect = QtCore.QRectF(x - padding, y_top - padding, icon_size + 2 * padding, icon_size + 2 * padding)
                self._close_overlay.setRect(bg_rect)
                self._close_overlay.setZValue(1000)
                self._close_rect = bg_rect
        except Exception:
            pass

        y = header_height

        # --- Actions and Insert Zones ---
        for zone in self.action_insert_zones:
            zone.setParentItem(None)
            if self.scene():
                try:
                    self.scene().removeItem(zone)
                except Exception:
                    pass
        self.action_insert_zones.clear()

        zone = ActionInsertZone(content_width, self, 0, parent=self)
        zone.setPos(10, y)
        self.action_insert_zones.append(zone)
        y += zone.rect().height()

        for i, block in enumerate(self.action_blocks):
            h = block.calc_height_for_width(content_width)
            block.setGeometry(QtCore.QRectF(10, y, content_width, h))
            y += h
            zone = ActionInsertZone(content_width, self, i + 1, parent=self)
            zone.setPos(10, y)
            self.action_insert_zones.append(zone)
            y += zone.rect().height()

        # --- Footer ---
        if getattr(self, 'use_proxies', False) and getattr(self, 'out_edit', None) and getattr(self, 'out_proxy', None):
            try:
                self.out_edit.document().setTextWidth(content_width)
                self.out_edit.document().adjustSize()
                footer_doc_h = float(self.out_edit.document().size().height())
            except Exception:
                footer_doc_h = float(self.out_edit.viewport().size().height())
            footer_height = footer_doc_h + PADDING
            # lock proxy/widget geometry to full width to prevent width collapse while editing
            try:
                self.out_proxy.setGeometry(QtCore.QRectF(10, y, content_width, footer_doc_h))
                self.out_proxy.setMinimumSize(QtCore.QSizeF(content_width, footer_doc_h))
                self.out_proxy.setMaximumSize(QtCore.QSizeF(content_width, footer_doc_h))
                self.out_proxy.setPreferredSize(QtCore.QSizeF(content_width, footer_doc_h))
                # enforce fixed pixel wrap and fixed width
                try:
                    self.out_edit.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.FixedPixelWidth)
                    self.out_edit.setLineWrapColumnOrWidth(int(content_width))
                except Exception:
                    pass
                self.out_edit.setMinimumWidth(int(content_width))
                self.out_edit.setMaximumWidth(int(content_width))
                self.out_edit.setFixedWidth(int(content_width))
                self.out_edit.setFixedHeight(int(footer_doc_h))
                # Ensure the widget resizes properly
                self.out_edit.updateGeometry()
            except Exception:
                self.out_proxy.setPos(10, y)
                try:
                    self.out_edit.setFixedWidth(int(content_width))
                    self.out_edit.setFixedHeight(int(footer_doc_h))
                    self.out_edit.updateGeometry()
                except Exception:
                    pass
        else:
            # Force full content width for outcome text
            self.outcome_text.setTextWidth(content_width)
            try:
                doc_f = self.outcome_text.document()
                doc_f.setTextWidth(content_width)
                doc_f.adjustSize()
                footer_doc_h = float(doc_f.size().height())
            except Exception:
                footer_doc_h = float(self.outcome_text.boundingRect().height())
            footer_height = footer_doc_h + PADDING
            self.outcome_text.setPos(10, y)

            # CRITICAL FIX: Ensure EditableTextItem respects width limits
            try:
                self.outcome_text.setTextWidth(content_width)
            except Exception:
                pass

        y += footer_height
        # cache for paint()
        self._footer_h = footer_height

        # Notify scene before changing cached height so size updates immediately
        self.prepareGeometryChange()
        self._cached_height = y
        # Update this item immediately (useful while typing)
        try:
            self.update()
        except Exception:
            pass
        if self.scene():
            self.scene().update()
            if hasattr(self.scene(), 'relayout'):
                QtCore.QTimer.singleShot(0, self.scene().relayout)

    def update_action_data(self, action_block, new_action_data):
        try:
            idx = self.action_blocks.index(action_block)
            self.task_data['actions'][idx] = new_action_data
        except (ValueError, IndexError) as e:
            print(f"Error updating action data: {e}")

    def _compute_close_rect(self) -> QtCore.QRectF:
        """
        Compute the clickable rectangle for the close icon in local coordinates,
        mirroring the geometry used during paint().
        """
        r = self.boundingRect()
        header_height = self.description_text.boundingRect().height() + PADDING
        header_rect = QtCore.QRectF(r.left() + 1, r.top() + 1, r.width() - 2, header_height)

        icon_size = 16
        margin = 8
        padding = 1
        x = header_rect.right() - icon_size - margin
        y = header_rect.top() + (header_rect.height() - icon_size) / 2
        return QtCore.QRectF(x - padding, y - padding, icon_size + 2 * padding, icon_size + 2 * padding)



    def mouseMoveEvent(self, event):
        if event.buttons() != QtCore.Qt.LeftButton:
            return super().mouseMoveEvent(event)

        if (
            event.screenPos() - event.buttonDownScreenPos(QtCore.Qt.LeftButton)
        ).manhattanLength() < QtWidgets.QApplication.startDragDistance():
            return super().mouseMoveEvent(event)

        drag = QtGui.QDrag(event.widget())
        mime = QtCore.QMimeData()

        try:
            idx = self.scene().blocks.index(self)
            mime.setText(f"task-drag:{idx}")
            drag.setMimeData(mime)
            drag.exec_(QtCore.Qt.MoveAction)
        except (ValueError, AttributeError):
            pass

        super().mouseMoveEvent(event)

    def boundingRect(self):
        if self._cached_height < 0:
            self.relayout_actions()
        rect = QtCore.QRectF(0, 0, BLOCK_WIDTH, self._cached_height)
        return rect

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        container_rect = self.boundingRect()
        painter.setBrush(QtGui.QColor("#2D2D2D"))

        pen_color = (
            QtGui.QColor("#50fa7b")
            if option.state & QtWidgets.QStyle.State_Selected
            else QtGui.QColor("#555555")
        )
        pen_width = 2.0 if option.state & QtWidgets.QStyle.State_Selected else 1.5
        painter.setPen(QtGui.QPen(pen_color, pen_width))
        painter.drawRoundedRect(container_rect, 10, 10)

        header_height = getattr(self, "_header_h", self.description_text.boundingRect().height() + PADDING)
        header_rect = QtCore.QRectF(
            container_rect.left() + 1,
            container_rect.top() + 1,
            container_rect.width() - 2,
            header_height,
        )
        painter.setBrush(QtGui.QColor("#3A3A3A"))
        painter.setPen(QtCore.Qt.NoPen)
        path = QtGui.QPainterPath()
        path.addRoundedRect(header_rect, 9, 9)
        path.addRect(header_rect.left(), header_rect.top() + 20, header_rect.width(), header_rect.height() - 20)
        painter.drawPath(path)

        if self.close_icon:
            icon_size = 16
            margin = 8
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
                try:
                    red_icon = qta.icon('fa5s.times', color='#c83030')
                except Exception:
                    red_icon = qta.icon('fa5s.trash', color='#c83030')
                red_icon.paint(painter, icon_rect.toRect())
            except Exception:
                pass

        footer_height = getattr(self, "_footer_h", self.outcome_text.boundingRect().height() + PADDING)
        footer_rect = QtCore.QRectF(
            container_rect.left() + 1,
            container_rect.bottom() - footer_height,
            container_rect.width() - 2,
            footer_height,
        )
        painter.setBrush(QtGui.QColor("#3A3A3A"))
        path = QtGui.QPainterPath()
        path.addRoundedRect(footer_rect, 9, 9)
        path.addRect(footer_rect.left(), footer_rect.top(), footer_rect.width(), 20)
        painter.drawPath(path)


class InsertZone(QtWidgets.QGraphicsRectItem):
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
            try:
                self.button_proxy.widget().setParent(None)
            except Exception:
                pass
            try:
                self.button_proxy.deleteLater()
            except Exception:
                pass
            self.button_proxy = None
            self.button = None
        super().hoverLeaveEvent(event)

    def _on_button_clicked(self):
        if self.callback_add:
            parent = None
            if self.scene() and self.scene().views():
                parent = self.scene().views()[0].window()
            self.callback_add(idx=self.idx, parent=parent)


class BlockScene(QtWidgets.QGraphicsScene):
    def __init__(self, plan=None, left_margin=40):
        super().__init__()
        self.blocks = [] # This will now store TaskBlocks
        self.insert_zones = []
        self._plan = plan or []
        # left margin controls x offset of blocks; computed by dialog
        self.left_margin = left_margin
        self.setBackgroundBrush(QtGui.QColor(245, 245, 245))
        self.rebuild()

    def dragEnterEvent(self, event):
        if event.mimeData().hasText() and event.mimeData().text().startswith("task-drag:"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        mime_text = event.mimeData().text()
        if mime_text.startswith("task-drag:"):
            try:
                source_idx = int(mime_text.split(":")[1])

                # Find drop position
                drop_y = event.scenePos().y()
                target_idx = 0
                for i, block in enumerate(self.blocks):
                    if drop_y > block.pos().y() + block.boundingRect().height() / 2:
                        target_idx = i + 1

                # Adjust target index if moving item downwards
                if source_idx < target_idx:
                    target_idx -= 1

                if source_idx != target_idx:
                    # Move data
                    task_data = self._plan.pop(source_idx)
                    self._plan.insert(target_idx, task_data)

                    # Move UI block
                    task_block = self.blocks.pop(source_idx)
                    self.blocks.insert(target_idx, task_block)

                    self.relayout()

                event.acceptProposedAction()
            except (ValueError, IndexError) as e:
                print(f"Task drop failed: {e}")
                event.ignore()
        else:
            event.ignore()

    def remove_task(self, task_block_to_remove):
        try:
            idx = self.blocks.index(task_block_to_remove)
            del self._plan[idx]
            self.blocks.pop(idx)

            # The insert_zones list has one more item than blocks (one at the top)
            # So the zone after block `idx` is at `idx + 1`
            zone_to_remove = self.insert_zones.pop(idx + 1)

            self.removeItem(task_block_to_remove)
            self.removeItem(zone_to_remove)

            self.relayout()
        except (ValueError, IndexError) as e:
            print(f"Error removing task: {e}")

    def add_task_with_dialog(self, idx=None, parent=None):
        # This is a placeholder for a future "Add Task" dialog.
        # For now, we'll add a default task structure.
        new_task = {
            "task_description": "New Task Description",
            "actions": [],
            "expected_outcome": "New Expected Outcome"
        }
        if idx is None:
            idx = len(self._plan)
        self._plan.insert(idx, new_task)
        self.rebuild()


    def rebuild(self):
        self.clear()
        self.blocks.clear()
        self.insert_zones.clear()


        y = 20.0

        # Insert zone at the very top
        zone_top = InsertZone(BLOCK_WIDTH, self.add_task_with_dialog, 0)
        zone_top.setPos(self.left_margin, y)
        self.addItem(zone_top)
        self.insert_zones.append(zone_top)
        y += INTER_BLOCK_GAP

        for i, task_data in enumerate(self._plan):
            task_block = TaskBlock(task_data)
            self.addItem(task_block)
            h = task_block.boundingRect().height()
            task_block.setPos(self.left_margin, y)
            self.blocks.append(task_block)
            y += h

            # Insert zone after each task block
            zone = InsertZone(BLOCK_WIDTH, self.add_task_with_dialog, i + 1)
            zone.setPos(self.left_margin, y)
            self.addItem(zone)
            self.insert_zones.append(zone)
            y += INTER_BLOCK_GAP

        scene_rect = self.itemsBoundingRect()
        self.setSceneRect(scene_rect)


    def relayout(self):
        y = 20.0

        # Reposition top insert zone
        self.insert_zones[0].setPos(self.left_margin, y)
        y += INTER_BLOCK_GAP

        for i, task_block in enumerate(self.blocks):
            h = task_block.boundingRect().height()
            task_block.setPos(self.left_margin, y)
            y += h

            # Reposition the insert zone that follows this block
            zone = self.insert_zones[i + 1]
            zone.setPos(self.left_margin, y)
            y += INTER_BLOCK_GAP

        scene_rect = self.itemsBoundingRect()
        self.setSceneRect(scene_rect)

    def insert_block_with_action(self, action: dict, idx: int):
        # This method is now deprecated for inserting top-level actions.
        # It will need to be adapted to add an action to a specific task.
        print("TODO: Implement adding an action to a specific task.")

    def save_json(self):
        # This method needs to be updated to reconstruct the plan from TaskBlocks
        data = []
        for task_block in self.blocks:
            task_data = deepcopy(task_block.task_data)
            task_data["actions"] = []
            for action_block in task_block.action_blocks:
                 task_data["actions"].append(action_block.get_data())
            data.append(task_data)

        path, _ = QtWidgets.QFileDialog.getSaveFileName(None, "Save JSON", filter="JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            QtWidgets.QMessageBox.information(None, "Saved", f"Saved to:\n{path}")


class PlanEditorDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, plan=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Strategic Plan (Block Editor)")
        self.resize(700, 800)
        # Apply global app theme so the dialog looks like a native component
        try:
            self.setStyleSheet(styles.DARK_STYLE)
        except Exception:
            pass
        self._plan = deepcopy(plan or [])
        self._parsed_plan = None

        # If a parent (chat window) is provided, match its width so the editor
        # visually lines up with the chat window (removes white gutters).
        try:
            if parent is not None:
                parent_w = parent.width() or parent.geometry().width()
            else:
                parent_w = None
        except Exception:
            parent_w = None

        dialog_width = parent_w if parent_w and parent_w > 200 else 700
        # Fix the dialog width to the chat window width to avoid side gutters
        try:
            self.setFixedWidth(dialog_width)
        except Exception:
            self.resize(dialog_width, self.height())

        # Compute BLOCK_WIDTH to fill the dialog horizontally with small padding
        try:
            # prefer almost-full width, leave 20px total padding (10px each side)
            computed_block_w = max(320, dialog_width - 20)
            left_margin = max(8, (dialog_width - computed_block_w) // 2)
            # update module-level BLOCK_WIDTH so ActionBlock/BlockScene use it
            global BLOCK_WIDTH
            BLOCK_WIDTH = computed_block_w

        except Exception:
            left_margin = 40
            pass

        layout = QtWidgets.QVBoxLayout(self)

        # Pass left_margin to the scene so items use the full dialog width
        self.scene = BlockScene(self._plan, left_margin=left_margin)
        self.view = QtWidgets.QGraphicsView(self.scene)
        self.view.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self.view.setFrameShape(QtWidgets.QFrame.NoFrame)

        # Allow horizontal scrollbar to see the real issue
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)


        layout.addWidget(self.view, 1)

        toolbar = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Save")
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.add_btn = QtWidgets.QPushButton("Add Task") # Changed from "Add Action"
        # object names reuse app button styles
        self.save_btn.setObjectName("SendButton")
        self.cancel_btn.setObjectName("CancelButton")
        self.add_btn.setObjectName("PilotButton")
        toolbar.addStretch(1)
        toolbar.addWidget(self.add_btn)
        toolbar.addWidget(self.save_btn)
        toolbar.addWidget(self.cancel_btn)
        layout.addLayout(toolbar)

        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn.clicked.connect(self.reject)
        self.add_btn.clicked.connect(lambda: self.scene.add_task_with_dialog(parent=self))

        def relayout_and_scroll_top():
            self.scene.relayout()
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().minimum())

        QtCore.QTimer.singleShot(0, relayout_and_scroll_top)

    def _on_save(self):
        data = []
        for task_block in self.scene.blocks:
            # Reconstruct the task data from the block
            task_data = deepcopy(task_block.task_data)
            actions_data = []
            for action_block in task_block.action_blocks:
                actions_data.append(action_block.get_data())
            task_data['actions'] = actions_data
            data.append(task_data)

        # Auto-append finish action if the last action of the last task isn't 'finish'
        if not data:
            finish_action = {"name": "finish", "arguments": {"description": "Finish", "message": "Task completed."}}
            finish_task = {
                "task_description": "Complete the mission",
                "actions": [finish_action],
                "expected_outcome": "The task is marked as complete.",
            }
            data.append(finish_task)
        else:
            last_task = data[-1]
            if not last_task.get('actions') or last_task['actions'][-1].get('name') != 'finish':
                finish_action = {"name": "finish", "arguments": {"description": "Finish", "message": "Task completed."}}
                if 'actions' not in last_task:
                    last_task['actions'] = []
                last_task['actions'].append(finish_action)

        self._parsed_plan = data
        self.accept()

    def get_plan(self):
        return self._parsed_plan


def open_plan_editor(parent, plan):
    dlg = PlanEditorDialog(parent, plan)
    res = dlg.exec()
    if res == QtWidgets.QDialog.Accepted:
        return dlg.get_plan()
    return None
