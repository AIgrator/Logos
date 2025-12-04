"""
The main entry point for the Plan Editor. Contains the PlanEditorDialog
and the `open_plan_editor` function.
"""
from copy import deepcopy

from PySide6 import QtCore, QtGui, QtWidgets

from .scene import BlockScene

# Global variable to hold block width, configurable by the dialog
BLOCK_WIDTH = 420

class PlanEditorDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, plan=None):
        super().__init__(parent)
        self.setWindowTitle("Strategic Plan Editor (Refactored)")

        # --- Default & Minimum Width Calculation ---
        global BLOCK_WIDTH
        BLOCK_WIDTH = 420
        min_dialog_width = BLOCK_WIDTH + 40
        self.setMinimumWidth(min_dialog_width)
        self.resize(min_dialog_width, 800)

        self._plan = deepcopy(plan or [])
        self._parsed_plan = None

        # --- Layout and Widgets ---
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Initial left margin calculation
        left_margin = (self.width() - BLOCK_WIDTH) // 2

        self.scene = BlockScene(
            plan_data=self._plan,
            block_width=BLOCK_WIDTH,
            left_margin=left_margin
        )
        self.view = QtWidgets.QGraphicsView(self.scene)
        self.view.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self.view.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        layout.addWidget(self.view, 1)

        # --- Toolbar ---
        toolbar = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Save")
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.add_task_btn = QtWidgets.QPushButton("Add Task")

        toolbar.addStretch(1)
        toolbar.addWidget(self.add_task_btn)
        toolbar.addWidget(self.save_btn)
        toolbar.addWidget(self.cancel_btn)
        layout.addLayout(toolbar)

        # --- Connections ---
        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn.clicked.connect(self.reject)
        self.add_task_btn.clicked.connect(lambda: self.scene.add_task(parent=self))

        QtCore.QTimer.singleShot(0, self._initial_view_setup)

    def resizeEvent(self, event: QtGui.QResizeEvent):
        """Handle window resize to update block widths."""
        super().resizeEvent(event)
        new_dialog_width = event.size().width()
        new_block_width = new_dialog_width - 40 # Maintain margins
        new_left_margin = (new_dialog_width - new_block_width) // 2

        # Update the scene with the new dimensions
        if hasattr(self, 'scene'):
            self.scene.update_dimensions(new_block_width, new_left_margin)

    def _initial_view_setup(self):
        self.scene.relayout()
        self.view.verticalScrollBar().setValue(0)

    def _on_save(self):
        data = self.scene.get_plan_data()

        # Auto-append finish action if necessary
        if not data:
            finish_action = {"name": "finish", "arguments": {"description": "Finish", "message": "Task completed."}}
            finish_task = {
                "task_description": "Complete the mission",
                "actions": [finish_action],
                "expected_outcome": "The task is marked as complete."
            }
            data.append(finish_task)
        else:
            last_task = data[-1]
            if not last_task.get('actions') or last_task['actions'][-1].get('name') != 'finish':
                finish_action = {"name": "finish", "arguments": {"description": "Finish", "message": "Task completed."}}
                last_task.setdefault('actions', []).append(finish_action)

        self._parsed_plan = data
        self.accept()

    def get_plan(self):
        return self._parsed_plan


def open_plan_editor(parent, plan):
    """
    Public function to create, show, and return the result of the editor dialog.
    """
    dlg = PlanEditorDialog(parent, plan)
    if dlg.exec() == QtWidgets.QDialog.Accepted:
        return dlg.get_plan()
    return None
