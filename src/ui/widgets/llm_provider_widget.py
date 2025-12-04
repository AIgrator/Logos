# src/ui/settings/llm_provider_widget.py
"""
This module contains the LLMProviderWidget, a self-contained component
for selecting and configuring an LLM provider. It dynamically generates
UI elements based on the schemas provided by each discovered LLM provider.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QComboBox, QFormLayout, QLineEdit,
    QCheckBox, QPushButton, QMessageBox, QStackedWidget
)
from PySide6.QtCore import QTimer

from llm import PROVIDERS
from llm.model_config_manager import ModelConfigManager

class LLMProviderWidget(QWidget):
    """A widget for selecting and configuring LLM providers."""

    def __init__(self, settings_manager, show_update_button=True, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.model_config_manager = ModelConfigManager()
        self.show_update_button = show_update_button

        self.widgets = {} # { "provider_id": { "setting_key": widget, ... } }
        self.provider_schemas = {} # { "provider_id": schema }
        self.current_provider_id = None

        self._setup_ui()
        self.load_initial_settings()

    def _setup_ui(self):
        """Initializes the user interface for the widget."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Provider selection dropdown
        provider_layout = QFormLayout()
        self.provider_combo = QComboBox()
        provider_layout.addRow("AI Provider:", self.provider_combo)
        main_layout.addLayout(provider_layout)

        # Stacked widget for provider-specific settings
        self.provider_stack = QStackedWidget()
        main_layout.addWidget(self.provider_stack)

        # Dynamically create UI for all providers
        self._create_provider_ui_pages()

        # Connect signals after everything is set up
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        # Update models button
        self.update_models_button = QPushButton("Update Models")
        self.update_models_button.clicked.connect(self._on_update_models_clicked)
        self.update_models_button.setVisible(self.show_update_button)
        main_layout.addWidget(self.update_models_button)

    def _create_provider_ui_pages(self):
        """Generates a UI page in the QStackedWidget for each provider."""
        sorted_providers = sorted(PROVIDERS.items(), key=lambda item: item[1].get_display_name())

        for provider_id, provider_class in sorted_providers:
            provider_config = provider_class.get_provider_config()
            if not provider_config.get("show_in_ui", True):
                continue

            self.provider_schemas[provider_id] = provider_class.get_ui_schema()
            self.provider_combo.addItem(provider_class.get_display_name(), userData=provider_id)

            page = QWidget()
            form_layout = QFormLayout(page)
            form_layout.setContentsMargins(0, 10, 0, 0)
            self.widgets[provider_id] = {}

            default_settings = provider_class.get_default_settings()
            ui_schema = self.provider_schemas[provider_id]

            for key, schema_item in ui_schema.items():
                default_value = default_settings.get(key)
                widget = self._create_widget_for_schema(provider_id, key, schema_item, default_value)
                if widget:
                    self.widgets[provider_id][key] = widget

                    label = schema_item.get("label", key)
                    if schema_item.get("type") == "checkbox":
                        form_layout.addRow(widget)
                    elif schema_item.get("type") == "nested_dropdown":
                        form_layout.addRow(schema_item.get("primary_dropdown_label", "Provider"), widget['primary'])
                        form_layout.addRow(schema_item.get("secondary_dropdown_label", "Model"), widget['secondary'])
                    else:
                        form_layout.addRow(label, widget)

            self.provider_stack.addWidget(page)

    def _create_widget_for_schema(self, provider_id, key, schema_item, default_value):
        """Creates a widget based on a schema definition."""
        widget_type = schema_item.get("type")
        widget = None

        if widget_type == "password":
            widget = QLineEdit()
            widget.setEchoMode(QLineEdit.Password)
        elif widget_type == "string":
            widget = QLineEdit(str(default_value) if default_value is not None else "")
        elif widget_type == "dropdown":
            widget = QComboBox()
            self._update_dropdown_model_list(widget, provider_id)
        elif widget_type == "nested_dropdown":
            primary_combo = QComboBox()
            secondary_combo = QComboBox()
            widget = {'primary': primary_combo, 'secondary': secondary_combo}
            self._update_nested_dropdown_model_list(widget, provider_id)
            primary_combo.currentIndexChanged.connect(
                lambda index, w=widget, p_id=provider_id: self._update_secondary_dropdown(w, p_id, index)
            )
        elif widget_type == "checkbox":
            widget = QCheckBox(schema_item.get("label", key))
            widget.setChecked(bool(default_value))

        return widget

    def _update_dropdown_model_list(self, combo, provider_id):
        """Populates a simple dropdown with models."""
        combo.clear()
        provider_config = ModelConfigManager.PROVIDER_CONFIG.get(provider_id, {})
        models = provider_config.get("models", [])
        combo.addItems(sorted(models))

    def _update_nested_dropdown_model_list(self, widget_dict, provider_id):
        """Populates the primary dropdown for a nested model selector."""
        primary_combo = widget_dict['primary']
        primary_combo.clear()

        provider_config = ModelConfigManager.PROVIDER_CONFIG.get(provider_id, {})
        provider_models = provider_config.get("models_by_provider", {})

        sorted_providers = sorted(provider_models.keys())
        for provider_name in sorted_providers:
            primary_combo.addItem(provider_name, userData=provider_name) # Use userData for the key

        if sorted_providers:
            self._update_secondary_dropdown(widget_dict, provider_id, 0)

    def _update_secondary_dropdown(self, widget_dict, provider_id, index):
        """Populates the secondary dropdown based on the primary's selection."""
        primary_combo = widget_dict['primary']
        secondary_combo = widget_dict['secondary']
        secondary_combo.clear()

        provider_key = primary_combo.itemData(index)
        if not provider_key:
            return

        provider_config = ModelConfigManager.PROVIDER_CONFIG.get(provider_id, {})
        provider_specific_models = provider_config.get("models_by_provider", {})
        models = provider_specific_models.get(provider_key, [])

        secondary_combo.addItems(sorted(models))

    def _on_provider_changed(self, index):
        """Switches the visible settings page."""
        provider_id = self.provider_combo.itemData(index)

        if provider_id and provider_id == self.current_provider_id:
            return # Avoid unnecessary updates

        self.current_provider_id = provider_id

        # Find the correct page index
        for i in range(self.provider_stack.count()):
            # This relies on the fact that we added pages and combo items in the same sorted order
            if self.provider_combo.itemData(i) == provider_id:
                self.provider_stack.setCurrentIndex(i)
                break

    def _on_update_models_clicked(self):
        """Handles updating the model list from the source."""
        self.update_models_button.setEnabled(False)
        self.update_models_button.setText("Updating...")

        success, message = self.model_config_manager.update_models_from_source()

        if success:
            QMessageBox.information(self, "Success", "Model list updated successfully.")
            # Just need to repopulate dropdowns, not recreate the whole UI
            self.repopulate_all_dropdowns()
        else:
            QMessageBox.information(self, "Error", f"Failed to update model list:\n{message}")

        self.update_models_button.setEnabled(True)
        self.update_models_button.setText("Update Models")

    def repopulate_all_dropdowns(self):
        """Repopulates all model dropdowns with fresh data."""
        for provider_id, widgets in self.widgets.items():
            schema = self.provider_schemas.get(provider_id, {})
            for key, schema_item in schema.items():
                widget = widgets.get(key)
                if not widget:
                    continue

                if schema_item.get("type") == "dropdown":
                    self._update_dropdown_model_list(widget, provider_id)
                elif schema_item.get("type") == "nested_dropdown":
                    self._update_nested_dropdown_model_list(widget, provider_id)

    def load_initial_settings(self):
        """Loads settings from the settings manager when the widget is created."""
        all_settings = self.settings_manager.all()
        active_provider_id = all_settings.get('active_provider')
        provider_settings = all_settings.get('llm_providers', {})

        for provider_id, settings in provider_settings.items():
            if provider_id in self.widgets:
                self.load_settings(provider_id, settings)

        if active_provider_id:
            index = self.provider_combo.findData(active_provider_id)
            if index != -1:
                self.provider_combo.setCurrentIndex(index)

        # Trigger initial UI update
        self._on_provider_changed(self.provider_combo.currentIndex())

    def load_settings(self, provider_id: str, settings: dict):
        """Loads a dictionary of settings into the UI for a specific provider."""
        if provider_id not in self.widgets:
            return

        schema = self.provider_schemas.get(provider_id, {})
        for key, widget in self.widgets[provider_id].items():
            value = settings.get(key)
            if value is None:
                continue

            schema_item = schema.get(key, {})
            widget_type = schema_item.get("type")

            if widget_type in ["password", "string"]:
                widget.setText(str(value))
            elif widget_type == "dropdown":
                widget.setCurrentText(str(value))
            elif widget_type == "checkbox":
                widget.setChecked(bool(value))
            elif widget_type == "nested_dropdown":
                if value and '/' in value:
                    primary_val, secondary_val = value.split('/', 1)
                    primary_combo = widget['primary']
                    index = primary_combo.findData(primary_val)
                    if index != -1:
                        primary_combo.setCurrentIndex(index)
                        # The secondary dropdown is populated by the signal,
                        # so we set its value after a short delay.
                        QTimer.singleShot(0, lambda w=widget['secondary'], v=secondary_val: w.setCurrentText(v))

    def get_all_settings(self) -> tuple[str, dict]:
        """Returns the active provider ID and a dictionary of all provider settings."""
        all_settings = {}
        for provider_id, widgets in self.widgets.items():
            provider_class = PROVIDERS.get(provider_id)
            if not provider_class:
                continue

            # Start with defaults to ensure all keys are present
            current_settings = provider_class.get_default_settings()
            schema = self.provider_schemas.get(provider_id, {})

            for key, widget in widgets.items():
                schema_item = schema.get(key, {})
                widget_type = schema_item.get("type")

                if widget_type in ["password", "string"]:
                    current_settings[key] = widget.text()
                elif widget_type == "dropdown":
                    current_settings[key] = widget.currentText()
                elif widget_type == "checkbox":
                    current_settings[key] = widget.isChecked()
                elif widget_type == "nested_dropdown":
                    primary_combo = widget['primary']
                    secondary_combo = widget['secondary']
                    primary_val = primary_combo.itemData(primary_combo.currentIndex())
                    secondary_val = secondary_combo.currentText()
                    if primary_val and secondary_val:
                        current_settings[key] = f"{primary_val}/{secondary_val}"

            all_settings[provider_id] = current_settings

        active_provider_id = self.provider_combo.currentData()
        return active_provider_id, all_settings

    def get_current_api_key_widget(self):
        """Returns the API key widget for the currently selected provider."""
        provider_id = self.provider_combo.currentData()
        if provider_id and provider_id in self.widgets:
            return self.widgets[provider_id].get("api_key")
        return None
