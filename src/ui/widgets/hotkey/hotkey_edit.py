from PySide6 import QtCore, QtGui, QtWidgets
from .base_hotkey_edit import BaseKeyEdit
from .base_key_dialog import BaseKeyDialog
from src.computer.tools.constants import UI_KEY_COMMON

class HotkeyEdit(BaseKeyEdit):
    """Input field for key combinations (hotkey)."""
    def __init__(self, hotkey="", parent=None):
        super().__init__(parent)
        self.hotkey_sequence = QtGui.QKeySequence()
        self.stored_parts = []
        self.set_key_sequence(hotkey)
        self.button.setToolTip("Choose combination…")

    def update_text(self):
        self.setText('+'.join(self.stored_parts))

    def set_key_sequence(self, sequence):
        if isinstance(sequence, list):
            self.stored_parts = sequence
        else:
            self.stored_parts = sequence.split('+')
        self.hotkey_sequence = QtGui.QKeySequence('+'.join(self.stored_parts), QtGui.QKeySequence.PortableText)
        self.update_text()

    def get_key_sequence(self):
        return self.stored_parts[:]

    def keyPressEvent(self, event):
        if not self._is_recording:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key in (
            QtCore.Qt.Key.Key_Control,
            QtCore.Qt.Key.Key_Alt,
            QtCore.Qt.Key.Key_Shift,
            QtCore.Qt.Key.Key_Meta,
        ):
            return

        if key == QtCore.Qt.Key.Key_Escape:
            self.stored_parts = []
            self.hotkey_sequence = QtGui.QKeySequence()
            self.update_text()
            self.clearFocus()
            return

        if key == QtCore.Qt.Key.Key_unknown:
            return

        modifiers = event.modifiers()
        parts = []
        if modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if modifiers & QtCore.Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if modifiers & QtCore.Qt.KeyboardModifier.MetaModifier:
            parts.append("Meta")

        key_text = QtGui.QKeySequence(key).toString()
        if key_text and key_text not in parts:
            parts.append(key_text)

        self.stored_parts = parts
        self.hotkey_sequence = QtGui.QKeySequence('+'.join(parts), QtGui.QKeySequence.PortableText)
        self.update_text()
        self.clearFocus()

    def open_dialog(self):
        dlg = HotkeyDialog(self)
        dlg.set_hotkey(self.get_key_sequence())
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            hotkey_str = dlg.get_hotkey_str()
            self.set_key_sequence(hotkey_str)
        else:
            # Restore original display if dialog cancelled
            self.clearFocus()

class HotkeyDialog(BaseKeyDialog):
    """Dialog for selecting a key combination with modifiers."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Combination")

        # Override to use UI_KEY_COMMON for hotkey main keys
        self._populate_keys()

        # Modifier checkboxes
        self.ctrl_cb = QtWidgets.QCheckBox('Ctrl')
        self.alt_cb = QtWidgets.QCheckBox('Alt')
        self.shift_cb = QtWidgets.QCheckBox('Shift')
        self.win_cb = QtWidgets.QCheckBox('Win/Meta')

        mod_layout = QtWidgets.QHBoxLayout()
        mod_layout.addWidget(self.ctrl_cb)
        mod_layout.addWidget(self.alt_cb)
        mod_layout.addWidget(self.shift_cb)
        mod_layout.addWidget(self.win_cb)

        # Insert modifier layout at the top of the main layout
        self.main_layout.insertLayout(0, mod_layout)

    def _populate_keys(self):
        """Override to populate with common keys only (no symbols)."""
        self.key_combo.addItem('<None>')
        # Use the original insertion order from UI_KEY_COMMON
        keys_in_order = UI_KEY_COMMON.keys()
        self.key_combo.addItems(keys_in_order)


    def get_main_key(self):
        """
        Override to return the display name for the selected key (for hotkeys).
        """
        display_name = self.key_combo.currentText()
        if display_name != '<None>':
            return display_name
        return ""

    def set_main_key(self, key_value):
        """
        Override to select a key in the combo box based on its pyautogui value using UI_KEY_COMMON.
        Performs a reverse lookup to find the display name.
        """
        display_name_to_set = '<None>'
        if key_value:
            # Find the display name (key) that corresponds to the given value in UI_KEY_COMMON
            for name, value in UI_KEY_COMMON.items():
                if value == key_value:
                    display_name_to_set = name
                    break

        index = self.key_combo.findText(display_name_to_set, QtCore.Qt.MatchFlag.MatchFixedString)
        self.key_combo.setCurrentIndex(index if index >= 0 else 0)


    def set_hotkey(self, keys_list):
        def normalize_key(key):
            key_lower = key.lower()
            if key_lower in ('control', 'ctrl'):
                return 'Ctrl'
            elif key_lower == 'alt':
                return 'Alt'
            elif key_lower == 'shift':
                return 'Shift'
            elif key_lower == 'meta':
                return 'Meta'
            elif key_lower  == 'win':
                return 'Win'
            else:
                # For main key, uppercase letters, keep symbols as is
                return key.upper() if key.isalpha() else key

        normalized_list = [normalize_key(k) for k in keys_list]

        self.ctrl_cb.setChecked('Ctrl' in normalized_list)
        self.alt_cb.setChecked('Alt' in normalized_list)
        self.shift_cb.setChecked('Shift' in normalized_list)
        self.win_cb.setChecked('Meta' in normalized_list)

        main_key = next((k for k in normalized_list if k not in ('Ctrl', 'Alt', 'Shift', 'Meta')), "")
        self.set_main_key(main_key)

    def get_hotkey_str(self):
        parts = []
        if self.ctrl_cb.isChecked():
            parts.append('Ctrl')
        if self.alt_cb.isChecked():
            parts.append('Alt')
        if self.shift_cb.isChecked():
            parts.append('Shift')
        if self.win_cb.isChecked():
            parts.append('Meta')

        key = self.get_main_key()
        if key:
            parts.append(key)
        return '+'.join(parts)
