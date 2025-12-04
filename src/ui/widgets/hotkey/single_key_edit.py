from PySide6 import QtCore, QtGui, QtWidgets
from .base_hotkey_edit import BaseKeyEdit
from .base_key_dialog import BaseKeyDialog
from src.computer.tools.constants import UI_KEY_MAP

class SingleKeyEdit(BaseKeyEdit):
    """Input field for a SINGLE key."""
    def __init__(self, key_value="", parent=None):
        super().__init__(parent)
        self.key_value = "" # Stores the pyautogui value, e.g., 'f5', 'pagedown'
        self.set_key_sequence(key_value)
        self.button.setToolTip("Choose key…")

    def update_text(self):
        """Updates the display text based on the stored pyautogui value."""
        display_name = ""
        if self.key_value:
            # Reverse lookup: find the display name for the current value
            for name, value in UI_KEY_MAP.items():
                if value == self.key_value:
                    display_name = name
                    break
        self.setText(display_name)

    def set_key_sequence(self, sequence_value):
        """Sets the internal key value (should be a pyautogui-compatible string)."""
        self.key_value = str(sequence_value)
        self.update_text()

    def get_key_sequence(self):
        """Returns the stored pyautogui-compatible key value."""
        return self.key_value

    def keyPressEvent(self, event):
        if not self._is_recording:
            super().keyPressEvent(event)
            return

        key_code = event.key()
        modifiers = event.modifiers()

        if key_code in (
            QtCore.Qt.Key.Key_Control,
            QtCore.Qt.Key.Key_Alt,
            QtCore.Qt.Key.Key_Shift,
            QtCore.Qt.Key.Key_Meta,
        ):
            return

        if modifiers & (QtCore.Qt.KeyboardModifier.ControlModifier |
                        QtCore.Qt.KeyboardModifier.AltModifier |
                        QtCore.Qt.KeyboardModifier.MetaModifier):
            return

        if key_code == QtCore.Qt.Key.Key_Escape:
            self.key_value = ""
            self.update_text()
            self.clearFocus()
            return

        if key_code == QtCore.Qt.Key.Key_unknown:
            return

        # Rebuild the key string manually, similar to HotkeyEdit, for reliability.
        # This correctly handles Shift + key combinations.
        parts = []
        if modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")

        key_text = QtGui.QKeySequence(key_code).toString()
        if key_text and key_text not in parts:
            parts.append(key_text)

        final_key_str = "+".join(parts)

        # Find the corresponding pyautogui value in our map
        pyautogui_value = UI_KEY_MAP.get(final_key_str, "")

        # If not found, it might be a simple character, try without modifiers
        if not pyautogui_value and len(parts) > 1:
             pyautogui_value = UI_KEY_MAP.get(parts[-1], "")

        self.key_value = pyautogui_value if pyautogui_value else final_key_str
        self.update_text()
        self.clearFocus()

    def open_dialog(self):
        dlg = SingleKeyDialog(self)
        # Pass the pyautogui value to the dialog
        dlg.set_key(self.key_value)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            # The dialog returns a pyautogui value
            self.key_value = dlg.get_key_str()
            self.update_text()

class SingleKeyDialog(BaseKeyDialog):
    """Dialog for selecting a single key without modifiers."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Key")

    def set_key(self, key_value):
        """Receives a pyautogui value."""
        self.set_main_key(key_value)

    def get_key_str(self):
        """Returns the selected pyautogui value."""
        return self.get_main_key()
