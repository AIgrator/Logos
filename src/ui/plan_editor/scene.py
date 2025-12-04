"""
Manages the QGraphicsScene, laying out TaskBlocks and handling drag-and-drop.
"""
from PySide6 import QtGui, QtWidgets

from .components import InsertZone
from .task_block import TaskBlock

INTER_BLOCK_GAP = 16

class BlockScene(QtWidgets.QGraphicsScene):
    def __init__(self, plan_data=None, block_width=420, left_margin=40):
        super().__init__()
        self.blocks = []
        self.insert_zones = []
        self._plan_data = plan_data or []
        self._block_width = block_width
        self.left_margin = left_margin

        self.setBackgroundBrush(QtGui.QColor(43, 43, 43))
        self.rebuild()

    def update_dimensions(self, new_block_width, new_left_margin):
        """Updates the width of all blocks and the scene margin."""
        self._block_width = new_block_width
        self.left_margin = new_left_margin
        for block in self.blocks:
            block.set_width(new_block_width)
        for zone in self.insert_zones:
            zone.setRect(0, 0, new_block_width, INTER_BLOCK_GAP)
        self.relayout()

    def dragEnterEvent(self, event):
        if event.mimeData().hasText() and event.mimeData().text().startswith("task-drag:"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        mime_text = event.mimeData().text()
        if not mime_text.startswith("task-drag:"):
            event.ignore()
            return

        try:
            source_idx = int(mime_text.split(":")[1])

            drop_y = event.scenePos().y()
            target_idx = 0
            # Find the index where the block should be inserted
            for i, block in enumerate(self.blocks):
                if drop_y > block.pos().y() + block.boundingRect().height() / 2:
                    target_idx = i + 1

            if source_idx < target_idx:
                target_idx -= 1

            if source_idx != target_idx:
                # Move data in the underlying plan list
                task_data = self._plan_data.pop(source_idx)
                self._plan_data.insert(target_idx, task_data)

                # Move the UI block
                task_block = self.blocks.pop(source_idx)
                self.blocks.insert(target_idx, task_block)

                self.relayout()

            event.acceptProposedAction()
        except (ValueError, IndexError) as e:
            print(f"Task drop failed: {e}")
            event.ignore()

    def remove_task(self, task_block_to_remove):
        try:
            idx = self.blocks.index(task_block_to_remove)
            del self._plan_data[idx]
            self.blocks.pop(idx)

            self.removeItem(task_block_to_remove)

            # A full rebuild is simpler and safer than trying to manage insert zones manually
            self.rebuild()
        except (ValueError, IndexError) as e:
            print(f"Error removing task: {e}")

    def add_task(self, idx=None, parent=None):
        new_task = {
            "task_description": "New Task Description",
            "actions": [],
            "expected_outcome": "New Expected Outcome"
        }
        if idx is None:
            idx = len(self._plan_data)

        self._plan_data.insert(idx, new_task)
        self.rebuild() # Rebuild the scene to include the new task

    def rebuild(self):
        # Clear everything except the underlying data
        for item in self.items():
            self.removeItem(item)
        self.blocks.clear()
        self.insert_zones.clear()

        # Create and add items based on the current plan data
        for task_data in self._plan_data:
            task_block = TaskBlock(task_data, self._block_width)
            # When a task block's height changes (e.g., accordion), relayout the scene
            task_block.geometry_changed.connect(self.relayout)
            self.addItem(task_block)
            self.blocks.append(task_block)

        # Create insert zones
        for i in range(len(self.blocks) + 1):
            zone = InsertZone(self._block_width, self.add_task, i)
            self.addItem(zone)
            self.insert_zones.append(zone)

        self.relayout()

    def relayout(self):
        y = 20.0

        # Position top insert zone
        self.insert_zones[0].setPos(self.left_margin, y)
        y += INTER_BLOCK_GAP

        for i, task_block in enumerate(self.blocks):
            task_block.setPos(self.left_margin, y)
            y += task_block.boundingRect().height()

            # Position the insert zone that follows this block
            if i + 1 < len(self.insert_zones):
                self.insert_zones[i + 1].setPos(self.left_margin, y)
                y += INTER_BLOCK_GAP

        # Update scene rectangle to allow scrolling
        self.setSceneRect(self.itemsBoundingRect())

    def get_plan_data(self):
        """Retrieves the final, ordered plan data from the UI blocks."""
        return [block.get_data() for block in self.blocks]
