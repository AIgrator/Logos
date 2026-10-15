"""
This module contains the initial setup wizard for the application.
It guides the user through the essential first-time configuration steps.
"""
import os
import sys
import logging
import json
from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QStackedWidget,
    QLabel, QWidget, QMessageBox, QCheckBox
)
from PySide6.QtGui import QIcon

# Assuming settings_manager is available in the path
from src.ui.widgets.llm_provider_widget import LLMProviderWidget
from src.ui.theme_manager import ThemeManager, DARK_THEME
from llm.model_config_manager import ModelConfigManager


class InitialSetupWizard(QDialog):
    """A step-by-step wizard to configure the application for the first time."""
    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Initial Setup")
        self.setWindowIcon(QIcon("assets/ico.ico"))
        self.setModal(True)
        self.setMinimumWidth(500)

        # --- Pre-UI Setup: Load latest models into cache ---
        # This is done synchronously before the UI is built to ensure the
        # LLMProviderWidget is populated with the most up-to-date model list.
        logging.info("Initial setup: Caching latest model list...")
        success, message = ModelConfigManager.update_models_from_source()
        if not success:
            QMessageBox.critical(
                self,
                "Initialization Error",
                f"Failed to download the initial model list, which is required for setup. Please check your internet connection and restart the application.\n\nError: {message}"
            )
            # Schedule the dialog to close immediately after this method finishes
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self.reject)
            return
        # --- End Pre-UI Setup ---

        self.settings_manager = settings_manager # Use the passed-in instance
        self.theme_manager = ThemeManager(self.settings_manager)

        self.layout = QVBoxLayout(self)
        self.stacked_widget = QStackedWidget()

        # --- Create Navigation Buttons First ---
        # This is critical because page creation methods might trigger `update_button_states`
        # which needs these buttons to exist.
        self.nav_layout = QHBoxLayout()
        self.prev_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")

        self.prev_button.clicked.connect(self.go_to_previous_page)
        self.next_button.clicked.connect(self.go_to_next_page)
        # --- End Button Creation ---

        self.create_llm_page()
        self.create_final_choices_page()

        self.layout.addWidget(self.stacked_widget)

        self.nav_layout.addStretch()
        self.nav_layout.addWidget(self.prev_button)
        self.nav_layout.addWidget(self.next_button)
        self.layout.addLayout(self.nav_layout)

        self.stacked_widget.setCurrentIndex(0)
        self.update_button_states()

    def create_llm_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel("<h3>Step 1 of 2: Configure AI Provider</h3>"
                       "Welcome! Please select your preferred AI provider and enter your API key to begin.")
        label.setWordWrap(True)
        layout.addWidget(label)

        # Instantiate SettingsManager before passing it to the widget
        self.llm_widget = LLMProviderWidget(self.settings_manager, show_update_button=False)

        # --- Dynamic Signal Connection for Real-time Validation ---
        # This attribute will hold the currently visible API key widget.
        self.current_api_key_widget = None

        def on_provider_changed():
            """On provider change, reconnect the textChanged signal to the new widget."""
            # Disconnect from the old widget to prevent multiple signals
            if self.current_api_key_widget:
                try:
                    self.current_api_key_widget.textChanged.disconnect(self.update_button_states)
                except (RuntimeError, TypeError):
                    pass  # Signal was not connected or widget is gone

            # Get the new widget and store a reference to it
            self.current_api_key_widget = self.llm_widget.get_current_api_key_widget()

            # Connect the textChanged signal for the new widget
            if self.current_api_key_widget:
                self.current_api_key_widget.textChanged.connect(self.update_button_states)

            # Immediately update the button state
            self.update_button_states()

        # Connect the handler and call it once for initial setup
        self.llm_widget.provider_combo.currentIndexChanged.connect(on_provider_changed)
        on_provider_changed()

        layout.addWidget(self.llm_widget)
        self.stacked_widget.addWidget(page)

    def create_final_choices_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel("<h3>Step 2 of 2: Final Setup Options</h3>"
                       "Your application is almost ready! Please select additional setup options:")
        label.setWordWrap(True)
        layout.addWidget(label)

        self.create_global_command_checkbox = QCheckBox("Create global 'logos' command (recommended)")
        self.create_global_command_checkbox.setChecked(True) # Default to checked
        layout.addWidget(self.create_global_command_checkbox)

        self.create_desktop_shortcut_checkbox = QCheckBox("Create a desktop shortcut")
        self.create_desktop_shortcut_checkbox.setChecked(True) # Default to checked
        layout.addWidget(self.create_desktop_shortcut_checkbox)

        # Platform-specific checkbox
        if sys.platform == "win32":
            self.platform_specific_checkbox = QCheckBox("Add 'Open with Logos' to Windows right-click menu (for folders)")
        elif sys.platform == "linux":
            self.platform_specific_checkbox = QCheckBox("Create Desktop Application Entry (.desktop file)")
        else:
            self.platform_specific_checkbox = None # No option for other OSes

        if self.platform_specific_checkbox:
            self.platform_specific_checkbox.setChecked(True) # Default to checked
            layout.addWidget(self.platform_specific_checkbox)

        layout.addStretch()
        self.stacked_widget.addWidget(page)

    def go_to_next_page(self):
        current_index = self.stacked_widget.currentIndex()

        if current_index == 0: # After LLM page, go to Final Choices
            self.stacked_widget.setCurrentIndex(current_index + 1)
        elif current_index == 1: # After Final Choices page, finish setup
            self._save_setup_choices()
            self.accept() # Close the wizard successfully

        self.update_button_states()

    def go_to_previous_page(self):
        current_index = self.stacked_widget.currentIndex()
        if current_index > 0:
            self.stacked_widget.setCurrentIndex(current_index - 1)
        self.update_button_states()

    def _save_setup_choices(self):
        """Saves the user's choices for global command and context menu/desktop entry to a JSON file."""
        choices = {
            "create_global_command": self.create_global_command_checkbox.isChecked(),
            "create_desktop_shortcut": self.create_desktop_shortcut_checkbox.isChecked(),
        }

        # Add platform-specific choice
        if self.platform_specific_checkbox:
            if sys.platform == "win32":
                choices["add_context_menu"] = self.platform_specific_checkbox.isChecked()
            elif sys.platform == "linux":
                choices["create_desktop_entry"] = self.platform_specific_checkbox.isChecked()

        setup_choices_file = os.path.join(os.getcwd(), "setup_choices.json")
        try:
            with open(setup_choices_file, "w", encoding="utf-8") as f:
                json.dump(choices, f, indent=4)
            logging.info(f"Setup choices saved to {setup_choices_file}")
        except Exception as e:
            logging.error(f"Failed to save setup choices to {setup_choices_file}: {e}", exc_info=True)

    def update_button_states(self):
        current_index = self.stacked_widget.currentIndex()
        total_pages = self.stacked_widget.count()

        self.prev_button.setVisible(current_index > 0)

        if current_index == total_pages - 1:
            self.next_button.setText("Finish")
        else:
            self.next_button.setText("Next")

        self.next_button.setVisible(True)

        # --- Context-aware button enabling ---
        if current_index == 0:
            # On the LLM page, check for a valid API key using the new direct method
            api_key_widget = self.llm_widget.get_current_api_key_widget()
            api_key = api_key_widget.text() if api_key_widget else ""
            self.next_button.setEnabled(bool(api_key and api_key.strip()))
        else:
            # On all other pages, the button is enabled by default
            self.next_button.setEnabled(True)
    def get_settings(self):
        active_provider, providers_from_widget = self.llm_widget.get_all_settings()

        final_settings = self.settings_manager.all().copy()
        final_settings['active_provider'] = active_provider

        # Deep merge the provider settings from the widget into the defaults.
        # This ensures we only update fields the user has edited (e.g., api_key, model)
        # while preserving the complete structure for all other providers and settings.
        for provider_name, user_settings in providers_from_widget.items():
            if provider_name in final_settings['llm_providers']:
                final_settings['llm_providers'][provider_name].update(user_settings)

        return final_settings

def run_setup_wizard(settings_manager):
    """
    Runs the initial setup wizard, passing it the main settings manager.
    Args:
        settings_manager (SettingsManager): The main instance of the settings manager.

    Returns:
        bool: True if setup was completed, False if cancelled.
    """
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    # --- Apply theme before showing the wizard ---
    theme_manager = ThemeManager(settings_manager)
    # The theme should have been detected and set in settings_manager by main.py
    # before this function is called. We default to dark if it's somehow missing.
    theme_to_apply = settings_manager.get("theme", DARK_THEME)
    theme_manager.apply_theme(theme_to_apply)
    # --- End theme application ---

    wizard = InitialSetupWizard(settings_manager)
    result = wizard.exec()

    if result == QDialog.Accepted:
        settings = wizard.get_settings()
        # Use the central settings manager to apply and save the new settings.
        # This ensures all components are notified and keyring is used correctly.
        settings_manager.set_many(settings)
        print("Setup complete. Settings have been saved.")
        return True

    print("Setup was cancelled by the user.")
    return False
