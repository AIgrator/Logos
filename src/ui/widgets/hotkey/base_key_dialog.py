from PySide6 import QtCore, QtWidgets
from src.computer.tools.constants import UI_KEY_MAP

class BaseKeyDialog(QtWidgets.QDialog):
    """
    Base dialog for selecting a key.
    Provides a combo box with a standard list of keys from UI_KEY_MAP and OK/Cancel buttons.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(True)

        self.main_layout = QtWidgets.QVBoxLayout(self)

        # Key selection combo box
        self.main_layout.addWidget(QtWidgets.QLabel("Key:"))
        self.key_combo = QtWidgets.QComboBox()
        self._populate_keys()
        self.main_layout.addWidget(self.key_combo)

        # OK/Cancel buttons
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.main_layout.addWidget(buttons)

    def _populate_keys(self):
        """Fills the combo box with keys from the UI_KEY_MAP in their original order."""
        self.key_combo.addItem('<None>')
        # Use the original insertion order from the dictionary, do not sort.
        keys_in_order = UI_KEY_MAP.keys()
        self.key_combo.addItems(keys_in_order)

    def set_main_key(self, key_value):
        """
        Selects a key in the combo box based on its pyautogui value.
        Performs a reverse lookup to find the display name.
        """
        display_name_to_set = '<None>'
        if key_value:
            # Find the display name (key) that corresponds to the given value
            for name, value in UI_KEY_MAP.items():
                if value == key_value:
                    display_name_to_set = name
                    break

        index = self.key_combo.findText(display_name_to_set, QtCore.Qt.MatchFlag.MatchFixedString)
        self.key_combo.setCurrentIndex(index if index >= 0 else 0)

    def get_main_key(self):
        """
        Gets the pyautogui value for the currently selected key from the combo box.
        """
        display_name = self.key_combo.currentText()
        if display_name and display_name != '<None>':
            # Return the pyautogui value from the map
            return UI_KEY_MAP.get(display_name, "")
        return ""
