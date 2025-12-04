from PySide6 import QtCore, QtGui, QtWidgets

class BaseKeyEdit(QtWidgets.QLineEdit):
    """
    Base class for key input fields.
    Handles common logic: button creation, recording styles,
    focus events, and resizing.
    """
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setReadOnly(True)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self._is_recording = False

        # "..." button to open the dialog
        self.button = QtWidgets.QToolButton(self)
        self.button.setText("…")
        self.button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.button.setFixedWidth(22)
        self.button.clicked.connect(self.open_dialog)
        self.setTextMargins(0, 0, self.button.width(), 0)

    def resizeEvent(self, event):
        """Moves the button when the field is resized."""
        super().resizeEvent(event)
        self.button.move(self.rect().right() - self.button.width(), 0)

    def focusInEvent(self, event):
        """Enters recording mode on focus."""
        super().focusInEvent(event)
        self._is_recording = True
        self.setText("Press key(s)...")
        self.setStyleSheet("background-color: #4a4a4a; color: white;")

    def focusOutEvent(self, event):
        """Exits recording mode and restores text on focus out."""
        super().focusOutEvent(event)
        self._is_recording = False
        self.setStyleSheet("")
        self.update_text() # Method to be implemented by subclasses

    def keyPressEvent(self, event):
        """Handles key presses (must be implemented in subclasses)."""
        if not self._is_recording:
            super().keyPressEvent(event)
            return
        # Recording logic will be in child classes

    def open_dialog(self):
        """Opens the selection dialog (must be implemented in subclasses)."""
        raise NotImplementedError("This method must be implemented in a subclass.")

    def update_text(self):
        """Updates the displayed text (must be implemented in subclasses)."""
        raise NotImplementedError("This method must be implemented in a subclass.")

    def get_key_sequence(self):
        """Gets the current value (must be implemented in subclasses)."""
        raise NotImplementedError("This method must be implemented in a subclass.")

    def set_key_sequence(self, sequence):
        """Sets a new value (must be implemented in subclasses)."""
        raise NotImplementedError("This method must be implemented in a subclass.")
