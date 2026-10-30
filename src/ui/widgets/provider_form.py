"""Schema-driven settings form for a single LLM provider.

Each provider class describes its settings with ``get_ui_schema()``. This
widget turns that schema into fields (API key, model dropdown or a
provider/model pair, checkboxes and notes) and converts them back to the
values stored under ``llm_providers.<provider_id>``. Shared by the initial
setup widget and the Settings provider dialog so both render the same form.
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QWidget,
)

from llm import PROVIDERS
from llm.model_config_manager import ModelConfigManager


class ProviderForm(QWidget):
    """Fields for one provider, laid out with the provider's UI schema."""

    def __init__(self, provider_id, parent=None):
        super().__init__(parent)
        self.provider_id = provider_id
        provider_class = PROVIDERS[provider_id]
        self.schema = provider_class.get_ui_schema() or {}
        self.defaults = provider_class.get_default_settings() or {}
        self.fields = {}

        self.layout = QFormLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        for key, schema_item in self.schema.items():
            widget = self._create_field(
                schema_item, self.defaults.get(key))
            if widget is None:
                continue
            self.fields[key] = widget
            field_type = schema_item.get("type")
            if field_type in ("checkbox", "note"):
                self.layout.addRow(widget)
            elif field_type == "nested_dropdown":
                self.layout.addRow(
                    schema_item.get("primary_dropdown_label", "Provider"),
                    widget["primary"])
                self.layout.addRow(
                    schema_item.get("secondary_dropdown_label", "Model"),
                    widget["secondary"])
            else:
                self.layout.addRow(schema_item.get("label", key), widget)

    # --- construction -----------------------------------------------------

    def _create_field(self, schema_item, default_value):
        """Creates a widget based on a schema definition."""
        widget_type = schema_item.get("type")
        widget = None

        if widget_type == "password":
            widget = QLineEdit()
            widget.setEchoMode(QLineEdit.Password)
        elif widget_type == "string":
            widget = QLineEdit(
                str(default_value) if default_value is not None else "")
        elif widget_type == "dropdown":
            widget = QComboBox()
            # Editable so users can paste/type any model id manually
            # (e.g. "Qwen/Qwen3.8-27B:novita"); listed items come from models.json.
            widget.setEditable(True)
            populate_model_dropdown(widget, self.provider_id)
        elif widget_type == "nested_dropdown":
            primary_combo = QComboBox()
            secondary_combo = QComboBox()
            # Editable like the flat model dropdown, so a stored id that is
            # missing from the cached list survives editing.
            secondary_combo.setEditable(True)
            widget = {'primary': primary_combo, 'secondary': secondary_combo}
            self._populate_nested(widget)
            primary_combo.currentIndexChanged.connect(
                lambda index, w=widget: self._on_primary_changed(w, index)
            )
        elif widget_type == "checkbox":
            widget = QCheckBox(schema_item.get("label", ""))
            widget.setChecked(bool(default_value))
        elif widget_type == "note":
            widget = QLabel(schema_item.get("label", ""))
            widget.setWordWrap(True)
            widget.setTextInteractionFlags(
                widget.textInteractionFlags() | Qt.TextSelectableByMouse)
            widget.setStyleSheet(
                "QLabel { padding: 8px 10px; border: none; "
                "border-left: 3px solid palette(highlight); "
                "border-radius: 3px; background: palette(alternate-base); }"
            )

        tooltip = schema_item.get("tooltip")
        if tooltip and not isinstance(widget, dict) and widget is not None:
            widget.setToolTip(tooltip)
        return widget

    def _populate_nested(self, widget_dict):
        """Fills the primary dropdown with the model's provider groups."""
        primary_combo = widget_dict['primary']
        primary_combo.blockSignals(True)
        try:
            primary_combo.clear()
            provider_models = self._models_by_provider()
            for provider_name in sorted(provider_models):
                primary_combo.addItem(provider_name, userData=provider_name)
        finally:
            primary_combo.blockSignals(False)
        if primary_combo.count():
            self._on_primary_changed(widget_dict, 0)

    def _on_primary_changed(self, widget_dict, index):
        primary_combo = widget_dict['primary']
        secondary_combo = widget_dict['secondary']
        secondary_combo.clear()
        provider_key = primary_combo.itemData(index)
        if not provider_key:
            return
        models = self._models_by_provider().get(provider_key, [])
        secondary_combo.addItems(sorted(models))

    def _models_by_provider(self) -> dict:
        provider_config = ModelConfigManager.PROVIDER_CONFIG.get(
            self.provider_id, {})
        return provider_config.get("models_by_provider", {}) or {}

    # --- values -----------------------------------------------------------

    def load(self, values: dict):
        """Loads stored values into the fields."""
        values = values or {}
        for key, widget in self.fields.items():
            value = values.get(key)
            if value is None:
                continue
            field_type = self.schema.get(key, {}).get("type")
            if field_type in ("password", "string"):
                widget.setText(str(value))
            elif field_type == "dropdown":
                widget.setCurrentText(str(value))
            elif field_type == "checkbox":
                widget.setChecked(bool(value))
            elif field_type == "nested_dropdown" and '/' in str(value):
                primary_val, secondary_val = str(value).split('/', 1)
                primary_combo = widget['primary']
                index = primary_combo.findData(primary_val)
                if index == -1:
                    # Provider defaults spell the group lowercase while the
                    # dropdown uses models.json display names ("anthropic" vs
                    # "Anthropic"). A group that is gone from the cache is
                    # kept as a one-off item so the stored id is not replaced.
                    for candidate in range(primary_combo.count()):
                        data = primary_combo.itemData(candidate)
                        if str(data).casefold() == primary_val.casefold():
                            index = candidate
                            break
                if index == -1:
                    primary_combo.addItem(primary_val, primary_val)
                    index = primary_combo.count() - 1
                if index != -1:
                    primary_combo.setCurrentIndex(index)
                    # The secondary dropdown is populated by the signal, so
                    # set its value after the pending update runs.
                    QTimer.singleShot(
                        0, lambda w=widget['secondary'], v=secondary_val:
                        w.setCurrentText(v))

    def values(self) -> dict:
        """Current field values merged over the provider defaults."""
        settings = dict(self.defaults)
        for key, widget in self.fields.items():
            field_type = self.schema.get(key, {}).get("type")
            if field_type in ("password", "string"):
                settings[key] = widget.text()
            elif field_type == "dropdown":
                settings[key] = widget.currentText()
            elif field_type == "checkbox":
                settings[key] = widget.isChecked()
            elif field_type == "nested_dropdown":
                primary_combo = widget['primary']
                secondary_combo = widget['secondary']
                primary_val = primary_combo.itemData(
                    primary_combo.currentIndex())
                secondary_val = secondary_combo.currentText()
                if primary_val and secondary_val:
                    settings[key] = f"{primary_val}/{secondary_val}"
        return settings

    def repopulate_models(self):
        """Rebuilds model dropdowns from the refreshed model cache."""
        for key, widget in self.fields.items():
            field_type = self.schema.get(key, {}).get("type")
            if field_type == "dropdown":
                populate_model_dropdown(widget, self.provider_id)
            elif field_type == "nested_dropdown":
                self._populate_nested(widget)

    def api_key_widget(self):
        widget = self.fields.get("api_key")
        return widget if isinstance(widget, QLineEdit) else None


def populate_model_dropdown(combo: QComboBox, provider_id: str):
    """Replaces a model dropdown's items with the cached model list."""
    combo.clear()
    provider_config = ModelConfigManager.PROVIDER_CONFIG.get(provider_id, {})
    models = provider_config.get("models", [])
    combo.addItems(sorted(models))
