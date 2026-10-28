"""
This module defines the settings window for the application.
"""
import copy
import os
import logging

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QWidget,
    QDialogButtonBox,
    QLabel,
    QHBoxLayout,
    QGroupBox,
    QFormLayout,
    QGridLayout,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QListWidget,
    QLineEdit,
    QListWidgetItem,
    QButtonGroup,
    QStackedWidget,
    QScrollArea,
    QToolButton,
    QFrame,
    QRadioButton,
    QMessageBox,
    QPlainTextEdit,
)
from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QIcon, QPalette
from src.ui.theme_manager import (
    LIGHT_THEME,
    DARK_THEME,
    create_dark_palette,
    create_light_palette,
)

from settings_manager import SettingsManager
from llm import PROVIDERS, providers_ready, start_provider_warmup
from build_info import APP_VERSION
from src.ui.widgets.font_manager.font_combobox import FontComboBox
from src.ui.widgets.hotkey.hotkey_edit import HotkeyEdit
from src.apps.installed_apps import AppScanner
from src.ui.widgets.llm_provider_widget import LLMProviderWidget
from src.ui.widgets.mode_model_widget import ModeModelWidget
from src.ui.widgets.custom_provider_dialog import CustomProviderDialog
from src.ui.utils import CustomToolTipManager, NavButtonHoverFilter

from src.ui.widgets.busy_spinner import BusySpinner

CONTEXT_SETTINGS_DOC_URL = (
    "https://github.com/AIgrator/Logos/blob/main/docs/context_settings.md")
PROJECT_URL = "https://github.com/AIgrator/Logos"
ISSUES_URL = f"{PROJECT_URL}/issues"
LICENSE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "LICENSE")


def _read_license_text() -> str:
    try:
        with open(LICENSE_FILE, encoding="utf-8") as license_file:
            return license_file.read().strip()
    except OSError as e:
        logging.warning(f"Could not read license file {LICENSE_FILE}: {e}")
        return "MIT License"


class SettingsWindow(QDialog):

    def __init__(self, settings_manager: SettingsManager, controller, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.controller = controller
        self.setWindowTitle("Settings")
        self.setWindowIcon(QIcon("assets/ico.ico"))
        self.setMinimumWidth(400)
        self.setModal(True)

        self.tooltip_manager = CustomToolTipManager(self)
        self._context_doc_links = []
        # Custom providers are applied to the live settings as the user edits
        # them (so provider pages and model pickers stay in sync). The snapshot
        # lets Cancel roll all of it back.
        self._custom_snapshot = None
        self._deleted_custom_ids = set()

        # Main layout
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 10)
        self.layout.setSpacing(0)

        # Content layout
        content_layout = QHBoxLayout()
        content_layout.setSpacing(0)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # Navigation bar on the left
        self.nav_bar = self._create_nav_bar()
        content_layout.addWidget(self.nav_bar)

        # Separator line
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        content_layout.addWidget(line)

        # Section pages on the right (one visible at a time)
        _, self.sections = self._create_content_area()
        content_layout.addWidget(self.section_stack)

        self.layout.addLayout(content_layout)

        # OK and Cancel buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.save_and_accept)
        self.button_box.rejected.connect(self.reject)
        self.layout.addWidget(self.button_box)

        # Defer height adjustment to ensure all widgets are initialized
        QTimer.singleShot(0, self._adjust_window_height)

        self.load_settings()

        self.controller.theme_manager.theme_changed.connect(self._update_icon_colors)
        self.controller.theme_manager.theme_changed.connect(self._refresh_context_doc_links)
        self._update_icon_colors()

    def _update_icon_colors(self):
        """Updates all icon colors based on the current theme."""
        # The NavButtonHoverFilter will handle nav button icon updates automatically.
        pass

    def _update_all_nav_button_icons(self):
        """Forces an update of all navigation button icons."""
        for button in self.nav_button_group.buttons():
            # The event filter is a child of the button
            filter_instance = button.findChild(NavButtonHoverFilter)
            if filter_instance:
                filter_instance.force_update()

    def _adjust_window_height(self):
        """Adjusts the window height to fit the navigation bar and buttons."""
        nav_height = self.nav_bar.sizeHint().height()
        button_box_height = self.button_box.sizeHint().height()
        main_margins = self.layout.contentsMargins()

        # Calculate total height with some padding
        total_height = nav_height + button_box_height + main_margins.top() + main_margins.bottom() + 20

        self.setFixedHeight(total_height)

    def _create_nav_bar(self):
        """Creates the left-side navigation bar with icon buttons."""
        nav_widget = QWidget()
        nav_layout = QVBoxLayout(nav_widget)
        nav_layout.setContentsMargins(5, 10, 5, 10)
        nav_layout.setSpacing(5)
        nav_layout.setAlignment(Qt.AlignTop)

        self.nav_button_group = QButtonGroup(self)
        self.nav_button_group.setExclusive(True)

        # Define sections: icon, tooltip, and a placeholder for the widget
        self.nav_sections = {
            "general": ("fa5s.cog", "General"),
            "custom": ("fa5s.plug", "Custom Providers"),
            "common": ("fa5s.cogs", "Common"),
            "hotkeys": ("fa5s.keyboard", "Hotkeys"),
            "browser": ("fa5s.globe", "Browser"),
            "terminal": ("fa5s.terminal", "Terminal"),
            "vision": ("fa5s.robot", "Vision"),
            "compact": ("fa5s.compress-arrows-alt", "Compact Mode"),
            "apps": ("fa5s.th-large", "Applications"),
            "context": ("fa5s.file-alt", "Context"),
            "about": ("fa5s.info-circle", "About"),
        }

        for key, (icon_name, tooltip) in self.nav_sections.items():
            if key == "about":
                # Keep About pinned to the bottom of the navigation bar.
                nav_layout.addStretch(1)
            button = QToolButton()
            # The icon is now set and managed by the NavButtonHoverFilter
            button.setIconSize(QSize(24, 24))
            button.setCheckable(True)
            button.setProperty("section_key", key)
            button.setStyleSheet("""
                QToolButton { border: none; padding: 5px; border-radius: 4px; }
                QToolButton:checked { background-color: palette(highlight); }
            """)

            # Install the new filter to manage icon colors for different states
            button.installEventFilter(NavButtonHoverFilter(self.controller.theme_manager, icon_name, parent=button))

            self.tooltip_manager.install(button, tooltip)
            nav_layout.addWidget(button)
            self.nav_button_group.addButton(button)

        self.nav_button_group.buttonClicked.connect(self._on_nav_button_clicked)

        return nav_widget

    def _create_content_area(self):
        """Creates the right-side area: one independent page per section.

        Each nav button shows ONLY its section (QStackedWidget). Every page
        is wrapped in its own scroll area whose vertical bar appears only
        when that section doesn't fit the height (AsNeeded) — no shared
        scrollbar anymore.
        """
        sections = {
            "general": self._create_general_section(),
            "custom": self._create_custom_providers_section(),
            "common": self._create_common_section(),
            "hotkeys": self._create_hotkeys_section(),
            "browser": self._create_browser_section(),
            "terminal": self._create_terminal_section(),
            "vision": self._create_vision_section(),
            "compact": self._create_compact_mode_section(),
            "apps": self._create_apps_section(),
            "context": self._create_context_section(),
            "about": self._create_about_section(),
        }

        self.section_stack = QStackedWidget()
        self.section_order = []
        for key in self.nav_sections:
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QFrame.NoFrame)
            page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            page.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            holder = QWidget()
            holder_layout = QVBoxLayout(holder)
            holder_layout.setContentsMargins(15, 15, 15, 15)
            holder_layout.setAlignment(Qt.AlignTop)
            holder_layout.addWidget(sections[key])
            page.setWidget(holder)
            self.section_stack.addWidget(page)
            self.section_order.append(key)

        self.section_stack.setMinimumWidth(400)
        return self.section_stack, sections

    def _on_nav_button_clicked(self, button):
        """Shows only the selected section's page."""
        section_key = button.property("section_key")
        if section_key in self.section_order:
            self.section_stack.setCurrentIndex(
                self.section_order.index(section_key))

        self._update_all_nav_button_icons()

    def showEvent(self, event):
        """Reload settings and set initial state when shown."""
        self.load_settings()
        # Set the first button as checked by default
        first_button = self.nav_button_group.buttons()[0]
        first_button.setChecked(True)
        self._on_nav_button_clicked(first_button)
        if not getattr(self, "_fonts_scheduled", False):
            # Registering every OS font with Qt takes ~0.8 s; do it after the
            # window is painted so opening Settings never waits for it.
            self._fonts_scheduled = True
            QTimer.singleShot(100, self.font_combo.populate_fonts)
        super().showEvent(event)

    def closeEvent(self, event):
        self._discard_custom_changes()
        super().closeEvent(event)

    def reject(self):
        self._discard_custom_changes()
        super().reject()

    def load_settings(self):
        """Loads settings from the manager and updates the UI."""
        # General
        active_provider = self.settings_manager.get("active_provider", "google_gemini")
        providers = self.settings_manager.get("llm_providers", {})
        if self.llm_provider_widget is not None:
            self.llm_provider_widget.load_settings(active_provider, providers)
        self.mode_model_widget.load_settings(self.settings_manager.get("mode_models", {}) or {})
        self._populate_custom_providers()

        # Common
        self.max_retries_spinbox.setValue(self.settings_manager.get("max_retries", 3))
        self.retry_delay_spinbox.setValue(self.settings_manager.get("retry_delay_seconds", 2))
        self.max_errors_spinbox.setValue(int(self.settings_manager.get("max_consecutive_errors", 3) or 3))
        self.prevent_multiple_instances_checkbox.setChecked(
            self.settings_manager.get("prevent_multiple_instances", True)
        )

        theme = self.settings_manager.get("theme", DARK_THEME)
        if theme == LIGHT_THEME:
            self.light_theme_radio.setChecked(True)
        else:
            self.dark_theme_radio.setChecked(True)

        # Browser (ad blocking defaults to ON)
        browser_settings = self.settings_manager.get("browser", {})
        self.block_ads_checkbox.setChecked(bool(browser_settings.get("block_ads", True)))
        self.browser_headless_checkbox.setChecked(bool(browser_settings.get("headless", False)))
        self.browser_timeout_spinbox.setValue(int(browser_settings.get("default_timeout_ms", 30000) or 30000))

        # Terminal (long tasks need a generous shell timeout)
        terminal_settings = self.settings_manager.get("terminal", {})
        self.shell_timeout_spinbox.setValue(int(terminal_settings.get("shell_timeout_seconds", 60) or 60))
        self.shell_detach_spinbox.setValue(int(terminal_settings.get("shell_detach_seconds", 25) or 25))

        # Vision (agent loop; default OFF)
        vision_cfg = self.settings_manager.get("vision", {})
        self.allow_shell_commands_checkbox.setChecked(bool(vision_cfg.get("allow_shell_commands", False)))
        self.allow_system_surfaces_checkbox.setChecked(bool(vision_cfg.get("allow_system_surfaces", False)))
        self.restrict_to_allowed_apps_checkbox.setChecked(bool(vision_cfg.get("restrict_to_allowed_apps", False)))
        self.max_turns_spinbox.setValue(int(vision_cfg.get("max_turns_per_task", 20) or 20))
        self.shot_width_spinbox.setValue(int(vision_cfg.get("screenshot_max_width", 1600) or 0))

        # Debug (keys persist under "omniparser" for now; the recognizer
        # service will adopt them later)
        omniparser_settings = self.settings_manager.get("omniparser", {})

        # Debug
        self.font_combo.set_selected_font(omniparser_settings.get("font_path", "default"))
        self.font_size_spinbox.setValue(omniparser_settings.get("font_size", 16))
        self.text_padding_spinbox.setValue(omniparser_settings.get("text_padding", 2))
        self.save_trace_log_checkbox.setChecked(omniparser_settings.get("save_trace_log", True))
        self.save_pre_intent_checkbox.setChecked(omniparser_settings.get("save_pre_intent_screenshot", True))
        self.save_post_checkbox.setChecked(omniparser_settings.get("save_post_screenshot", True))

        # Hotkeys
        hotkey_settings = self.settings_manager.get("hotkeys", {})
        self.toggle_mode_hotkey.set_key_sequence(hotkey_settings.get("toggle_mode", "Ctrl+Shift+A"))
        self.pause_resume_hotkey.set_key_sequence(hotkey_settings.get("pause_resume", "Ctrl+Alt+P"))
        self.stop_execution_hotkey.set_key_sequence(hotkey_settings.get("stop_execution", "Ctrl+Alt+S"))
        self.pause_for_feedback_hotkey.set_key_sequence(hotkey_settings.get("pause_for_feedback", "Ctrl+Alt+F"))
        self.cancel_request_hotkey.set_key_sequence(hotkey_settings.get("cancel_request", "Ctrl+Shift+X"))

        # Context (agent history compaction; all defaults = send everything)
        context_settings = self.settings_manager.get("context", {})
        self.history_keep_last_spinbox.setValue(context_settings.get("history_keep_last_n", 0))
        self.history_max_chars_spinbox.setValue(context_settings.get("history_max_result_chars", 0))
        self.history_dedup_checkbox.setChecked(bool(context_settings.get("history_dedup_snapshots", False)))
        self.history_trim_tracebacks_checkbox.setChecked(bool(context_settings.get("history_trim_tracebacks", False)))
        self.handoff_enabled_checkbox.setChecked(bool(context_settings.get("handoff_enabled", True)))
        self.handoff_mode_switch_checkbox.setChecked(bool(context_settings.get("handoff_on_mode_switch", True)))
        self.handoff_model_switch_checkbox.setChecked(bool(context_settings.get("handoff_on_model_switch", True)))
        self.auto_compact_checkbox.setChecked(bool(context_settings.get("auto_compact", True)))
        self.summary_max_tokens_spinbox.setValue(int(context_settings.get("summary_max_tokens", 1200)))
        self.summary_threshold_spinbox.setValue(float(context_settings.get("summary_threshold_ratio", 0.6)))
        self.summary_model_lineedit.setText(str(context_settings.get("summary_model") or ""))

        # Compact Mode
        compact_mode_settings = self.settings_manager.get("compact_mode", {})
        self.compact_mode_width_spinbox.setValue(compact_mode_settings.get("width", 700))
        self.compact_mode_height_spinbox.setValue(compact_mode_settings.get("height", 120))
        position = compact_mode_settings.get("position", "bottom_center")
        for button in self.position_button_group.buttons():
            if button.property("position_id") == position:
                button.setChecked(True)
                break

        self.load_apps_from_cache()

    def _collect_settings_from_ui(self):
        """Gathers all settings from the UI into a single dictionary."""
        if self.llm_provider_widget is not None:
            active_provider_to_save, providers_to_save = (
                self.llm_provider_widget.get_all_settings())
        else:
            # Provider UI is still loading: keep stored values instead of
            # wiping them with defaults from the placeholder.
            active_provider_to_save = self.settings_manager.get(
                "active_provider", "google_gemini")
            providers_to_save = dict(
                self.settings_manager.get("llm_providers", {}) or {})

        omniparser_settings = self.settings_manager.get("omniparser", {}).copy()

        omniparser_settings.update({
            "font_path": self.font_combo.get_selected_font_path(),
            "font_size": self.font_size_spinbox.value(),
            "text_padding": self.text_padding_spinbox.value(),
            "save_trace_log": self.save_trace_log_checkbox.isChecked(),
            "save_pre_intent_screenshot": self.save_pre_intent_checkbox.isChecked(),
            "save_post_screenshot": self.save_post_checkbox.isChecked(),
        })

        new_allowed_apps = [
            self.apps_list_widget.item(i).data(Qt.UserRole)
            for i in range(self.apps_list_widget.count())
            if self.apps_list_widget.item(i).checkState() == Qt.Checked
        ]

        hotkeys_to_save = {
            "toggle_mode": self.toggle_mode_hotkey.get_key_sequence(),
            "pause_resume": self.pause_resume_hotkey.get_key_sequence(),
            "stop_execution": self.stop_execution_hotkey.get_key_sequence(),
            "pause_for_feedback": self.pause_for_feedback_hotkey.get_key_sequence(),
            "cancel_request": self.cancel_request_hotkey.get_key_sequence()
        }

        browser_settings = {
            "block_ads": self.block_ads_checkbox.isChecked(),
            "headless": self.browser_headless_checkbox.isChecked(),
            "default_timeout_ms": self.browser_timeout_spinbox.value(),
        }
        # Preserve keys with no UI widget (e.g. profile_dir) so saving
        # settings never silently drops them.
        try:
            existing_browser = self.settings_manager.get("browser", {}) or {}
            for key, value in existing_browser.items():
                browser_settings.setdefault(key, value)
        except Exception:
            pass

        terminal_settings = {
            "shell_timeout_seconds": self.shell_timeout_spinbox.value(),
            "shell_detach_seconds": self.shell_detach_spinbox.value(),
        }
        # Preserve keys with no UI widget so saving never silently drops them.
        try:
            existing_terminal = self.settings_manager.get("terminal", {}) or {}
            for key, value in existing_terminal.items():
                terminal_settings.setdefault(key, value)
        except Exception:
            pass

        # Vision agent loop: single checkbox; the unified error policy
        # lives on the Agent tab. Preserve other stored keys so saving
        # never silently drops them.
        vision_settings = {
            "allow_shell_commands": self.allow_shell_commands_checkbox.isChecked(),
            "allow_system_surfaces": self.allow_system_surfaces_checkbox.isChecked(),
            "restrict_to_allowed_apps": self.restrict_to_allowed_apps_checkbox.isChecked(),
            "max_turns_per_task": self.max_turns_spinbox.value(),
            "screenshot_max_width": self.shot_width_spinbox.value(),
        }
        try:
            existing_vision = self.settings_manager.get("vision", {}) or {}
            for key, value in existing_vision.items():
                vision_settings.setdefault(key, value)
        except Exception:
            pass

        context_settings = {
            "history_keep_last_n": self.history_keep_last_spinbox.value(),
            "history_max_result_chars": self.history_max_chars_spinbox.value(),
            "history_dedup_snapshots": self.history_dedup_checkbox.isChecked(),
            "history_trim_tracebacks": self.history_trim_tracebacks_checkbox.isChecked(),
            "handoff_enabled": self.handoff_enabled_checkbox.isChecked(),
            "handoff_on_mode_switch": self.handoff_mode_switch_checkbox.isChecked(),
            "handoff_on_model_switch": self.handoff_model_switch_checkbox.isChecked(),
            "auto_compact": self.auto_compact_checkbox.isChecked(),
            "summary_max_tokens": self.summary_max_tokens_spinbox.value(),
            "summary_threshold_ratio": round(self.summary_threshold_spinbox.value(), 2),
            "summary_model": self.summary_model_lineedit.text().strip(),
        }
        # Preserve keys with no UI widget so saving never silently drops them.
        try:
            existing_context = self.settings_manager.get("context", {}) or {}
            for key, value in existing_context.items():
                context_settings.setdefault(key, value)
        except Exception:
            pass

        selected_button = self.position_button_group.checkedButton()
        position = selected_button.property("position_id") if selected_button else "bottom_center"

        compact_mode_settings = {
            "width": self.compact_mode_width_spinbox.value(),
            "height": self.compact_mode_height_spinbox.value(),
            "position": position
        }

        theme = LIGHT_THEME if self.light_theme_radio.isChecked() else DARK_THEME

        search_settings = {}

        return {
            "active_provider": active_provider_to_save,
            "llm_providers": providers_to_save,
            "custom_providers": self._custom_definitions(),
            "mode_models": self.mode_model_widget.get_settings(),
            "omniparser": omniparser_settings,
            "allowed_apps": new_allowed_apps,
            "hotkeys": hotkeys_to_save,
            "max_retries": self.max_retries_spinbox.value(),
            "retry_delay_seconds": self.retry_delay_spinbox.value(),
            "max_consecutive_errors": self.max_errors_spinbox.value(),
            "prevent_multiple_instances": self.prevent_multiple_instances_checkbox.isChecked(),
            "vision": vision_settings,
            "browser": browser_settings,
            "terminal": terminal_settings,
            "compact_mode": compact_mode_settings,
            "context": context_settings,
            "theme": theme,
            "search": search_settings
        }

    def save_and_accept(self):
        """Saves settings."""
        self.all_settings_to_save = self._collect_settings_from_ui()
        self.settings_manager.set_many(self.all_settings_to_save)
        for provider_id in self._deleted_custom_ids:
            self.settings_manager.delete_provider_key(provider_id)
        self._deleted_custom_ids.clear()
        self._custom_snapshot = None
        self.hide()

    def _create_general_section(self):
        """Creates the General settings section.

        Building the provider UI needs every provider SDK imported (seconds of
        work), so unless the background warm-up already finished, the page
        shows a busy placeholder and the widget is inserted once imports end.
        """
        group = QGroupBox("General")
        layout = QVBoxLayout(group)
        self.general_provider_layout = layout
        self.llm_provider_widget = None
        self.provider_placeholder = None
        self.provider_spinner = None

        if providers_ready():
            self.llm_provider_widget = LLMProviderWidget(self.settings_manager)
            layout.addWidget(self.llm_provider_widget)
        else:
            self.provider_placeholder = QWidget()
            placeholder_layout = QVBoxLayout(self.provider_placeholder)
            placeholder_layout.setContentsMargins(0, 0, 0, 0)
            placeholder_layout.addStretch(1)
            self.provider_spinner = BusySpinner()
            self.provider_spinner.start()
            placeholder_layout.addWidget(
                self.provider_spinner, 0, Qt.AlignHCenter)
            label = QLabel("Loading provider settings…")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet(
                "color: #9E9E9E; font-style: italic; padding-top: 10px;")
            placeholder_layout.addWidget(label)
            placeholder_layout.addStretch(1)
            layout.addWidget(self.provider_placeholder)

            start_provider_warmup()
            self._provider_poll_timer = QTimer(self)
            self._provider_poll_timer.setInterval(150)
            self._provider_poll_timer.timeout.connect(
                self._try_build_provider_widget)
            self._provider_poll_timer.start()

        self.mode_model_widget = ModeModelWidget(self.settings_manager)
        layout.addWidget(self.mode_model_widget)
        if self.llm_provider_widget is None:
            # The Vision model selector lists models of configured providers,
            # so it has nothing to show until the provider UI exists.
            self.mode_model_widget.setVisible(False)

        return group

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_busy_layout()

    def _update_busy_layout(self):
        """Sizes the loading state relative to the window (spinner ≈ 15% h)."""
        spinner = getattr(self, "provider_spinner", None)
        if spinner is None:
            return
        side = max(32, int(self.height() * 0.15))
        spinner.setFixedSize(side, side)
        placeholder = getattr(self, "provider_placeholder", None)
        if placeholder is not None:
            placeholder.setMinimumHeight(max(160, int(self.height() * 0.6)))

    def _try_build_provider_widget(self):
        """Swaps the busy placeholder for the real provider widget once ready."""
        if self.llm_provider_widget is not None or not providers_ready():
            return
        self._provider_poll_timer.stop()
        widget = LLMProviderWidget(self.settings_manager)
        layout = self.general_provider_layout
        index = layout.indexOf(self.provider_placeholder)
        layout.insertWidget(index, widget)
        layout.removeWidget(self.provider_placeholder)
        self.provider_spinner.stop()
        self.provider_spinner = None
        self.provider_placeholder.deleteLater()
        self.provider_placeholder = None
        self.llm_provider_widget = widget
        self.mode_model_widget.setVisible(True)

    def _create_custom_providers_section(self):
        """User-defined OpenAI-compatible providers."""
        group = QGroupBox("Custom Providers")
        layout = QVBoxLayout(group)

        description = QLabel(
            "Add any OpenAI-compatible endpoint: local models served by "
            "LM Studio, Ollama or llama.cpp, or a company gateway. "
            "Custom providers work in Chat, Terminal and Browser. Vision "
            "requires a native computer-use provider (OpenAI, Anthropic or "
            "Google), so custom providers are not offered there."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.custom_providers_list = QListWidget()
        self.custom_providers_list.setMinimumHeight(220)
        self.custom_providers_list.itemSelectionChanged.connect(
            self._update_custom_provider_buttons)
        self.custom_providers_list.itemDoubleClicked.connect(
            lambda _item: self._edit_custom_provider())
        layout.addWidget(self.custom_providers_list)

        self.custom_providers_empty_label = QLabel(
            "No custom providers yet. Use \"Add Provider\" to connect one.")
        self.custom_providers_empty_label.setStyleSheet(
            "color: #9E9E9E; font-style: italic;")
        self.custom_providers_empty_label.setWordWrap(True)
        layout.addWidget(self.custom_providers_empty_label)

        buttons = QHBoxLayout()
        self.add_custom_provider_button = QPushButton("Add Provider...")
        self.add_custom_provider_button.clicked.connect(
            self._add_custom_provider)
        self.edit_custom_provider_button = QPushButton("Edit...")
        self.edit_custom_provider_button.clicked.connect(
            self._edit_custom_provider)
        self.delete_custom_provider_button = QPushButton("Delete")
        self.delete_custom_provider_button.clicked.connect(
            self._delete_custom_provider)
        buttons.addWidget(self.add_custom_provider_button)
        buttons.addWidget(self.edit_custom_provider_button)
        buttons.addWidget(self.delete_custom_provider_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        return group

    # --- Custom provider definitions -----------------------------------

    def _custom_definitions(self):
        definitions = self.settings_manager.get("custom_providers", []) or []
        return [copy.deepcopy(d) for d in definitions if isinstance(d, dict)]

    def _selected_custom_provider_id(self):
        item = self.custom_providers_list.currentItem()
        return item.data(Qt.UserRole) if item else ""

    def _populate_custom_providers(self):
        selected = self._selected_custom_provider_id()
        self.custom_providers_list.clear()
        definitions = self._custom_definitions()
        for definition in definitions:
            pid = definition.get("id", "")
            name = definition.get("name") or pid
            item = QListWidgetItem(f"{name}  ({pid})")
            item.setData(Qt.UserRole, pid)
            models = ", ".join(m.get("id", "")
                               for m in definition.get("models") or [])
            item.setToolTip(f"{definition.get('base_url', '')}\n{models}")
            self.custom_providers_list.addItem(item)
            if pid == selected:
                self.custom_providers_list.setCurrentItem(item)
        self.custom_providers_empty_label.setVisible(not definitions)
        self._update_custom_provider_buttons()

    def _update_custom_provider_buttons(self):
        has_selection = bool(self._selected_custom_provider_id())
        self.edit_custom_provider_button.setEnabled(has_selection)
        self.delete_custom_provider_button.setEnabled(has_selection)

    def _remember_custom_state(self):
        if self._custom_snapshot is not None:
            return
        self._custom_snapshot = {
            "custom_providers": self._custom_definitions(),
            "llm_providers": copy.deepcopy(
                self.settings_manager.get("llm_providers", {}) or {}),
            "active_provider": self.settings_manager.get("active_provider"),
        }

    def _apply_custom_definitions(self, definitions, api_key_updates=None):
        """Stages definitions in memory and refreshes the provider widgets."""
        self._remember_custom_state()
        providers = self.settings_manager.get("llm_providers", {}) or {}
        for pid, key in (api_key_updates or {}).items():
            entry = providers.get(pid)
            if not isinstance(entry, dict):
                entry = {}
                providers[pid] = entry
            entry["api_key"] = key
        self.settings_manager.set_many({
            "custom_providers": copy.deepcopy(definitions),
            "llm_providers": providers,
        }, save=False, emit=False)
        try:
            self.settings_manager.sync_custom_providers(
                save=False, delete_keys=False)
        except Exception as e:
            logging.error(f"Failed to apply custom providers: {e}",
                          exc_info=True)
            return
        self._refresh_after_custom_change()

    def _refresh_after_custom_change(self):
        if self.llm_provider_widget is not None:
            self.llm_provider_widget.rebuild_provider_pages()
        self.mode_model_widget.load_settings(
            self.settings_manager.get("mode_models", {}) or {})
        self._populate_custom_providers()

    def _select_custom_provider(self, provider_id):
        for row in range(self.custom_providers_list.count()):
            item = self.custom_providers_list.item(row)
            if item.data(Qt.UserRole) == provider_id:
                self.custom_providers_list.setCurrentItem(item)
                break

    def _add_custom_provider(self):
        dialog = CustomProviderDialog(self, reserved_ids=set(PROVIDERS))
        if dialog.exec() != QDialog.Accepted or not dialog.definition:
            return
        definition = dialog.definition
        definitions = self._custom_definitions()
        definitions.append(definition)
        self._deleted_custom_ids.discard(definition["id"])
        self._apply_custom_definitions(
            definitions, {definition["id"]: dialog.api_key})
        self._select_custom_provider(definition["id"])

    def _edit_custom_provider(self):
        pid = self._selected_custom_provider_id()
        definition = next(
            (d for d in self._custom_definitions() if d.get("id") == pid), None)
        if definition is None:
            return
        providers = self.settings_manager.get("llm_providers", {}) or {}
        api_key = (providers.get(pid) or {}).get("api_key", "")
        dialog = CustomProviderDialog(
            self, definition=definition, api_key=api_key,
            reserved_ids=set(PROVIDERS))
        if dialog.exec() != QDialog.Accepted or not dialog.definition:
            return
        definitions = [
            dialog.definition if d.get("id") == pid else d
            for d in self._custom_definitions()
        ]
        self._apply_custom_definitions(definitions, {pid: dialog.api_key})
        self._select_custom_provider(pid)

    def _delete_custom_provider(self):
        pid = self._selected_custom_provider_id()
        definition = next(
            (d for d in self._custom_definitions() if d.get("id") == pid), None)
        if definition is None:
            return
        name = definition.get("name") or pid
        answer = QMessageBox.question(
            self, "Delete provider",
            f"Delete the custom provider \"{name}\" ({pid})?\n\n"
            "Its API key and settings will be removed.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._deleted_custom_ids.add(pid)
        definitions = [
            d for d in self._custom_definitions() if d.get("id") != pid]
        self._apply_custom_definitions(definitions)

    def _discard_custom_changes(self):
        snapshot = self._custom_snapshot
        self._custom_snapshot = None
        self._deleted_custom_ids.clear()
        if snapshot is None:
            return
        self.settings_manager.set_many(snapshot, save=False, emit=False)
        try:
            self.settings_manager.sync_custom_providers(
                save=False, delete_keys=False)
        except Exception as e:
            logging.error(f"Failed to roll back custom providers: {e}",
                          exc_info=True)
            return
        self._refresh_after_custom_change()

    def _create_hotkeys_section(self):
        """Creates the Hotkeys settings section."""
        group = QGroupBox("Hotkeys")
        layout = QFormLayout(group)

        self.toggle_mode_hotkey = HotkeyEdit()
        layout.addRow("Toggle Mode:", self.toggle_mode_hotkey)
        self.pause_resume_hotkey = HotkeyEdit()
        layout.addRow("Pause/Resume Execution:", self.pause_resume_hotkey)
        self.stop_execution_hotkey = HotkeyEdit()
        layout.addRow("Stop Execution:", self.stop_execution_hotkey)
        self.pause_for_feedback_hotkey = HotkeyEdit()
        layout.addRow("Pause for Feedback:", self.pause_for_feedback_hotkey)
        self.cancel_request_hotkey = HotkeyEdit()
        layout.addRow("Cancel Request:", self.cancel_request_hotkey)

        return group

    def _create_apps_section(self):
        """Creates the Apps settings section."""
        group = QGroupBox("Applications")
        layout = QVBoxLayout(group)

        description = QLabel(
            "Applications the assistant may launch directly. Checked apps "
            "are listed to the Vision model, which can open them by name "
            "without searching the Start menu. With 'Restrict actions to "
            "selected Applications' enabled in Vision settings, only the "
            "checked apps can also be interacted with."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.scan_prompt_label = QLabel("Application cache not found. Please scan to discover all applications.")
        self.scan_prompt_label.setStyleSheet("font-style: italic; color: #D32F2F; font-weight: bold;")
        self.scan_prompt_label.setVisible(False)
        layout.addWidget(self.scan_prompt_label)

        scan_button = QPushButton("Scan for Applications")
        scan_button.clicked.connect(self.force_scan_and_populate)
        layout.addWidget(scan_button)

        filter_row = QHBoxLayout()
        self.show_selected_apps_checkbox = QCheckBox("Show only selected applications")
        self.show_selected_apps_checkbox.setToolTip(
            "Hide unselected apps so the checked ones are easy to find. "
            "Turn this off to browse and select from all installed apps.")
        self.show_selected_apps_checkbox.toggled.connect(
            self._apply_apps_filter)
        filter_row.addWidget(self.show_selected_apps_checkbox)
        filter_row.addStretch()
        self.apps_count_label = QLabel()
        self.apps_count_label.setStyleSheet("color: #9E9E9E;")
        filter_row.addWidget(self.apps_count_label)
        layout.addLayout(filter_row)

        self.apps_list_widget = QListWidget()
        self.apps_list_widget.setToolTip("Select the applications the assistant may launch directly. Enable the Vision restriction checkbox to also limit interaction to this list.")
        self.apps_list_widget.setFixedHeight(250) # Set a fixed height for the list
        self.apps_list_widget.itemChanged.connect(self._on_app_item_changed)
        layout.addWidget(self.apps_list_widget)

        self.apps_empty_label = QLabel(
            "No applications selected. Uncheck 'Show only selected "
            "applications' to choose from all installed apps.")
        self.apps_empty_label.setStyleSheet(
            "color: #9E9E9E; font-style: italic;")
        self.apps_empty_label.setWordWrap(True)
        self.apps_empty_label.setVisible(False)
        layout.addWidget(self.apps_empty_label)

        return group

    def _populate_apps_list(self, all_apps):
        """Helper function to populate the QListWidget with a list of apps."""
        try:
            all_apps.sort(key=lambda x: x['name'].lower())
        except Exception as e:
            logging.error(f"Could not sort apps list: {e}")

        allowed_apps = self.settings_manager.get('allowed_apps', [])
        allowed_app_ids = {AppScanner._get_unique_app_id(app) for app in allowed_apps}

        self.apps_list_widget.blockSignals(True)
        try:
            self.apps_list_widget.clear()
            for app in all_apps:
                item = QListWidgetItem(app['name'])
                item.setData(Qt.UserRole, app)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)

                app_id = AppScanner._get_unique_app_id(app)
                if app_id in allowed_app_ids:
                    item.setCheckState(Qt.Checked)
                else:
                    item.setCheckState(Qt.Unchecked)
                self.apps_list_widget.addItem(item)
        finally:
            self.apps_list_widget.blockSignals(False)

        self.show_selected_apps_checkbox.blockSignals(True)
        self.show_selected_apps_checkbox.setChecked(False)
        self.show_selected_apps_checkbox.blockSignals(False)
        self._apply_apps_filter()

    def _on_app_item_changed(self, _item):
        """Keeps the selected-only view and counter in sync."""
        self._apply_apps_filter()

    def _apply_apps_filter(self, _checked=None):
        """Hides unselected rows without dropping their selection state."""
        only_selected = self.show_selected_apps_checkbox.isChecked()
        selected = 0
        visible = 0
        for index in range(self.apps_list_widget.count()):
            item = self.apps_list_widget.item(index)
            is_selected = item.checkState() == Qt.Checked
            if is_selected:
                selected += 1
            hidden = only_selected and not is_selected
            item.setHidden(hidden)
            if not hidden:
                visible += 1

        total = self.apps_list_widget.count()
        self.apps_count_label.setText(f"Selected: {selected} / {total}")
        self.apps_empty_label.setVisible(
            only_selected and total > 0 and visible == 0)

    def load_apps_from_cache(self):
        """Loads applications from cache or settings."""
        try:
            scanner = AppScanner(self.settings_manager)
            cached_apps = scanner.load_from_cache()

            if cached_apps:
                self.scan_prompt_label.setVisible(False)
                self._populate_apps_list(cached_apps)
            else:
                self.scan_prompt_label.setVisible(True)
                allowed_apps = self.settings_manager.get('allowed_apps', [])
                self._populate_apps_list(allowed_apps)
        except Exception as e:
            logging.error(f"Failed to load applications: {e}", exc_info=True)
            self._populate_apps_list([])

    def force_scan_and_populate(self):
        """Forces a new scan for applications and repopulates the list."""
        try:
            scanner = AppScanner(self.settings_manager)
            all_apps = scanner.scan_and_cache()
            self.scan_prompt_label.setVisible(False)
            self._populate_apps_list(all_apps)
        except Exception as e:
            logging.error(f"Failed to scan for applications: {e}", exc_info=True)
            self._populate_apps_list([])

    def _create_help_label(self, tooltip_text):
        """Creates a styled QLabel with a tooltip."""
        help_label = QLabel("?")
        self.tooltip_manager.install(help_label, tooltip_text)
        help_label.setStyleSheet("font-weight: bold; color: #9E9E9E; padding: 2px;")
        return help_label

    def _create_context_setting_label(self, text, anchor):
        """Context setting name that opens its section in the online guide."""
        label = QLabel()
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(True)
        label.setToolTip("Open this setting's description in the online guide")
        self._context_doc_links.append((label, text, anchor))
        self._render_context_doc_link(label, text, anchor)
        return label

    def _render_context_doc_link(self, label, text, anchor):
        theme = self.controller.theme_manager.current_theme
        palette = create_light_palette() if theme == LIGHT_THEME else create_dark_palette()
        color = palette.color(QPalette.ColorRole.Link).name()
        label.setText(
            f'<a href="{CONTEXT_SETTINGS_DOC_URL}#{anchor}" '
            f'style="color: {color}; text-decoration: underline;">{text}</a>')

    def _refresh_context_doc_links(self):
        for label, text, anchor in self._context_doc_links:
            self._render_context_doc_link(label, text, anchor)

    def _create_common_section(self):
        """Creates the Common settings section."""
        group = QGroupBox("Common")
        tab_layout = QVBoxLayout(group)

        network_group = QGroupBox("Network")
        network_layout = QGridLayout(network_group)
        network_layout.setColumnStretch(2, 1)

        self.max_retries_spinbox = QSpinBox()
        self.max_retries_spinbox.setRange(1, 10)
        self.max_retries_spinbox.setFixedWidth(80)
        max_retries_tooltip = (
            "Max number of retries for a failed network request. "
            "This only applies to non-critical errors (e.g., temporary server issues)."
        )
        network_layout.addWidget(QLabel("Max Retries:"), 0, 0)
        network_layout.addWidget(self._create_help_label(max_retries_tooltip), 0, 1, Qt.AlignLeft)
        network_layout.addWidget(self.max_retries_spinbox, 0, 3)

        self.retry_delay_spinbox = QDoubleSpinBox()
        self.retry_delay_spinbox.setRange(1.0, 10.0)
        self.retry_delay_spinbox.setSingleStep(0.5)
        self.retry_delay_spinbox.setSuffix(" s")
        self.retry_delay_spinbox.setFixedWidth(80)
        retry_delay_tooltip = "Initial delay in seconds before the first retry."
        network_layout.addWidget(QLabel("Retry Delay:"), 1, 0)
        network_layout.addWidget(self._create_help_label(retry_delay_tooltip), 1, 1, Qt.AlignLeft)
        network_layout.addWidget(self.retry_delay_spinbox, 1, 3)
        tab_layout.addWidget(network_group)

        app_group = QGroupBox("Application")
        app_layout = QGridLayout(app_group)
        app_layout.setColumnStretch(2, 1)
        self.prevent_multiple_instances_checkbox = QCheckBox()
        prevent_multiple_tooltip = "Prevents more than one instance of the application from running."
        app_layout.addWidget(QLabel("Allow only one instance:"), 0, 0)
        app_layout.addWidget(self._create_help_label(prevent_multiple_tooltip), 0, 1, Qt.AlignLeft)
        app_layout.addWidget(self.prevent_multiple_instances_checkbox, 0, 3)
        tab_layout.addWidget(app_group)

        theme_group = QGroupBox("Theme")
        theme_layout = QHBoxLayout(theme_group)
        self.light_theme_radio = QRadioButton("Light")
        self.dark_theme_radio = QRadioButton("Dark")
        self.theme_button_group = QButtonGroup(self)
        self.theme_button_group.addButton(self.light_theme_radio)
        self.theme_button_group.addButton(self.dark_theme_radio)
        theme_layout.addWidget(self.light_theme_radio)
        theme_layout.addWidget(self.dark_theme_radio)
        theme_layout.addStretch()
        tab_layout.addWidget(theme_group)

        self.light_theme_radio.toggled.connect(self._on_theme_changed)
        self.dark_theme_radio.toggled.connect(self._on_theme_changed)

        # Agent (was its own nav section; a single knob, so folded into Common)
        agent_group = QGroupBox("Agent")
        agent_layout = QGridLayout(agent_group)
        agent_layout.setColumnStretch(2, 1)
        self.max_errors_spinbox = QSpinBox()
        self.max_errors_spinbox.setRange(1, 10)
        self.max_errors_spinbox.setFixedWidth(80)
        max_errors_tooltip = (
            "Failed steps in a row before the agent stops and asks you "
            "(after one self-assessment turn). Shared by all agent modes. "
            "Applies immediately."
        )
        agent_layout.addWidget(QLabel("Failures before asking:"), 0, 0)
        agent_layout.addWidget(self._create_help_label(max_errors_tooltip), 0, 1, Qt.AlignLeft)
        agent_layout.addWidget(self.max_errors_spinbox, 0, 3)
        tab_layout.addWidget(agent_group)

        return group

    def _on_theme_changed(self):
        """Applies the selected theme immediately."""
        if self.light_theme_radio.isChecked():
            self.controller.theme_manager.apply_theme(LIGHT_THEME)
        elif self.dark_theme_radio.isChecked():
            self.controller.theme_manager.apply_theme(DARK_THEME)

    def _create_compact_mode_section(self):
        """Creates the Compact Mode settings section."""
        group = QGroupBox("Compact Mode")
        tab_layout = QVBoxLayout(group)

        description = QLabel(
            "The compact window is used automatically during Vision mode "
            "tasks, so the assistant's own window does not cover the screen "
            "it is working on. These values set its size and where it appears."
        )
        description.setWordWrap(True)
        tab_layout.addWidget(description)

        compact_mode_group = QGroupBox("Compact Mode Geometry")
        compact_mode_layout = QGridLayout(compact_mode_group)
        compact_mode_layout.setColumnStretch(2, 1)
        self.compact_mode_width_spinbox = QSpinBox()
        self.compact_mode_width_spinbox.setRange(300, 2000)
        self.compact_mode_width_spinbox.setSuffix(" px")
        self.compact_mode_width_spinbox.setFixedWidth(100)
        width_tooltip = "Default width of the compact mode window."
        compact_mode_layout.addWidget(QLabel("Default Width:"), 0, 0)
        compact_mode_layout.addWidget(self._create_help_label(width_tooltip), 0, 1, Qt.AlignLeft)
        compact_mode_layout.addWidget(self.compact_mode_width_spinbox, 0, 3)
        self.compact_mode_height_spinbox = QSpinBox()
        self.compact_mode_height_spinbox.setRange(50, 500)
        self.compact_mode_height_spinbox.setSuffix(" px")
        self.compact_mode_height_spinbox.setFixedWidth(100)
        height_tooltip = "Default height of the compact mode window."
        compact_mode_layout.addWidget(QLabel("Default Height:"), 1, 0)
        compact_mode_layout.addWidget(self._create_help_label(height_tooltip), 1, 1, Qt.AlignLeft)
        compact_mode_layout.addWidget(self.compact_mode_height_spinbox, 1, 3)
        tab_layout.addWidget(compact_mode_group)

        position_group = QGroupBox("Default Position")
        position_layout = QGridLayout(position_group)
        position_layout.setColumnStretch(1, 1)
        position_grid_widget = self._create_position_grid()
        position_tooltip = "Default position where the compact window will appear."
        position_layout.addWidget(QLabel("Anchor Point:"), 0, 0, Qt.AlignTop)
        position_layout.addWidget(self._create_help_label(position_tooltip), 0, 1, Qt.AlignTop)
        position_layout.addWidget(position_grid_widget, 0, 2)
        tab_layout.addWidget(position_group)

        return group

    def _create_vision_section(self):
        """Creates the Vision Mode settings section (agent loop tunables)."""
        group = QGroupBox("Vision Mode (agent loop)")
        vision_layout = QGridLayout(group)
        vision_layout.setColumnStretch(2, 1)

        self.allow_shell_commands_checkbox = QCheckBox(
            "Allow terminal commands in Vision mode")
        shell_tooltip = ("Lets the agent open a terminal itself (Win+R) for "
                         "quick checks (sizes, counts) or operations faster "
                         "through shell than GUI. Read-only checks are fine; "
                         "modifying commands still need your approval. "
                         "Off by default.")
        self.allow_shell_commands_checkbox.setToolTip(shell_tooltip)
        vision_layout.addWidget(self.allow_shell_commands_checkbox, 0, 0, 1, 4)

        self.allow_system_surfaces_checkbox = QCheckBox(
            "Allow Windows Search / Start menu")
        surfaces_tooltip = ("Lets the agent use Windows Search and the Start "
                            "menu to launch applications. With the restriction "
                            "below enabled, launched apps still need to be "
                            "checked in Applications. UWP windows are resolved "
                            "to their real process. Off by default.")
        self.allow_system_surfaces_checkbox.setToolTip(surfaces_tooltip)
        vision_layout.addWidget(self.allow_system_surfaces_checkbox, 1, 0, 1, 4)

        self.restrict_to_allowed_apps_checkbox = QCheckBox(
            "Restrict actions to selected Applications")
        restrict_tooltip = ("When enabled, the agent may only interact with "
                            "apps checked in Applications (plus Explorer and "
                            "its own window). Off by default: any window can "
                            "be used, while destructive actions and credential "
                            "entry still need approval.")
        self.restrict_to_allowed_apps_checkbox.setToolTip(restrict_tooltip)
        vision_layout.addWidget(
            self.restrict_to_allowed_apps_checkbox, 2, 0, 1, 4)

        self.max_turns_spinbox = QSpinBox()
        self.max_turns_spinbox.setRange(5, 200)
        self.max_turns_spinbox.setFixedWidth(80)
        max_turns_tooltip = ("Max computer-use turns per task before pausing. "
                             "Single-action providers burn turns faster.")
        vision_layout.addWidget(QLabel("Max turns per task:"), 3, 0)
        vision_layout.addWidget(self._create_help_label(max_turns_tooltip), 3, 1, Qt.AlignLeft)
        vision_layout.addWidget(self.max_turns_spinbox, 3, 3)

        self.shot_width_spinbox = QSpinBox()
        self.shot_width_spinbox.setRange(0, 3840)
        self.shot_width_spinbox.setSingleStep(160)
        self.shot_width_spinbox.setFixedWidth(80)
        shot_width_tooltip = ("Max screenshot width sent to the model "
                              "(0 = native). Vendors normalize anyway; "
                              "1600 saves tokens with no accuracy loss.")
        vision_layout.addWidget(QLabel("Screenshot max width:"), 4, 0)
        vision_layout.addWidget(self._create_help_label(shot_width_tooltip), 4, 1, Qt.AlignLeft)
        vision_layout.addWidget(self.shot_width_spinbox, 4, 3)

        self.save_trace_log_checkbox = QCheckBox("Save detailed trace log")
        trace_log_tooltip = "Appends one JSON line per computer-use turn (actions, safety, screenshot path) to the app timeline."
        vision_layout.addWidget(self.save_trace_log_checkbox, 5, 0)
        vision_layout.addWidget(self._create_help_label(trace_log_tooltip), 5, 1)
        self.save_pre_intent_checkbox = QCheckBox("Save pre-action screenshot")
        pre_intent_tooltip = "Saves a screenshot before each turn's actions (screenshots/cua_<time>/)."
        vision_layout.addWidget(self.save_pre_intent_checkbox, 6, 0)
        vision_layout.addWidget(self._create_help_label(pre_intent_tooltip), 6, 1)
        self.save_post_checkbox = QCheckBox("Save post-action screenshot")
        post_action_tooltip = "Saves an annotated screenshot after each turn (red marks: clicks, drags)."
        vision_layout.addWidget(self.save_post_checkbox, 7, 0)
        vision_layout.addWidget(self._create_help_label(post_action_tooltip), 7, 1)

        annotation_group = QGroupBox("Annotation Settings")
        annotation_layout = QGridLayout(annotation_group)
        annotation_layout.setColumnStretch(2, 1)
        self.font_combo = FontComboBox()
        font_tooltip = "Font used for text on the annotated screenshots."
        reset_button = QPushButton("Reset to Default")
        reset_button.clicked.connect(lambda: self.font_combo.setCurrentIndex(0))
        font_widget = QWidget()
        font_layout = QHBoxLayout(font_widget)
        font_layout.setContentsMargins(0,0,0,0)
        font_layout.addWidget(self.font_combo, 1)
        font_layout.addWidget(reset_button)
        annotation_layout.addWidget(QLabel("Font:"), 0, 0)
        annotation_layout.addWidget(self._create_help_label(font_tooltip), 0, 1, Qt.AlignLeft)
        annotation_layout.addWidget(font_widget, 0, 2, 1, 2)
        self.font_size_spinbox = QSpinBox()
        self.font_size_spinbox.setRange(10, 28)
        self.font_size_spinbox.setSuffix(" px")
        self.font_size_spinbox.setFixedWidth(80)
        font_size_tooltip = "Font size for text labels on annotated screenshots."
        annotation_layout.addWidget(QLabel("Font Size:"), 1, 0)
        annotation_layout.addWidget(self._create_help_label(font_size_tooltip), 1, 1, Qt.AlignLeft)
        annotation_layout.addWidget(self.font_size_spinbox, 1, 3)
        self.text_padding_spinbox = QSpinBox()
        self.text_padding_spinbox.setRange(0, 12)
        self.text_padding_spinbox.setSuffix(" px")
        self.text_padding_spinbox.setFixedWidth(80)
        padding_tooltip = "Padding around text labels on annotated screenshots."
        annotation_layout.addWidget(QLabel("Padding:"), 2, 0)
        annotation_layout.addWidget(self._create_help_label(padding_tooltip), 2, 1, Qt.AlignLeft)
        annotation_layout.addWidget(self.text_padding_spinbox, 2, 3)
        vision_layout.addWidget(annotation_group, 8, 0, 1, 4)

        return group

    def _create_context_section(self):
        """Creates the Context (history compaction + handoff summary) section."""
        group = QGroupBox("Context")
        context_layout = QGridLayout(group)
        context_layout.setColumnStretch(2, 1)

        self.history_keep_last_spinbox = QSpinBox()
        self.history_keep_last_spinbox.setRange(0, 200)
        self.history_keep_last_spinbox.setFixedWidth(80)
        keep_last_tooltip = (
            "How many of the latest history steps are sent in full. Older steps "
            "are condensed to one line each (command + outcome). 0 = send the "
            "entire history (default, most expensive)."
        )
        context_layout.addWidget(self._create_context_setting_label(
            "Keep last N steps full:", "1-keep-last-n-steps-full"), 0, 0)
        context_layout.addWidget(self._create_help_label(keep_last_tooltip), 0, 1, Qt.AlignLeft)
        context_layout.addWidget(self.history_keep_last_spinbox, 0, 3)

        self.history_max_chars_spinbox = QSpinBox()
        self.history_max_chars_spinbox.setRange(0, 20000)
        self.history_max_chars_spinbox.setSingleStep(500)
        self.history_max_chars_spinbox.setFixedWidth(80)
        max_chars_tooltip = (
            "Max characters per single history entry (longer entries are cut "
            "head+tail with a marker). 0 = unlimited (default)."
        )
        context_layout.addWidget(self._create_context_setting_label(
            "Max chars per entry:", "2-max-chars-per-entry"), 1, 0)
        context_layout.addWidget(self._create_help_label(max_chars_tooltip), 1, 1, Qt.AlignLeft)
        context_layout.addWidget(self.history_max_chars_spinbox, 1, 3)

        self.history_dedup_checkbox = QCheckBox()
        dedup_tooltip = (
            "Drop consecutive identical page snapshots, keeping one copy with "
            "an '(unchanged)' note. Snapshots repeat a lot and cost tokens."
        )
        context_layout.addWidget(self._create_context_setting_label(
            "Dedup repeated snapshots:", "3-dedup-repeated-snapshots"), 2, 0)
        context_layout.addWidget(self._create_help_label(dedup_tooltip), 2, 1, Qt.AlignLeft)
        context_layout.addWidget(self.history_dedup_checkbox, 2, 3)

        self.history_trim_tracebacks_checkbox = QCheckBox()
        trim_tooltip = (
            "Cut long Python tracebacks in tool errors to head+tail lines. "
            "The error type and location (what teaches the model) are kept."
        )
        context_layout.addWidget(self._create_context_setting_label(
            "Trim long tracebacks:", "4-trim-long-tracebacks"), 3, 0)
        context_layout.addWidget(self._create_help_label(trim_tooltip), 3, 1, Qt.AlignLeft)
        context_layout.addWidget(self.history_trim_tracebacks_checkbox, 3, 3)

        self.handoff_enabled_checkbox = QCheckBox()
        handoff_tooltip = (
            "Keep a rolling summary next to each session journal so a new mode, "
            "model or application run can continue with compact context instead "
            "of the full journal. Summaries never mix between sessions."
        )
        context_layout.addWidget(self._create_context_setting_label(
            "Rolling handoff summary:", "5-rolling-handoff-summary"), 4, 0)
        context_layout.addWidget(self._create_help_label(handoff_tooltip), 4, 1, Qt.AlignLeft)
        context_layout.addWidget(self.handoff_enabled_checkbox, 4, 3)

        self.handoff_mode_switch_checkbox = QCheckBox()
        mode_tooltip = "Seed the new session's summary when switching chat/terminal/browser mode."
        context_layout.addWidget(self._create_context_setting_label(
            "On mode switch:", "6-on-mode-switch"), 5, 0)
        context_layout.addWidget(self._create_help_label(mode_tooltip), 5, 1, Qt.AlignLeft)
        context_layout.addWidget(self.handoff_mode_switch_checkbox, 5, 3)

        self.handoff_model_switch_checkbox = QCheckBox()
        model_tooltip = "Refresh and inject the summary when switching the active model."
        context_layout.addWidget(self._create_context_setting_label(
            "On model switch:", "7-on-model-switch"), 6, 0)
        context_layout.addWidget(self._create_help_label(model_tooltip), 6, 1, Qt.AlignLeft)
        context_layout.addWidget(self.handoff_model_switch_checkbox, 6, 3)

        self.summary_max_tokens_spinbox = QSpinBox()
        self.summary_max_tokens_spinbox.setRange(200, 8000)
        self.summary_max_tokens_spinbox.setSingleStep(100)
        self.summary_max_tokens_spinbox.setFixedWidth(80)
        max_tokens_tooltip = "Token budget requested from the summarizer model."
        context_layout.addWidget(self._create_context_setting_label(
            "Summary max tokens:", "8-summary-max-tokens"), 7, 0)
        context_layout.addWidget(self._create_help_label(max_tokens_tooltip), 7, 1, Qt.AlignLeft)
        context_layout.addWidget(self.summary_max_tokens_spinbox, 7, 3)

        self.summary_threshold_spinbox = QDoubleSpinBox()
        self.summary_threshold_spinbox.setRange(0.1, 1.0)
        self.summary_threshold_spinbox.setSingleStep(0.05)
        self.summary_threshold_spinbox.setDecimals(2)
        self.summary_threshold_spinbox.setFixedWidth(80)
        threshold_tooltip = (
            "Refresh the summary when estimated context usage reaches this "
            "fraction of the model window."
        )
        context_layout.addWidget(self._create_context_setting_label(
            "Refresh threshold:", "9-refresh-threshold"), 8, 0)
        context_layout.addWidget(self._create_help_label(threshold_tooltip), 8, 1, Qt.AlignLeft)
        context_layout.addWidget(self.summary_threshold_spinbox, 8, 3)

        self.summary_model_lineedit = QLineEdit()
        self.summary_model_lineedit.setPlaceholderText(
            "provider:model (empty = current session model)")
        summary_model_tooltip = (
            "Optional cheaper model for handoff summaries. Use "
            "'provider:model' (for example 'openai:gpt-5-mini'), or a bare "
            "model id to keep the active provider. Empty uses the session model."
        )
        context_layout.addWidget(self._create_context_setting_label(
            "Summarizer model:", "10-summarizer-model"), 9, 0)
        context_layout.addWidget(self._create_help_label(summary_model_tooltip), 9, 1, Qt.AlignLeft)
        context_layout.addWidget(self.summary_model_lineedit, 9, 2, 1, 2)

        self.auto_compact_checkbox = QCheckBox()
        auto_compact_tooltip = (
            "When free space in the model window drops to the response buffer "
            "(~15% of the window, at least 16k tokens), older events are "
            "compacted into the session summary automatically once the current "
            "step finishes. Chat, Terminal and Browser only."
        )
        context_layout.addWidget(self._create_context_setting_label(
            "Auto-compact on low headroom:", "11-auto-compact-on-low-headroom"), 10, 0)
        context_layout.addWidget(self._create_help_label(auto_compact_tooltip), 10, 1, Qt.AlignLeft)
        context_layout.addWidget(self.auto_compact_checkbox, 10, 3)

        return group

    def _create_about_link(self, text, url):
        """Creates a label that opens an external URL in the default browser."""
        label = QLabel(f'<a href="{url}">{text}</a>')
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(True)
        label.setToolTip(f"Open {url}")
        return label

    def _create_about_section(self):
        """Creates the About section pinned to the bottom of the navigation."""
        group = QGroupBox("About")
        layout = QVBoxLayout(group)
        layout.setSpacing(14)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)
        logo_label = QLabel()
        logo_label.setPixmap(QIcon("assets/ico.ico").pixmap(QSize(64, 64)))
        header_layout.addWidget(logo_label, 0, Qt.AlignTop)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        name_label = QLabel("Logos")
        name_font = name_label.font()
        name_font.setPointSize(name_font.pointSize() + 8)
        name_font.setBold(True)
        name_label.setFont(name_font)
        version_label = QLabel(f"Version {APP_VERSION}")
        version_label.setStyleSheet("color: #9E9E9E;")
        title_layout.addWidget(name_label)
        title_layout.addWidget(version_label)
        title_layout.addStretch(1)
        header_layout.addLayout(title_layout, 1)
        layout.addLayout(header_layout)

        details_layout = QGridLayout()
        details_layout.setHorizontalSpacing(16)
        details_layout.setVerticalSpacing(8)
        details_layout.setColumnStretch(1, 1)
        details = [
            ("Author", QLabel("Yaroslav Litvinchenko")),
            ("Email", self._create_about_link(
                "aigrator@gmail.com", "mailto:aigrator@gmail.com")),
            ("GitHub", self._create_about_link(
                "github.com/AIgrator/Logos", PROJECT_URL)),
            ("Issue Tracker", self._create_about_link(
                "github.com/AIgrator/Logos/issues", ISSUES_URL)),
            ("License", QLabel("MIT")),
            ("Copyright", QLabel("Copyright (c) 2026 Yaroslav Litvinchenko")),
        ]
        for row, (title, value) in enumerate(details):
            field_label = QLabel(title)
            field_label.setStyleSheet("font-weight: bold;")
            value.setWordWrap(True)
            details_layout.addWidget(field_label, row, 0)
            details_layout.addWidget(value, row, 1)
        layout.addLayout(details_layout)

        license_box = QPlainTextEdit()
        license_box.setPlainText(_read_license_text())
        license_box.setReadOnly(True)
        license_box.setFixedHeight(170)
        license_box.setToolTip("MIT License (LICENSE file in the project root)")
        layout.addWidget(license_box)

        return group

    def _create_browser_section(self):
        """Creates the Browser settings section."""
        group = QGroupBox("Browser")
        browser_layout = QGridLayout(group)
        browser_layout.setColumnStretch(2, 1)

        self.block_ads_checkbox = QCheckBox()
        block_ads_tooltip = (
            "Block advertising requests using EasyList filter rules (no extension needed). "
            "ON by default. Turn OFF if a site gates content behind ads or a subscription: "
            "without this choice some pages withhold content after ad views."
        )
        browser_layout.addWidget(QLabel("Block ads:"), 0, 0)
        browser_layout.addWidget(self._create_help_label(block_ads_tooltip), 0, 1, Qt.AlignLeft)
        browser_layout.addWidget(self.block_ads_checkbox, 0, 3)

        self.browser_headless_checkbox = QCheckBox()
        headless_tooltip = "Run the controlled browser without a visible window. Applies on next browser start."
        browser_layout.addWidget(QLabel("Headless:"), 1, 0)
        browser_layout.addWidget(self._create_help_label(headless_tooltip), 1, 1, Qt.AlignLeft)
        browser_layout.addWidget(self.browser_headless_checkbox, 1, 3)

        self.browser_timeout_spinbox = QSpinBox()
        self.browser_timeout_spinbox.setRange(5000, 120000)
        self.browser_timeout_spinbox.setSingleStep(5000)
        self.browser_timeout_spinbox.setSuffix(" ms")
        self.browser_timeout_spinbox.setFixedWidth(100)
        timeout_tooltip = "Action timeout for browser tools. Applies on next browser start."
        browser_layout.addWidget(QLabel("Action timeout:"), 2, 0)
        browser_layout.addWidget(self._create_help_label(timeout_tooltip), 2, 1, Qt.AlignLeft)
        browser_layout.addWidget(self.browser_timeout_spinbox, 2, 3)

        return group

    def _create_terminal_section(self):
        """Creates the Terminal settings section."""
        group = QGroupBox("Terminal")
        terminal_layout = QGridLayout(group)
        terminal_layout.setColumnStretch(2, 1)

        self.shell_timeout_spinbox = QSpinBox()
        self.shell_timeout_spinbox.setRange(10, 3600)
        self.shell_timeout_spinbox.setSingleStep(10)
        self.shell_timeout_spinbox.setSuffix(" s")
        self.shell_timeout_spinbox.setFixedWidth(100)
        shell_timeout_tooltip = (
            "Max run time for shell commands. Long tasks (large copies, builds) "
            "need a generous value; progress streams live into the chat and "
            "Stop kills the process. Applies to the next command."
        )
        terminal_layout.addWidget(QLabel("Shell timeout:"), 0, 0)
        terminal_layout.addWidget(self._create_help_label(shell_timeout_tooltip), 0, 1, Qt.AlignLeft)
        terminal_layout.addWidget(self.shell_timeout_spinbox, 0, 3)

        self.shell_detach_spinbox = QSpinBox()
        self.shell_detach_spinbox.setRange(5, 600)
        self.shell_detach_spinbox.setSingleStep(5)
        self.shell_detach_spinbox.setSuffix(" s")
        self.shell_detach_spinbox.setFixedWidth(100)
        shell_detach_tooltip = (
            "After this many seconds a foreground command transparently becomes "
            "a background job (same timeout still applies). Lower = more responsive, "
            "higher = fewer poll turns for medium commands."
        )
        terminal_layout.addWidget(QLabel("Detach to background:"), 1, 0)
        terminal_layout.addWidget(self._create_help_label(shell_detach_tooltip), 1, 1, Qt.AlignLeft)
        terminal_layout.addWidget(self.shell_detach_spinbox, 1, 3)

        return group

    def _create_position_grid(self):
        """Creates the 3x3 grid of buttons for position selection."""
        grid_widget = QWidget()
        grid_layout = QGridLayout(grid_widget)
        grid_layout.setSpacing(2)
        grid_layout.setContentsMargins(0, 0, 0, 0)

        self.position_button_group = QButtonGroup(self)
        self.position_button_group.setExclusive(True)

        positions = [
            ("top_left", "Top-Left"), ("top_center", "Top-Center"), ("top_right", "Top-Right"),
            ("middle_left", "Middle-Left"), ("middle_center", "Middle-Center"), ("middle_right", "Middle-Right"),
            ("bottom_left", "Bottom-Left"), ("bottom_center", "Bottom-Center"), ("bottom_right", "Bottom-Right")
        ]

        for i, (pos_id, tooltip) in enumerate(positions):
            row, col = divmod(i, 3)
            button = QPushButton()
            button.setCheckable(True)
            button.setFixedSize(30, 30)
            button.setProperty("position_id", pos_id)
            button.setStyleSheet(self.controller.theme_manager.get_style("POSITION_BUTTON_STYLE"))
            self.tooltip_manager.install(button, tooltip)
            grid_layout.addWidget(button, row, col)
            self.position_button_group.addButton(button)

        return grid_widget


