"""
Represents a collapsible Task block, which contains a list of ActionBlocks.
This is the core component for the "accordion" functionality.
"""
from PySide6 import QtCore, QtGui, QtWidgets
import qtawesome as qta

from .action_block import ActionBlock
from .components import ActionInsertZone, CloseButtonItem, EditableTextItem
from .dialogs import ActionDialog

# Constants
PADDING = 10
TITLE_FONT = QtGui.QFont("Segoe UI", 11, QtGui.QFont.Bold)
DESC_FONT = QtGui.QFont("Segoe UI", 10)


class TaskBlock(QtWidgets.QGraphicsWidget):
    # Signal to notify the scene that this block's geometry has changed
    geometry_changed = QtCore.Signal()

    def __init__(self, task_data, block_width, parent=None):
        super().__init__(parent)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, True)
        self.setFiltersChildEvents(True)
        self.setAcceptedMouseButtons(QtCore.Qt.LeftButton)

        self.task_data = task_data
        self.action_blocks = []
        self.action_insert_zones = []
        self._block_width = block_width
        self._cached_height = -1

        # --- Accordion State ---
        self.is_collapsed = False
        self._toggle_rect = None # Clickable area for collapse/expand
        self._close_rect = None  # Clickable area for the close button

        # --- UI Elements ---
        self.description_text = EditableTextItem(
            text=self.task_data.get('task_description', 'No description'),
            prefix="Task: ", font=TITLE_FONT, color=QtGui.QColor(230, 230, 230), parent=self
        )
        self.outcome_text = EditableTextItem(
            text=self.task_data.get('expected_outcome', 'No outcome specified'),
            prefix="✅ Outcome: ", font=DESC_FONT, color=QtGui.QColor(200, 200, 200), parent=self
        )
        self._close_overlay = CloseButtonItem(self)

        # Connect signals for text changes
        self.description_text.editing_finished.connect(self._on_description_changed)
        self.outcome_text.editing_finished.connect(self._on_outcome_changed)
        self.description_text.live_edited.connect(self.relayout_actions)
        self.outcome_text.live_edited.connect(self.relayout_actions)

        # Create child ActionBlocks
        for action_data in self.task_data.get("actions", []):
            self._create_action_block(action_data)

        self.relayout_actions()

    def set_width(self, new_width):
        """Sets a new width for the block and its children."""
        if self._block_width == new_width:
            return

        self._block_width = new_width
        content_width = new_width - 2 * PADDING

        for action_block in self.action_blocks:
            action_block.set_width(content_width)

        for zone in self.action_insert_zones:
            zone.setRect(0, 0, content_width, zone.rect().height())

        self.relayout_actions()

    def _create_action_block(self, action_data, at_index=None):
        """Helper to create and connect a single ActionBlock."""
        content_width = self._block_width - 2 * PADDING
        action_block = ActionBlock(action_data, content_width, parent=self)
        action_block.data_changed.connect(
            lambda data, block=action_block: self.update_action_data(block, data)
        )
        action_block.delete_requested.connect(self.remove_action)

        if at_index is None or at_index >= len(self.action_blocks):
            self.action_blocks.append(action_block)
        else:
            self.action_blocks.insert(at_index, action_block)
        return action_block

    def mousePressEvent(self, event):
        pos = event.pos()
        if self._toggle_rect and self._toggle_rect.contains(pos):
            self.is_collapsed = not self.is_collapsed
            self.relayout_actions()
            event.accept()
            return

        # Note: Close button is handled by the CloseButtonItem overlay

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            (event.screenPos() - event.buttonDownScreenPos(QtCore.Qt.LeftButton)).manhattanLength()
            < QtWidgets.QApplication.startDragDistance()
        ):
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

    def _on_description_changed(self, new_text):
        self.task_data['task_description'] = new_text
        self.relayout_actions()

    def _on_outcome_changed(self, new_text):
        self.task_data['expected_outcome'] = new_text
        self.relayout_actions()

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

                # Rebuild UI block list from data list
                for block in self.action_blocks:
                    block.setParentItem(None)
                self.action_blocks.clear()
                for data in actions_list:
                    self._create_action_block(data)

                self.relayout_actions()

    def remove_action(self, action_block_to_remove):
        try:
            idx = self.action_blocks.index(action_block_to_remove)
            del self.task_data['actions'][idx]
            self.action_blocks.pop(idx)
            action_block_to_remove.setParentItem(None)
            self.relayout_actions()
        except (ValueError, IndexError) as e:
            print(f"Error removing action: {e}")

    def update_action_data(self, action_block, new_action_data):
        try:
            idx = self.action_blocks.index(action_block)
            self.task_data['actions'][idx] = new_action_data
            # No relayout needed if only data changes, but action_block will trigger it
        except (ValueError, IndexError) as e:
            print(f"Error updating action data: {e}")

    def relayout_actions(self):
        self.prepareGeometryChange()
        content_width = self._block_width - 2 * PADDING

        # --- Header ---
        # Reserve space for the toggle icon on the left
        icon_space = 30
        text_left_margin = PADDING + icon_space
        text_content_width = self._block_width - text_left_margin - PADDING

        self.description_text.setTextWidth(text_content_width)
        header_height = self.description_text.boundingRect().height() + PADDING
        self.description_text.setPos(text_left_margin, PADDING / 2)

        # Position overlays for close and toggle buttons
        self._close_rect = QtCore.QRectF(self._block_width - 30, PADDING, 20, 20)
        self._close_overlay.setRect(self._close_rect)
        self._toggle_rect = QtCore.QRectF(PADDING, PADDING, 20, 20)

        y = header_height

        # --- Actions and Insert Zones (Accordion Logic) ---
        are_actions_visible = not self.is_collapsed

        # Clear old insert zones
        for zone in self.action_insert_zones:
            zone.setParentItem(None)
        self.action_insert_zones.clear()

        # Create first insert zone
        zone = ActionInsertZone(content_width, self, 0, parent=self)
        zone.setPos(PADDING, y)
        zone.setVisible(are_actions_visible)
        self.action_insert_zones.append(zone)
        if are_actions_visible:
            y += zone.rect().height()

        # Layout action blocks
        for i, block in enumerate(self.action_blocks):
            block.setVisible(are_actions_visible)
            if are_actions_visible:
                h = block.calc_height_for_width(content_width)
                block.setGeometry(QtCore.QRectF(PADDING, y, content_width, h))
                y += h

            zone = ActionInsertZone(content_width, self, i + 1, parent=self)
            zone.setPos(PADDING, y)
            zone.setVisible(are_actions_visible)
            self.action_insert_zones.append(zone)
            if are_actions_visible:
                y += zone.rect().height()

        # --- Footer ---
        self.outcome_text.setTextWidth(content_width)
        footer_height = self.outcome_text.boundingRect().height() + PADDING
        self.outcome_text.setPos(PADDING, y)

        y += footer_height
        self._cached_height = y

        self.update()
        self.geometry_changed.emit()

    def boundingRect(self):
        if self._cached_height < 0:
            self.relayout_actions()
        return QtCore.QRectF(0, 0, self._block_width, self._cached_height)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        r = self.boundingRect()
        painter.setBrush(QtGui.QColor("#2D2D2D"))

        pen_color = (
            QtGui.QColor("#50fa7b")
            if option.state & QtWidgets.QStyle.State_Selected
            else QtGui.QColor("#555555")
        )
        painter.setPen(QtGui.QPen(pen_color, 1.5))
        painter.drawRoundedRect(r, 10, 10)

        header_height = self.description_text.boundingRect().height() + PADDING
        header_rect = QtCore.QRectF(1, 1, r.width() - 2, header_height)

        painter.setBrush(QtGui.QColor("#3A3A3A"))
        painter.setPen(QtCore.Qt.NoPen)
        path = QtGui.QPainterPath()
        path.addRoundedRect(header_rect, 9, 9)
        if not self.is_collapsed: # Don't round bottom corners if expanded
            path.addRect(header_rect.left(), header_rect.top() + 20, header_rect.width(), header_rect.height() - 20)
        painter.drawPath(path)

        # --- Paint Icons ---
        # Toggle Icon
        toggle_icon_name = 'fa5s.chevron-down' if not self.is_collapsed else 'fa5s.chevron-right'
        try:
            qta.icon(toggle_icon_name, color='white').paint(painter, self._toggle_rect.toRect())
        except Exception as e:
            print(f"Could not paint toggle icon: {e}")

        # Close Icon
        try:
            qta.icon('fa5s.times', color='#c83030').paint(painter, self._close_rect.toRect())
        except Exception as e:
            print(f"Could not paint close icon: {e}")

        # --- Footer ---
        footer_y = r.bottom() - (self.outcome_text.boundingRect().height() + PADDING)
        footer_rect = QtCore.QRectF(1, footer_y, r.width() - 2, self.outcome_text.boundingRect().height() + PADDING)
        painter.setBrush(QtGui.QColor("#3A3A3A"))
        path = QtGui.QPainterPath()
        path.addRoundedRect(footer_rect, 9, 9)
        path.addRect(footer_rect.left(), footer_rect.top(), footer_rect.width(), 20) # Flatten top edge
        painter.drawPath(path)

    def get_data(self):
        """Reconstructs the task data from the current state of the UI."""
        self.task_data['actions'] = [block.get_data() for block in self.action_blocks]
        self.task_data['task_description'] = self.description_text.toPlainText().replace("Task: ", "", 1)
        self.task_data['expected_outcome'] = self.outcome_text.toPlainText().replace("✅ Outcome: ", "", 1)
        return self.task_data
