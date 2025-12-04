"""
Contains the ActionDialog for adding or editing a single action step.
"""
from PySide6 import QtWidgets

from src.computer.tools.constants import UI_MODIFIER_KEYS
from src.ui.widgets.hotkey.hotkey_edit import HotkeyEdit
from src.ui.widgets.hotkey.single_key_edit import SingleKeyEdit

from .actions_loader import load_actions, load_allowed_apps


class ActionDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, selected_action_name=None, arg_values=None):
        super().__init__(parent)
        self.setWindowTitle("Add / Edit Action")
        self.setMinimumWidth(520)
        self.selected_action = None
        self.arg_widgets = {}
        self.arg_values = arg_values or {}
        self.actions = load_actions()

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
        if not act:
             return

        self.selected_action = act
        self.lbl_desc.setText(act.get("description", ""))

        # clear form
        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)
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
            elif arg_name == "method" and act.get("name") == "write_text":
                combo = QtWidgets.QComboBox()
                combo.addItems(["clipboard", "direct"])
                widget = combo
            elif arg_name == "app_name" and act.get("name") == "launch_app":
                combo = QtWidgets.QComboBox()
                allowed_apps = load_allowed_apps()
                if allowed_apps:
                    combo.addItems(allowed_apps)
                else:
                    # Fallback if the app cache is empty or missing
                    combo.setEditable(True)
                    combo.setPlaceholderText("No cached apps found. Enter name manually.")
                widget = combo
            elif arg_name == "key":
                # Use a ComboBox for key_down/key_up, and SingleKeyEdit for others.
                if act.get("name") in ("key_down", "key_up"):
                    combo = QtWidgets.QComboBox()
                    combo.addItems(UI_MODIFIER_KEYS)
                    widget = combo
                else:
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
                # Special handling for 'keys' which should be a list
                if name == "keys" and self.selected_action.get("name") == "hotkey":
                     args[name] = [k.strip() for k in text.split(",") if k.strip()]
                else:
                    args[name] = text
            else:
                args[name] = None
        return {"name": self.selected_action["name"], "arguments": args}
