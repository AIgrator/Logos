"""
This module defines the settings window for the application.
"""
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
    QListWidgetItem,
    QProgressDialog,
    QStackedWidget,
    QButtonGroup,
    QScrollArea,
    QToolButton,
    QFrame,
    QRadioButton
)
from PySide6.QtCore import Qt, QObject, QThread, Signal, QSize, QTimer
from PySide6.QtGui import QIcon
import qtawesome as qta
from src.ui.theme_manager import LIGHT_THEME, DARK_THEME

from settings_manager import SettingsManager
from src.ui.widgets.font_manager.font_combobox import FontComboBox
from src.ui.widgets.hotkey.hotkey_edit import HotkeyEdit
from src.apps.installed_apps import AppScanner
from src.ui.widgets.llm_provider_widget import LLMProviderWidget
from src.ui.widgets.ocr_language_widget import OCRLanguageWidget
from src.ui.utils import CustomToolTipManager, NavButtonHoverFilter
import logging

class LanguageUpdateWorker(QObject):
    """A worker to handle OCR language updates in a background thread."""
    download_status_changed = Signal(str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, vision_manager, new_settings):
        super().__init__()
        self.vision_manager = vision_manager
        self.new_settings = new_settings

    def run(self):
        """Performs the re-initialization and emits status."""
        try:
            if self.vision_manager:
                self.vision_manager.reinitialize_ocr(
                    self.new_settings,
                    status_callback=self.download_status_changed.emit
                )
        except Exception as e:
            logging.error(f"Error during language update: {e}", exc_info=True)
            self.error.emit(str(e))
        finally:
            self.finished.emit()


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
        omniparser_cfg = self.settings_manager.get("omniparser", {})
        self.initial_ocr_languages = set(omniparser_cfg.get("ocr_languages", []))
        self.update_thread = None
        self.update_worker = None

        # --- Spinner animations for nav buttons ---
        self.omniparser_spinner = qta.Spin(self)
        self.debug_spinner = qta.Spin(self)
        self.icon_spinner = qta.icon('fa5s.spinner', color='#d3d3d3', animation=self.omniparser_spinner)
        # --- End spinner setup ---

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

        # Scroll area for settings on the right
        self.scroll_area, self.sections = self._create_content_area()
        content_layout.addWidget(self.scroll_area)

        self.layout.addLayout(content_layout)

        # OK and Cancel buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.save_and_accept)
        self.button_box.rejected.connect(self.hide)
        self.layout.addWidget(self.button_box)

        self._is_programmatic_scroll = False # Flag to prevent scroll handler during programmatic scroll

        # Defer height adjustment to ensure all widgets are initialized
        QTimer.singleShot(0, self._adjust_window_height)

        self.load_settings()

        self._update_vision_sections_state()
        self.controller.vision_module_ready.connect(self._on_vision_module_ready)
        self.controller.theme_manager.theme_changed.connect(self._update_icon_colors)
        self._update_icon_colors()

    def _update_icon_colors(self):
        """Updates all icon colors based on the current theme."""
        disabled_color = self.controller.theme_manager.get_style("ICON_COLOR_DISABLED")

        # The NavButtonHoverFilter will handle nav button icon updates automatically.

        # Update spinner icon color
        self.icon_spinner = qta.icon('fa5s.spinner', color=disabled_color, animation=self.omniparser_spinner)

        self._update_vision_sections_state()

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
            "common": ("fa5s.cogs", "Common"),
            "apps": ("fa5s.th-large", "Applications"),
            "hotkeys": ("fa5s.keyboard", "Hotkeys"),
            "compact": ("fa5s.compress-arrows-alt", "Compact Mode"),
            "browser": ("fa5s.globe", "Browser"),
            "terminal": ("fa5s.terminal", "Terminal"),
            "vision": ("fa5s.robot", "Vision"),
            "context": ("fa5s.file-alt", "Context"),
            "omniparser": ("fa5s.eye", "OmniParser"),
            "debug": ("fa5s.bug", "Debug")
        }

        for key, (icon_name, tooltip) in self.nav_sections.items():
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
        """Creates the right-side scroll area containing all setting sections."""
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setMinimumWidth(400)

        scroll_content = QWidget()
        content_layout = QVBoxLayout(scroll_content)
        content_layout.setContentsMargins(15, 15, 15, 15)
        content_layout.setSpacing(20)
        content_layout.setAlignment(Qt.AlignTop)

        sections = {
            "general": self._create_general_section(),
            "common": self._create_common_section(),
            "apps": self._create_apps_section(),
            "hotkeys": self._create_hotkeys_section(),
            "compact": self._create_compact_mode_section(),
            "browser": self._create_browser_section(),
            "terminal": self._create_terminal_section(),
            "vision": self._create_vision_section(),
            "context": self._create_context_section(),
            "omniparser": self._create_omniparser_section(),
            "debug": self._create_debug_section()
        }

        for key in self.nav_sections:
            content_layout.addWidget(sections[key])

        scroll_area.setWidget(scroll_content)
        scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        return scroll_area, sections

    def _on_nav_button_clicked(self, button):
        """Scrolls to the corresponding section when a nav button is clicked."""
        section_key = button.property("section_key")
        if section_key in self.sections:
            self._is_programmatic_scroll = True
            self.scroll_area.ensureWidgetVisible(self.sections[section_key], 100, 100)
            # Reset the flag after the animation has had time to finish
            QTimer.singleShot(300, self._reset_programmatic_scroll_flag)

        self._update_all_nav_button_icons()

    def _reset_programmatic_scroll_flag(self):
        """Resets the flag that prevents the scroll handler from running."""
        self._is_programmatic_scroll = False

    def _on_scroll_changed(self, value):
        """Updates the checked navigation button based on the scroll position."""
        if self._is_programmatic_scroll:
            return

        # Add a small offset so the button switches when the title is fully visible
        scroll_position = value + 10
        current_section_key = None

        # Find the last section that is scrolled past the top of the viewport
        for key, widget in self.sections.items():
            if widget.y() <= scroll_position:
                current_section_key = key
            else:
                # Since widgets are ordered from top to bottom, we can stop early.
                break

        if current_section_key:
            for button in self.nav_button_group.buttons():
                if button.property("section_key") == current_section_key:
                    if not button.isChecked():
                        # Block signals to prevent the button click from re-triggering a scroll
                        self.nav_button_group.blockSignals(True)
                        button.setChecked(True)
                        self.nav_button_group.blockSignals(False)
                    break

        self._update_all_nav_button_icons()

    def showEvent(self, event):
        """Reload settings and set initial state when shown."""
        self.load_settings()
        # Set the first button as checked by default
        first_button = self.nav_button_group.buttons()[0]
        first_button.setChecked(True)
        self._on_nav_button_clicked(first_button)
        super().showEvent(event)

    def _on_vision_module_ready(self):
        """Slot to enable vision-related sections when the module is ready."""
        self._update_vision_sections_state()

    def _update_vision_sections_state(self):
        """Enables or disables vision-related sections based on controller state."""
        is_ready = self.controller.is_vision_module_initialized

        self.omniparser_stack.setCurrentIndex(1 if is_ready else 0)
        self.debug_stack.setCurrentIndex(1 if is_ready else 0)

        for button in self.nav_button_group.buttons():
            key = button.property("section_key")
            if key not in ["omniparser", "debug"]:
                continue

            button.setEnabled(is_ready)
            spinner = self.omniparser_spinner if key == "omniparser" else self.debug_spinner

            filter_instance = button.findChild(NavButtonHoverFilter)
            if is_ready:
                spinner.stop()
                if filter_instance:
                    filter_instance.force_update()
            else:
                # The spinner icon's color is managed by _update_icon_colors
                button.setIcon(self.icon_spinner)
                spinner.start()


    def closeEvent(self, event):
        """Disconnect the signal when the window is closed."""
        try:
            self.controller.vision_module_ready.disconnect(self._on_vision_module_ready)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)

    def load_settings(self):
        """Loads settings from the manager and updates the UI."""
        # General
        active_provider = self.settings_manager.get("active_provider", "google_gemini")
        providers = self.settings_manager.get("llm_providers", {})
        self.llm_provider_widget.load_settings(active_provider, providers)

        # Common
        self.max_retries_spinbox.setValue(self.settings_manager.get("max_retries", 3))
        self.retry_delay_spinbox.setValue(self.settings_manager.get("retry_delay_seconds", 2))
        self.prevent_multiple_instances_checkbox.setChecked(
            self.settings_manager.get("prevent_multiple_instances", True)
        )
        self.webp_lossless_checkbox.setChecked(self.settings_manager.get("webp_lossless", True))
        self.webp_quality_spinbox.setValue(self.settings_manager.get("webp_quality", 80))
        is_lossless = self.webp_lossless_checkbox.isChecked()
        self.webp_quality_spinbox.setDisabled(is_lossless)
        self.webp_quality_label.setDisabled(is_lossless)

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

        # Vision
        vision_settings = self.settings_manager.get("vision", {})
        self.wait_check_interval_spinbox.setValue(vision_settings.get("wait_check_interval_ms", 5000) / 1000.0)
        self.post_action_delay_spinbox.setValue(vision_settings.get("post_action_delay_ms", 1000) / 1000.0)
        self.max_correction_attempts_spinbox.setValue(vision_settings.get("max_correction_attempts", 3))
        self.allow_shell_commands_checkbox.setChecked(bool(vision_settings.get("allow_shell_commands", False)))

        # OmniParser
        omniparser_settings = self.settings_manager.get("omniparser", {})
        self.box_threshold_spinbox.setValue(omniparser_settings.get("box_threshold", 0.35))
        self.text_threshold_spinbox.setValue(omniparser_settings.get("text_threshold", 0.8))
        self.containment_ratio_spinbox.setValue(omniparser_settings.get("containment_ratio", 0.8))
        selected_lang_codes = omniparser_settings.get("ocr_languages", [])
        self.ocr_language_widget.load_languages(selected_lang_codes)

        # Debug
        self.font_combo.set_selected_font(omniparser_settings.get("font_path", "cv2_default"))
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
        self.regenerate_plan_hotkey.set_key_sequence(hotkey_settings.get("regenerate_plan", "Ctrl+Shift+R"))
        self.edit_plan_hotkey.set_key_sequence(hotkey_settings.get("edit_plan", "Ctrl+Shift+E"))
        self.cancel_plan_hotkey.set_key_sequence(hotkey_settings.get("cancel_plan", "Ctrl+Shift+D"))
        self.run_plan_hotkey.set_key_sequence(hotkey_settings.get("run_plan", "Ctrl+Return"))

        # Context (agent history compaction; all defaults = send everything)
        context_settings = self.settings_manager.get("context", {})
        self.history_keep_last_spinbox.setValue(context_settings.get("history_keep_last_n", 0))
        self.history_max_chars_spinbox.setValue(context_settings.get("history_max_result_chars", 0))
        self.history_dedup_checkbox.setChecked(bool(context_settings.get("history_dedup_snapshots", False)))
        self.history_trim_tracebacks_checkbox.setChecked(bool(context_settings.get("history_trim_tracebacks", False)))

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
        active_provider_to_save, providers_to_save = self.llm_provider_widget.get_all_settings()

        omniparser_settings = self.settings_manager.get("omniparser", {}).copy()

        selected_lang_codes = self.ocr_language_widget.get_selected_languages()

        omniparser_settings.update({
            "font_path": self.font_combo.get_selected_font_path(),
            "font_size": self.font_size_spinbox.value(),
            "text_padding": self.text_padding_spinbox.value(),
            "box_threshold": self.box_threshold_spinbox.value(),
            "text_threshold": self.text_threshold_spinbox.value(),
            "containment_ratio": self.containment_ratio_spinbox.value(),
            "save_trace_log": self.save_trace_log_checkbox.isChecked(),
            "save_pre_intent_screenshot": self.save_pre_intent_checkbox.isChecked(),
            "save_post_screenshot": self.save_post_checkbox.isChecked(),
            "ocr_languages": selected_lang_codes
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
            "cancel_request": self.cancel_request_hotkey.get_key_sequence(),
            "regenerate_plan": self.regenerate_plan_hotkey.get_key_sequence(),
            "edit_plan": self.edit_plan_hotkey.get_key_sequence(),
            "cancel_plan": self.cancel_plan_hotkey.get_key_sequence(),
            "run_plan": self.run_plan_hotkey.get_key_sequence()
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

        vision_settings = {
            "wait_check_interval_ms": int(self.wait_check_interval_spinbox.value() * 1000),
            "post_action_delay_ms": int(self.post_action_delay_spinbox.value() * 1000),
            "max_correction_attempts": self.max_correction_attempts_spinbox.value(),
            "allow_shell_commands": self.allow_shell_commands_checkbox.isChecked(),
        }
        # Preserve keys with no UI widget (e.g. grounding_backend) so saving
        # settings never silently drops them.
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
            "omniparser": omniparser_settings,
            "allowed_apps": new_allowed_apps,
            "hotkeys": hotkeys_to_save,
            "max_retries": self.max_retries_spinbox.value(),
            "retry_delay_seconds": self.retry_delay_spinbox.value(),
            "prevent_multiple_instances": self.prevent_multiple_instances_checkbox.isChecked(),
            "vision": vision_settings,
            "browser": browser_settings,
            "terminal": terminal_settings,
            "compact_mode": compact_mode_settings,
            "context": context_settings,
            "webp_lossless": self.webp_lossless_checkbox.isChecked(),
            "webp_quality": self.webp_quality_spinbox.value(),
            "theme": theme,
            "search": search_settings
        }

    def save_and_accept(self):
        """Saves settings and handles OCR language updates if necessary."""
        self.all_settings_to_save = self._collect_settings_from_ui()
        new_ocr_languages = set(self.all_settings_to_save["omniparser"]["ocr_languages"])

        if new_ocr_languages != self.initial_ocr_languages:
            if self._trigger_language_update():
                logging.info("Language update finished, saving settings.")
                self.settings_manager.set_many(self.all_settings_to_save)
                self.hide()
            else:
                logging.warning("Language model download was cancelled. Settings not saved.")
        else:
            self.settings_manager.set_many(self.all_settings_to_save)
            self.hide()

    def _trigger_language_update(self):
        """Handles the UI and worker thread for updating languages."""
        vision_manager = self.controller.vision
        if not vision_manager:
            logging.error("Vision manager not available, cannot update languages.")
            return False

        self.progress_dialog = QProgressDialog("Initializing language models...", "Cancel", 0, 0, self)
        self.progress_dialog.setWindowTitle("Downloading models")
        self.progress_dialog.setStyleSheet(self.controller.theme_manager.get_style("PROGRESS_BAR_STYLE"))
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.setRange(0, 0)
        self.progress_dialog.setValue(0)

        update_thread = QThread()
        update_worker = LanguageUpdateWorker(vision_manager, self.all_settings_to_save)
        update_worker.moveToThread(update_thread)

        update_worker.download_status_changed.connect(self._on_download_status_changed)
        update_worker.finished.connect(self.progress_dialog.accept)
        update_worker.error.connect(self.progress_dialog.reject)
        self.progress_dialog.canceled.connect(update_thread.quit)

        update_thread.finished.connect(update_worker.deleteLater)
        update_thread.finished.connect(update_thread.deleteLater)

        update_thread.started.connect(update_worker.run)
        update_thread.start()

        result = self.progress_dialog.exec()

        if update_thread.isRunning():
            update_thread.quit()
            update_thread.wait()

        return result == QDialog.Accepted

    def _on_download_status_changed(self, status: str):
        """Updates the progress dialog label based on download status."""
        if 'downloading' in status.lower():
            self.progress_dialog.setLabelText("Downloading language models... This may take several minutes.")
        elif 'complete' in status.lower():
            self.progress_dialog.setLabelText("Download complete. Finalizing...")
        else:
            self.progress_dialog.setLabelText(status)

    def _create_general_section(self):
        """Creates the General settings section."""
        group = QGroupBox("General")
        layout = QVBoxLayout(group)
        self.llm_provider_widget = LLMProviderWidget(self.settings_manager)
        layout.addWidget(self.llm_provider_widget)

        return group

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
        self.regenerate_plan_hotkey = HotkeyEdit()
        layout.addRow("Regenerate Plan:", self.regenerate_plan_hotkey)
        self.edit_plan_hotkey = HotkeyEdit()
        layout.addRow("Edit Plan:", self.edit_plan_hotkey)
        self.cancel_plan_hotkey = HotkeyEdit()
        layout.addRow("Cancel Plan:", self.cancel_plan_hotkey)
        self.run_plan_hotkey = HotkeyEdit()
        layout.addRow("Run Plan:", self.run_plan_hotkey)

        return group

    def _create_apps_section(self):
        """Creates the Apps settings section."""
        group = QGroupBox("Applications")
        layout = QVBoxLayout(group)

        self.scan_prompt_label = QLabel("Application cache not found. Please scan to discover all applications.")
        self.scan_prompt_label.setStyleSheet("font-style: italic; color: #D32F2F; font-weight: bold;")
        self.scan_prompt_label.setVisible(False)
        layout.addWidget(self.scan_prompt_label)

        scan_button = QPushButton("Scan for Applications")
        scan_button.clicked.connect(self.force_scan_and_populate)
        layout.addWidget(scan_button)

        self.apps_list_widget = QListWidget()
        self.apps_list_widget.setToolTip("Select the applications the assistant is allowed to launch.")
        self.apps_list_widget.setFixedHeight(250) # Set a fixed height for the list
        layout.addWidget(self.apps_list_widget)

        return group

    def _populate_apps_list(self, all_apps):
        """Helper function to populate the QListWidget with a list of apps."""
        try:
            all_apps.sort(key=lambda x: x['name'].lower())
        except Exception as e:
            logging.error(f"Could not sort apps list: {e}")

        allowed_apps = self.settings_manager.get('allowed_apps', [])
        allowed_app_ids = {AppScanner._get_unique_app_id(app) for app in allowed_apps}

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

        compression_group = QGroupBox("Screenshot Compression")
        compression_layout = QGridLayout(compression_group)
        compression_layout.setColumnStretch(2, 1)
        self.webp_lossless_checkbox = QCheckBox()
        webp_lossless_tooltip = "Enable lossless WebP compression for screenshots."
        compression_layout.addWidget(QLabel("Lossless Compression:"), 0, 0)
        compression_layout.addWidget(self._create_help_label(webp_lossless_tooltip), 0, 1, Qt.AlignLeft)
        compression_layout.addWidget(self.webp_lossless_checkbox, 0, 3)
        self.webp_quality_spinbox = QSpinBox()
        self.webp_quality_spinbox.setRange(1, 100)
        self.webp_quality_spinbox.setFixedWidth(80)
        webp_quality_tooltip = "Sets the quality for lossy WebP compression (1-100)."
        self.webp_quality_label = QLabel("Compression Quality:")
        compression_layout.addWidget(self.webp_quality_label, 1, 0)
        compression_layout.addWidget(self._create_help_label(webp_quality_tooltip), 1, 1, Qt.AlignLeft)
        compression_layout.addWidget(self.webp_quality_spinbox, 1, 3)
        self.webp_lossless_checkbox.toggled.connect(self.webp_quality_spinbox.setDisabled)
        self.webp_lossless_checkbox.toggled.connect(self.webp_quality_label.setDisabled)
        tab_layout.addWidget(compression_group)

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
        """Creates the Vision  Mode settings section."""
        group = QGroupBox("Vision Mode")
        vision_layout = QGridLayout(group)
        vision_layout.setColumnStretch(2, 1)

        self.wait_check_interval_spinbox = QDoubleSpinBox()
        self.wait_check_interval_spinbox.setRange(1.0, 30.0)
        self.wait_check_interval_spinbox.setSingleStep(0.5)
        self.wait_check_interval_spinbox.setSuffix(" s")
        self.wait_check_interval_spinbox.setFixedWidth(80)
        wait_check_tooltip = "How often the assistant checks if a 'wait' condition is met."
        vision_layout.addWidget(QLabel("Wait Check Interval:"), 0, 0)
        vision_layout.addWidget(self._create_help_label(wait_check_tooltip), 0, 1, Qt.AlignLeft)
        vision_layout.addWidget(self.wait_check_interval_spinbox, 0, 3)

        self.post_action_delay_spinbox = QDoubleSpinBox()
        self.post_action_delay_spinbox.setRange(0.5, 10.0)
        self.post_action_delay_spinbox.setSingleStep(0.1)
        self.post_action_delay_spinbox.setSuffix(" s")
        self.post_action_delay_spinbox.setFixedWidth(80)
        post_action_tooltip = "How long the assistant waits after an action before evaluating the outcome."
        vision_layout.addWidget(QLabel("Post-Action Delay:"), 1, 0)
        vision_layout.addWidget(self._create_help_label(post_action_tooltip), 1, 1, Qt.AlignLeft)
        vision_layout.addWidget(self.post_action_delay_spinbox, 1, 3)

        self.max_correction_attempts_spinbox = QSpinBox()
        self.max_correction_attempts_spinbox.setRange(1, 10)
        self.max_correction_attempts_spinbox.setFixedWidth(80)
        max_correction_tooltip = "Max number of times the assistant will try to correct a failed step."
        vision_layout.addWidget(QLabel("Max Correction Attempts:"), 2, 0)
        vision_layout.addWidget(self._create_help_label(max_correction_tooltip), 2, 1, Qt.AlignLeft)
        vision_layout.addWidget(self.max_correction_attempts_spinbox, 2, 3)

        self.allow_shell_commands_checkbox = QCheckBox("Allow shell commands in Vision plans")
        shell_tooltip = ("Lets Vision plans mix mouse/keyboard actions with shell and file "
                         "commands (execute_shell, read/write files). Disabled by default.")
        self.allow_shell_commands_checkbox.setToolTip(shell_tooltip)
        vision_layout.addWidget(self.allow_shell_commands_checkbox, 3, 0, 1, 4)

        return group

    def _create_context_section(self):
        """Creates the Context (agent history compaction) settings section."""
        group = QGroupBox("Agent History (browser mode)")
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
        context_layout.addWidget(QLabel("Keep last N steps full:"), 0, 0)
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
        context_layout.addWidget(QLabel("Max chars per entry:"), 1, 0)
        context_layout.addWidget(self._create_help_label(max_chars_tooltip), 1, 1, Qt.AlignLeft)
        context_layout.addWidget(self.history_max_chars_spinbox, 1, 3)

        self.history_dedup_checkbox = QCheckBox()
        dedup_tooltip = (
            "Drop consecutive identical page snapshots, keeping one copy with "
            "an '(unchanged)' note. Snapshots repeat a lot and cost tokens."
        )
        context_layout.addWidget(QLabel("Dedup repeated snapshots:"), 2, 0)
        context_layout.addWidget(self._create_help_label(dedup_tooltip), 2, 1, Qt.AlignLeft)
        context_layout.addWidget(self.history_dedup_checkbox, 2, 3)

        self.history_trim_tracebacks_checkbox = QCheckBox()
        trim_tooltip = (
            "Cut long Python tracebacks in tool errors to head+tail lines. "
            "The error type and location (what teaches the model) are kept."
        )
        context_layout.addWidget(QLabel("Trim long tracebacks:"), 3, 0)
        context_layout.addWidget(self._create_help_label(trim_tooltip), 3, 1, Qt.AlignLeft)
        context_layout.addWidget(self.history_trim_tracebacks_checkbox, 3, 3)

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

    def _create_debug_section(self):
        """Creates the Debug settings section with a loading placeholder."""
        self.debug_stack = QStackedWidget()

        # --- Page 1: Loading Placeholder ---
        loading_widget = QWidget()
        loading_layout = QVBoxLayout(loading_widget)
        loading_layout.setAlignment(Qt.AlignCenter)
        loading_label = QLabel("Initializing Vision module, please wait...")
        loading_label.setStyleSheet("font-style: italic; color: #9E9E9E;")
        loading_layout.addWidget(loading_label)
        self.debug_stack.addWidget(loading_widget)

        # --- Page 2: Actual Settings ---
        group = QGroupBox("Debug")
        form_layout = QVBoxLayout(group)
        form_layout.setContentsMargins(10, 20, 10, 10)
        form_layout.setSpacing(15)

        debug_group = QGroupBox("Debugging")
        debug_layout = QGridLayout(debug_group)
        debug_layout.setColumnStretch(2, 1)
        self.save_trace_log_checkbox = QCheckBox("Save detailed trace log")
        trace_log_tooltip = "Saves a JSON file of all detected UI elements after each screen analysis."
        debug_layout.addWidget(self.save_trace_log_checkbox, 0, 0)
        debug_layout.addWidget(self._create_help_label(trace_log_tooltip), 0, 1)
        self.save_pre_intent_checkbox = QCheckBox("Save pre-action screenshot")
        pre_intent_tooltip = "Saves a screenshot before each action, showing what the assistant intends to do."
        debug_layout.addWidget(self.save_pre_intent_checkbox, 1, 0)
        debug_layout.addWidget(self._create_help_label(pre_intent_tooltip), 1, 1)
        self.save_post_checkbox = QCheckBox("Save post-action screenshot")
        post_action_tooltip = "Saves a screenshot after each action for evaluation."
        debug_layout.addWidget(self.save_post_checkbox, 2, 0)
        debug_layout.addWidget(self._create_help_label(post_action_tooltip), 2, 1)
        form_layout.addWidget(debug_group)

        annotation_group = QGroupBox("Annotation Settings")
        annotation_layout = QGridLayout(annotation_group)
        annotation_layout.setColumnStretch(2, 1)
        self.font_combo = FontComboBox()
        font_tooltip = "Font used for text on the debug screenshots."
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
        self.font_size_spinbox.setRange(10, 20)
        self.font_size_spinbox.setSuffix(" px")
        self.font_size_spinbox.setFixedWidth(80)
        font_size_tooltip = "Font size for text labels on debug screenshots."
        annotation_layout.addWidget(QLabel("Font Size:"), 1, 0)
        annotation_layout.addWidget(self._create_help_label(font_size_tooltip), 1, 1, Qt.AlignLeft)
        annotation_layout.addWidget(self.font_size_spinbox, 1, 3)
        self.text_padding_spinbox = QSpinBox()
        self.text_padding_spinbox.setRange(0, 5)
        self.text_padding_spinbox.setSuffix(" px")
        self.text_padding_spinbox.setFixedWidth(80)
        padding_tooltip = "Padding around text labels on debug screenshots."
        annotation_layout.addWidget(QLabel("Padding:"), 2, 0)
        annotation_layout.addWidget(self._create_help_label(padding_tooltip), 2, 1, Qt.AlignLeft)
        annotation_layout.addWidget(self.text_padding_spinbox, 2, 3)
        form_layout.addWidget(annotation_group)

        self.debug_stack.addWidget(group)
        return self.debug_stack

    def _create_omniparser_section(self):
        """Creates the OmniParser settings section with a loading placeholder."""
        self.omniparser_stack = QStackedWidget()

        # --- Page 1: Loading Placeholder ---
        loading_widget = QWidget()
        loading_layout = QVBoxLayout(loading_widget)
        loading_layout.setAlignment(Qt.AlignCenter)
        loading_label = QLabel("Initializing Vision module, please wait...")
        loading_label.setStyleSheet("font-style: italic; color: #9E9E9E;")
        loading_layout.addWidget(loading_label)
        self.omniparser_stack.addWidget(loading_widget)

        # --- Page 2: Actual Settings ---
        group = QGroupBox("OmniParser")
        form_layout = QVBoxLayout(group)
        form_layout.setContentsMargins(10, 20, 10, 10)
        form_layout.setSpacing(15)

        recognition_group = QGroupBox("Recognition Settings")
        recognition_layout = QGridLayout(recognition_group)
        recognition_layout.setColumnStretch(2, 1)
        self.box_threshold_spinbox = QDoubleSpinBox()
        self.box_threshold_spinbox.setRange(0.01, 1.0)
        self.box_threshold_spinbox.setSingleStep(0.01)
        self.box_threshold_spinbox.setDecimals(2)
        self.box_threshold_spinbox.setFixedWidth(80)
        box_threshold_tooltip = "Confidence threshold for detecting UI elements."
        recognition_layout.addWidget(QLabel("Box Threshold:"), 0, 0)
        recognition_layout.addWidget(self._create_help_label(box_threshold_tooltip), 0, 1, Qt.AlignLeft)
        recognition_layout.addWidget(self.box_threshold_spinbox, 0, 3)
        self.text_threshold_spinbox = QDoubleSpinBox()
        self.text_threshold_spinbox.setRange(0.1, 1.0)
        self.text_threshold_spinbox.setSingleStep(0.1)
        self.text_threshold_spinbox.setDecimals(2)
        self.text_threshold_spinbox.setFixedWidth(80)
        text_threshold_tooltip = "Confidence threshold for recognizing text (OCR)."
        recognition_layout.addWidget(QLabel("Text Threshold:"), 1, 0)
        recognition_layout.addWidget(self._create_help_label(text_threshold_tooltip), 1, 1, Qt.AlignLeft)
        recognition_layout.addWidget(self.text_threshold_spinbox, 1, 3)
        self.containment_ratio_spinbox = QDoubleSpinBox()
        self.containment_ratio_spinbox.setRange(0.5, 1.0)
        self.containment_ratio_spinbox.setSingleStep(0.1)
        self.containment_ratio_spinbox.setDecimals(2)
        self.containment_ratio_spinbox.setFixedWidth(80)
        containment_ratio_tooltip = "How much of a text block must be inside a UI element to be associated."
        recognition_layout.addWidget(QLabel("Containment Ratio:"), 2, 0)
        recognition_layout.addWidget(self._create_help_label(containment_ratio_tooltip), 2, 1, Qt.AlignLeft)
        recognition_layout.addWidget(self.containment_ratio_spinbox, 2, 3)
        self.ocr_language_widget = OCRLanguageWidget(self.settings_manager, self.controller.theme_manager)
        self.ocr_language_widget.setFixedHeight(150)
        ocr_tooltip = "Select languages for text recognition (OCR)."
        label_ocr = QLabel("OCR Languages:")
        recognition_layout.addWidget(label_ocr, 3, 0)
        recognition_layout.addWidget(self._create_help_label(ocr_tooltip), 3, 1)
        recognition_layout.addWidget(self.ocr_language_widget, 4, 0, 1, 4)
        form_layout.addWidget(recognition_group)

        self.omniparser_stack.addWidget(group)

        return self.omniparser_stack
