# src/ui/settings/llm_provider_widget.py
"""
This module contains the LLMProviderWidget, a self-contained component
for selecting and configuring an LLM provider. It dynamically generates
UI elements based on the schemas provided by each discovered LLM provider.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QComboBox, QFormLayout,
    QPushButton, QMessageBox, QStackedWidget
)

from llm import PROVIDERS
from llm.model_config_manager import ModelConfigManager
from src.ui.widgets.provider_form import ProviderForm


class LLMProviderWidget(QWidget):
    """A widget for selecting and configuring LLM providers."""

    def __init__(self, settings_manager, show_update_button=True, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.model_config_manager = ModelConfigManager()
        self.show_update_button = show_update_button

        self.forms = {}  # { "provider_id": ProviderForm }
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
        sorted_providers = sorted(
            PROVIDERS.items(), key=lambda item: item[1].get_display_name())

        for provider_id, provider_class in sorted_providers:
            provider_config = provider_class.get_provider_config()
            if not provider_config.get("show_in_ui", True):
                continue

            self.provider_combo.addItem(
                provider_class.get_display_name(), userData=provider_id)
            form = ProviderForm(provider_id)
            self.forms[provider_id] = form
            self.provider_stack.addWidget(form)

    def _on_provider_changed(self, index):
        """Switches the visible settings page."""
        provider_id = self.provider_combo.itemData(index)

        if provider_id and provider_id == self.current_provider_id:
            return  # Avoid unnecessary updates

        self.current_provider_id = provider_id
        form = self.forms.get(provider_id)
        if form is not None:
            self.provider_stack.setCurrentWidget(form)

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

    def rebuild_provider_pages(self):
        """Recreates pages after the provider registry changed.

        Unsaved edits from the current pages are kept and laid over the stored
        settings, so adding or deleting a custom provider never wipes values
        the user already typed but has not saved yet.
        """
        unsaved = self.get_all_settings()[1] if self.forms else {}
        current_id = self.provider_combo.currentData()
        stored = self.settings_manager.get("llm_providers", {}) or {}

        self.provider_combo.blockSignals(True)
        try:
            self.provider_combo.clear()
            while self.provider_stack.count():
                page = self.provider_stack.widget(0)
                self.provider_stack.removeWidget(page)
                page.deleteLater()
            self.forms.clear()
            self._create_provider_ui_pages()

            for provider_id, form in self.forms.items():
                values = dict(stored.get(provider_id) or {})
                values.update(unsaved.get(provider_id) or {})
                form.load(values)

            index = self.provider_combo.findData(current_id)
            self.provider_combo.setCurrentIndex(index if index != -1 else 0)
        finally:
            self.provider_combo.blockSignals(False)

        self.current_provider_id = None
        self._on_provider_changed(self.provider_combo.currentIndex())

    def repopulate_all_dropdowns(self):
        """Repopulates all model dropdowns with fresh data."""
        for form in self.forms.values():
            form.repopulate_models()

    def load_initial_settings(self):
        """Loads settings from the settings manager when the widget is created."""
        all_settings = self.settings_manager.all()
        active_provider_id = all_settings.get('active_provider')
        provider_settings = all_settings.get('llm_providers', {})

        for provider_id, settings in provider_settings.items():
            if provider_id in self.forms:
                self.load_settings(provider_id, settings)

        if active_provider_id:
            index = self.provider_combo.findData(active_provider_id)
            if index != -1:
                self.provider_combo.setCurrentIndex(index)

        # Trigger initial UI update
        self._on_provider_changed(self.provider_combo.currentIndex())

    def load_settings(self, provider_id: str, settings: dict):
        """Loads a dictionary of settings into the UI for a specific provider."""
        form = self.forms.get(provider_id)
        if form is not None:
            form.load(settings)

    def get_all_settings(self) -> tuple[str, dict]:
        """Returns the active provider ID and a dictionary of all provider settings."""
        stored = self.settings_manager.get("llm_providers", {}) or {}
        all_settings = {}
        for provider_id, form in self.forms.items():
            current_settings = form.values()
            # Per-model reasoning effort choices have no field on this page;
            # carry them over so saving settings never wipes them.
            stored_efforts = (
                stored.get(provider_id) or {}).get("reasoning_efforts")
            if isinstance(stored_efforts, dict) and stored_efforts:
                current_settings["reasoning_efforts"] = dict(stored_efforts)
            all_settings[provider_id] = current_settings

        active_provider_id = self.provider_combo.currentData()
        return active_provider_id, all_settings

    def get_current_api_key_widget(self):
        """Returns the API key widget for the currently selected provider."""
        form = self.forms.get(self.provider_combo.currentData())
        return form.api_key_widget() if form is not None else None
