"""Full-window settings page.

The page replaces the chat workspace (or the home screen) inside the main
window: a labelled sidebar on the left, one scrollable page per section, no
OK/Cancel. Every edit is saved shortly after it is made (the Providers and
MCP pages stage their own writes in the settings manager), so leaving the
page never discards anything.
"""
import copy
import os
import sys
import logging

import qtawesome as qta
from PySide6.QtWidgets import (
    QVBoxLayout,
    QWidget,
    QLabel,
    QHBoxLayout,
    QGroupBox,
    QFormLayout,
    QGridLayout,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QComboBox,
    QListWidget,
    QLineEdit,
    QListWidgetItem,
    QButtonGroup,
    QStackedWidget,
    QScrollArea,
    QFrame,
    QRadioButton,
    QMessageBox,
    QPlainTextEdit,
)
from PySide6.QtCore import Qt, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPalette, QKeySequence, QShortcut
from src.ui.theme_manager import (
    LIGHT_THEME,
    DARK_THEME,
    create_dark_palette,
    create_light_palette,
)
from src.ui.navigation import _theme_colors

from settings_manager import SettingsManager
from app_paths import resource_path
from context import skills as skills_store
from context import context_stats as context_stats_store
from context import handoff as handoff_store
from build_info import APP_VERSION, get_commit
from src.ui.widgets.font_manager.font_combobox import FontComboBox
from src.ui.widgets.hotkey.hotkey_edit import HotkeyEdit
from src.apps.installed_apps import AppScanner
from src.ui.widgets.providers_page import ProvidersPage
from src.ui.skill_catalog_dialog import SkillCatalogDialog
from src.ui.mcp_settings_page import MCPSettingsPage
from src.ui.utils import CustomToolTipManager

CONTEXT_SETTINGS_DOC_URL = (
    "https://github.com/AIgrator/Logos/blob/main/docs/context_settings.md")
PROJECT_URL = "https://github.com/AIgrator/Logos"
ISSUES_URL = f"{PROJECT_URL}/issues"
LICENSE_FILE = os.path.join(
    getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "LICENSE")


def _read_license_text() -> str:
    try:
        with open(LICENSE_FILE, encoding="utf-8") as license_file:
            return license_file.read().strip()
    except OSError as e:
        logging.warning(f"Could not read license file {LICENSE_FILE}: {e}")
        return "MIT License"


class SettingsPage(QWidget):
    """Full-window settings: labelled sections, live autosave, Back to app."""

    back_requested = Signal()

    SIDEBAR_WIDTH = 248
    # Settings read as a compact column instead of spanning the whole window.
    CONTENT_MAX_WIDTH = 880
    # The rail and the content travel as one centered block; the 56 px are
    # the content page's left and right margins, so the rail sits right next
    # to the column instead of being pinned to the window edge.
    BODY_MAX_WIDTH = SIDEBAR_WIDTH + CONTENT_MAX_WIDTH + 56
    # Nav groups in display order; a separator line sits between groups.
    # Modes follow the mode selector order: Terminal, Browser, Vision.
    NAV_GROUPS = (
        ("providers",),
        ("common", "hotkeys", "context"),
        ("terminal", "browser", "vision"),
        ("skills", "mcp"),
    )
    SAVE_DELAY_MS = 400

    def __init__(self, settings_manager: SettingsManager, controller, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.controller = controller
        self.setObjectName("SettingsPage")

        self.tooltip_manager = CustomToolTipManager(self)
        self._context_doc_links = []
        self._loading = False
        self._saved_state = {}
        self._fonts_scheduled = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QWidget()
        body.setObjectName("SettingsBody")
        body.setMaximumWidth(self.BODY_MAX_WIDTH)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._create_nav_bar())

        _, self.sections = self._create_content_area()
        body_layout.addWidget(self.section_stack, 1)

        # The dominant stretch grows the body to its cap before the side
        # stretches claim the rest, so the block stays centered.
        root.addStretch(1)
        root.addWidget(body, 1000)
        root.addStretch(1)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(self.SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._apply_changes)

        self._wire_autosave()
        self.providers_page.changed.connect(self._schedule_save)

        self._escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._escape_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self._on_escape)

        self.controller.theme_manager.theme_changed.connect(self._apply_style)
        self.controller.theme_manager.theme_changed.connect(self._refresh_context_doc_links)
        self._apply_style()

        first_button = self.nav_button_group.buttons()[0]
        first_button.setChecked(True)
        self._show_section(first_button)

    # --- sidebar ----------------------------------------------------------

    def _create_nav_bar(self):
        """Left rail: Back, grouped section buttons, About pinned."""
        self.nav_panel = QWidget()
        self.nav_panel.setObjectName("SettingsNav")
        self.nav_panel.setFixedWidth(self.SIDEBAR_WIDTH)
        layout = QVBoxLayout(self.nav_panel)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(6)

        self.back_button = QPushButton("Back to application")
        self.back_button.setObjectName("settingsBack")
        self.back_button.setToolTip("Close settings and return to the workspace (Esc)")
        self.back_button.clicked.connect(self._request_back)
        layout.addWidget(self.back_button)

        self.nav_sections = {
            "providers": ("fa5s.brain", "Providers"),
            "common": ("fa5s.cogs", "Common"),
            "hotkeys": ("fa5s.keyboard", "Hotkeys"),
            "context": ("fa5s.file-alt", "Context"),
            "terminal": ("fa5s.terminal", "Terminal"),
            "browser": ("fa5s.globe", "Browser"),
            "vision": ("fa5s.robot", "Vision"),
            "skills": ("fa5s.puzzle-piece", "Skills"),
            "mcp": ("fa5s.server", "MCP Servers"),
            "about": ("fa5s.info-circle", "About"),
        }

        nav_host = QWidget()
        nav_layout = QVBoxLayout(nav_host)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(1)

        self.nav_button_group = QButtonGroup(self)
        self.nav_button_group.setExclusive(True)
        self.nav_button_group.buttonClicked.connect(self._on_nav_button_clicked)

        self._nav_buttons = []
        for group_index, group in enumerate(self.NAV_GROUPS):
            if group_index:
                nav_layout.addWidget(self._create_nav_separator())
            for key in group:
                nav_layout.addWidget(self._create_nav_button(key))
        # Keep the buttons at their natural height on tall windows.
        nav_layout.addStretch(1)

        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        nav_scroll.setWidget(nav_host)
        layout.addWidget(nav_scroll, 1)

        layout.addWidget(self._create_nav_separator())
        layout.addWidget(self._create_nav_button("about"))

        return self.nav_panel

    def _create_nav_button(self, key):
        icon_name, label = self.nav_sections[key]
        button = QPushButton(label)
        button.setObjectName("settingsNavButton")
        button.setCheckable(True)
        button.setProperty("section_key", key)
        button.setProperty("icon_name", icon_name)
        button.setIconSize(QSize(16, 16))
        button.setCursor(Qt.PointingHandCursor)
        self.nav_button_group.addButton(button)
        self._nav_buttons.append((button, label))
        return button

    def _create_nav_separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("settingsNavSeparator")
        line.setFixedHeight(1)
        return line

    def _apply_style(self):
        """Re-tints icons and rebuilds the rail sheet for the current theme."""
        colors = _theme_colors(self.controller.theme_manager)
        self.nav_panel.setStyleSheet(f"""
            QWidget#SettingsNav {{
                background: {colors['panel']};
                border-right: 1px solid {colors['border']};
            }}
            QFrame#settingsNavSeparator {{ background: {colors['border']}; border: none; }}
            QPushButton#settingsNavButton {{
                background: transparent; color: {colors['text']};
                border: none; border-radius: 7px;
                padding: 7px 11px; text-align: left;
            }}
            QPushButton#settingsNavButton:hover {{ background: {colors['hover']}; }}
            QPushButton#settingsNavButton:checked {{
                background: {colors['selected']}; font-weight: 600;
            }}
            QPushButton#settingsBack {{
                background: transparent; color: {colors['muted']};
                border: none; border-radius: 7px;
                padding: 7px 11px; text-align: left;
            }}
            QPushButton#settingsBack:hover {{
                background: {colors['hover']}; color: {colors['text']};
            }}
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: transparent; width: 6px; margin: 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background: {colors['border']}; min-height: 24px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {colors['muted']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; background: none; border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QScrollBar:horizontal {{ height: 0; }}
        """)
        self.back_button.setIcon(
            qta.icon("fa5s.arrow-left", color=colors["muted"]))
        icon_color = colors["text"]
        for button, _label in self._nav_buttons:
            button.setIcon(
                qta.icon(button.property("icon_name"), color=icon_color))

    # --- content ----------------------------------------------------------

    def _create_content_area(self):
        """Creates the right-side area: one independent page per section.

        Each nav button shows ONLY its section (QStackedWidget). Every page
        is wrapped in its own scroll area whose vertical bar appears only
        when that section doesn't fit the height (AsNeeded) — no shared
        scrollbar anymore. Inside, the section sits in a column capped at
        CONTENT_MAX_WIDTH and centered, so settings stay compact instead of
        stretching across a wide window.
        """
        sections = {
            "providers": self._create_providers_section(),
            "common": self._create_common_section(),
            "hotkeys": self._create_hotkeys_section(),
            "browser": self._create_browser_section(),
            "terminal": self._create_terminal_section(),
            "mcp": self._create_mcp_section(),
            "skills": self._create_skills_section(),
            "vision": self._create_vision_section(),
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
            row = QHBoxLayout(holder)
            row.setContentsMargins(28, 22, 28, 22)
            row.setSpacing(0)
            column = QWidget()
            column.setMaximumWidth(self.CONTENT_MAX_WIDTH)
            column_layout = QVBoxLayout(column)
            column_layout.setContentsMargins(0, 0, 0, 0)
            column_layout.setSpacing(0)
            column_layout.addWidget(sections[key])
            row.addStretch(1)
            row.addWidget(column, 1000)
            row.addStretch(1)
            page.setWidget(holder)
            self.section_stack.addWidget(page)
            self.section_order.append(key)

        self.section_stack.setMinimumWidth(400)
        return self.section_stack, sections

    def _show_section(self, button):
        """Shows only the selected section's page."""
        section_key = button.property("section_key")
        if section_key in self.section_order:
            self.section_stack.setCurrentIndex(
                self.section_order.index(section_key))

    def _on_nav_button_clicked(self, button):
        self._show_section(button)

    # --- lifecycle and autosave ------------------------------------------

    def showEvent(self, event):
        """Reload settings each time the page becomes visible."""
        super().showEvent(event)
        self._reload()
        if not self._fonts_scheduled:
            # Registering every OS font with Qt takes ~0.8 s; do it after the
            # page is painted so opening Settings never waits for it.
            self._fonts_scheduled = True
            QTimer.singleShot(100, self.font_combo.populate_fonts)

    def hideEvent(self, event):
        """Persist pending edits when the page is left or the app closes."""
        self.flush_changes()
        page = getattr(self, "mcp_page", None)
        if page is not None:
            page.commit()
        super().hideEvent(event)

    def _on_escape(self):
        self._request_back()

    def _request_back(self):
        self.flush_changes()
        page = getattr(self, "mcp_page", None)
        if page is not None:
            page.commit()
        self.back_requested.emit()

    def _reload(self):
        """Loads values without autosaving the widgets while they change."""
        self._loading = True
        try:
            self.load_settings()
        finally:
            self._loading = False
        self._saved_state = copy.deepcopy(self._collect_settings_from_ui())

    def _wire_autosave(self):
        """Schedules a save whenever a value control changes."""
        for signal in (
            self.max_retries_spinbox.valueChanged,
            self.retry_delay_spinbox.valueChanged,
            self.max_errors_spinbox.valueChanged,
            self.prevent_multiple_instances_checkbox.toggled,
            self.light_theme_radio.toggled,
            self.dark_theme_radio.toggled,
            self.block_ads_checkbox.toggled,
            self.browser_network_combo.currentIndexChanged,
            self.browser_headless_checkbox.toggled,
            self.browser_timeout_spinbox.valueChanged,
            self.terminal_access_combo.currentIndexChanged,
            self.shell_timeout_spinbox.valueChanged,
            self.shell_detach_spinbox.valueChanged,
            self.allow_shell_commands_checkbox.toggled,
            self.allow_system_surfaces_checkbox.toggled,
            self.restrict_to_allowed_apps_checkbox.toggled,
            self.max_turns_spinbox.valueChanged,
            self.shot_width_spinbox.valueChanged,
            self.save_trace_log_checkbox.toggled,
            self.save_pre_intent_checkbox.toggled,
            self.save_post_checkbox.toggled,
            self.font_combo.currentIndexChanged,
            self.font_size_spinbox.valueChanged,
            self.text_padding_spinbox.valueChanged,
            self.history_keep_last_spinbox.valueChanged,
            self.history_max_chars_spinbox.valueChanged,
            self.history_dedup_checkbox.toggled,
            self.history_trim_tracebacks_checkbox.toggled,
            self.handoff_enabled_checkbox.toggled,
            self.handoff_model_switch_checkbox.toggled,
            self.summary_max_tokens_spinbox.valueChanged,
            self.summary_threshold_spinbox.valueChanged,
            self.summary_refresh_tokens_spinbox.valueChanged,
            self.auto_compact_checkbox.toggled,
            self.auto_compact_tokens_spinbox.valueChanged,
            self.summary_model_lineedit.textChanged,
            self.compact_mode_width_spinbox.valueChanged,
            self.compact_mode_height_spinbox.valueChanged,
            self.position_button_group.buttonClicked,
            self.toggle_mode_hotkey.textChanged,
            self.pause_resume_hotkey.textChanged,
            self.stop_execution_hotkey.textChanged,
            self.pause_for_feedback_hotkey.textChanged,
            self.cancel_request_hotkey.textChanged,
            self.apps_list_widget.itemChanged,
        ):
            signal.connect(self._schedule_save)

    def _schedule_save(self, *_args):
        if self._loading:
            return
        self._save_timer.start()

    def flush_changes(self):
        """Writes a pending edit immediately (leaving the page, app close)."""
        if self._save_timer.isActive():
            self._apply_changes()

    def _apply_changes(self):
        """Persists the current widget values; no-op when nothing changed."""
        self._save_timer.stop()
        if self._loading:
            return
        collected = self._collect_settings_from_ui()
        if collected == self._saved_state:
            return
        self._saved_state = copy.deepcopy(collected)
        self.settings_manager.set_many(collected)
        # Provider deletions prune their keychain entries once saved.
        self.providers_page.commit()

    def load_settings(self):
        """Loads settings from the manager and updates the UI."""
        # Providers (the page stages its own edits in the settings manager)
        self.providers_page.load_settings()

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
        from src.browser.policy import resolve_network_access
        network_index = self.browser_network_combo.findData(
            resolve_network_access(browser_settings))
        self.browser_network_combo.setCurrentIndex(max(0, network_index))
        self.browser_headless_checkbox.setChecked(bool(browser_settings.get("headless", False)))
        self.browser_timeout_spinbox.setValue(int(browser_settings.get("default_timeout_ms", 30000) or 30000))

        # Terminal (long tasks need a generous shell timeout)
        terminal_settings = self.settings_manager.get("terminal", {})
        access_index = self.terminal_access_combo.findData(
            terminal_settings.get("access_mode", "ask"))
        self.terminal_access_combo.setCurrentIndex(max(0, access_index))
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
        self.handoff_model_switch_checkbox.setChecked(bool(context_settings.get("handoff_on_model_switch", True)))
        self.auto_compact_checkbox.setChecked(bool(context_settings.get("auto_compact", True)))
        self.summary_max_tokens_spinbox.setValue(int(context_settings.get("summary_max_tokens", 1200)))
        self.summary_threshold_spinbox.setValue(float(context_settings.get("summary_threshold_ratio", 0.9)))
        self.summary_refresh_tokens_spinbox.setValue(int(context_settings.get(
            "summary_refresh_tokens", handoff_store.DEFAULT_REFRESH_TOKENS)))
        self.auto_compact_tokens_spinbox.setValue(int(context_settings.get(
            "auto_compact_tokens", context_stats_store.DEFAULT_SOFT_CAP_TOKENS)))
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
        self._refresh_skills()
        if getattr(self, "mcp_page", None) is not None:
            self.mcp_page.load_settings()

    def _collect_settings_from_ui(self):
        """Gathers all settings from the UI into a single dictionary."""
        # The Providers page writes its staged edits straight into the settings
        # manager, so the live values are already the final ones.
        active_provider_to_save = self.settings_manager.get(
            "active_provider", "")
        providers_to_save = copy.deepcopy(
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
            "network_access": (self.browser_network_combo.currentData()
                               or "any"),
            "headless": self.browser_headless_checkbox.isChecked(),
            "default_timeout_ms": self.browser_timeout_spinbox.value(),
        }
        # Preserve keys with no UI widget (e.g. profile_dir) so saving
        # settings never silently drops them.
        try:
            existing_browser = self.settings_manager.get("browser", {}) or {}
            for key, value in existing_browser.items():
                browser_settings.setdefault(key, value)
            # The pre-0.2 boolean is replaced by network_access; keeping it
            # would leave two sources of truth in the file.
            browser_settings.pop("block_private_networks", None)
        except Exception:
            pass

        terminal_settings = {
            "access_mode": self.terminal_access_combo.currentData() or "ask",
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
            "handoff_on_model_switch": self.handoff_model_switch_checkbox.isChecked(),
            "auto_compact": self.auto_compact_checkbox.isChecked(),
            "summary_max_tokens": self.summary_max_tokens_spinbox.value(),
            "summary_threshold_ratio": round(self.summary_threshold_spinbox.value(), 2),
            "summary_refresh_tokens": self.summary_refresh_tokens_spinbox.value(),
            "auto_compact_tokens": self.auto_compact_tokens_spinbox.value(),
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

        # The MCP page stages its edits directly in the settings manager; the
        # value here is the final (hydrated) state, sanitized when written.
        mcp_settings = self.settings_manager.get("mcp", {}) or {}
        if not isinstance(mcp_settings, dict):
            mcp_settings = {}
        mcp_settings = copy.deepcopy(mcp_settings)

        return {
            "active_provider": active_provider_to_save,
            "llm_providers": providers_to_save,
            "custom_providers": copy.deepcopy(
                self.settings_manager.get("custom_providers", []) or []),
            "mode_models": copy.deepcopy(
                self.settings_manager.get("mode_models", {}) or {}),
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
            "mcp": mcp_settings,
            "compact_mode": compact_mode_settings,
            "context": context_settings,
            "theme": theme,
            "search": search_settings
        }

    def _create_providers_section(self):
        """Provider connection and activation page."""
        self.providers_page = ProvidersPage(self.settings_manager)
        # The page's sheet resolves palette() roles at polish time; theme
        # switching in Common must repolish it while the page is open.
        self.controller.theme_manager.theme_changed.connect(
            self.providers_page.repolish)
        return self.providers_page

    @staticmethod
    def _page(widget):
        """Wraps a section so it hugs the top instead of absorbing height."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(widget)
        layout.addStretch(1)
        return page

    def _create_hotkeys_section(self):
        """Creates the Hotkeys settings section."""
        group = QGroupBox("Hotkeys")
        layout = QFormLayout(group)

        self.toggle_mode_hotkey = HotkeyEdit()
        layout.addRow("Toggle Vision:", self.toggle_mode_hotkey)
        self.pause_resume_hotkey = HotkeyEdit()
        layout.addRow("Pause/Resume Execution:", self.pause_resume_hotkey)
        self.stop_execution_hotkey = HotkeyEdit()
        layout.addRow("Stop Execution:", self.stop_execution_hotkey)
        self.pause_for_feedback_hotkey = HotkeyEdit()
        layout.addRow("Pause for Feedback:", self.pause_for_feedback_hotkey)
        self.cancel_request_hotkey = HotkeyEdit()
        layout.addRow("Cancel Request:", self.cancel_request_hotkey)

        return self._page(group)

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
            "(after one self-assessment turn). Shared by all agent panels. "
            "Applies immediately."
        )
        agent_layout.addWidget(QLabel("Failures before asking:"), 0, 0)
        agent_layout.addWidget(self._create_help_label(max_errors_tooltip), 0, 1, Qt.AlignLeft)
        agent_layout.addWidget(self.max_errors_spinbox, 0, 3)
        tab_layout.addWidget(agent_group)

        return self._page(group)

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
            "The compact window is used automatically during Vision "
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
        """Vision page: agent loop, compact window and allowed applications."""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(12)
        model_note = QLabel(
            "The model used by Vision is chosen on the Providers page "
            "(Vision model).")
        model_note.setStyleSheet("color: #9E9E9E;")
        model_note.setWordWrap(True)
        page_layout.addWidget(model_note)
        page_layout.addWidget(self._create_vision_agent_loop_group())
        page_layout.addWidget(self._create_compact_mode_section())
        page_layout.addWidget(self._create_apps_section())
        page_layout.addStretch(1)
        return page

    def _create_vision_agent_loop_group(self):
        """Creates the Vision settings group (agent loop tunables)."""
        group = QGroupBox("Vision (agent loop)")
        vision_layout = QGridLayout(group)
        vision_layout.setColumnStretch(2, 1)

        self.allow_shell_commands_checkbox = QCheckBox(
            "Allow terminal commands during Vision tasks")
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
        """Creates the Context page: history trimming, rolling summary and compaction."""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(14)

        history_group = QGroupBox("History trimming")
        context_layout = QGridLayout(history_group)
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

        page_layout.addWidget(history_group)

        summary_group = QGroupBox("Rolling summary")
        summary_layout = QGridLayout(summary_group)
        summary_layout.setColumnStretch(2, 1)

        self.handoff_enabled_checkbox = QCheckBox()
        handoff_tooltip = (
            "Keep a rolling summary next to each session journal so a new session, "
            "model or application run can continue with compact context instead "
            "of the full journal. Summaries never mix between sessions."
        )
        summary_layout.addWidget(self._create_context_setting_label(
            "Rolling handoff summary:", "5-rolling-handoff-summary"), 0, 0)
        summary_layout.addWidget(self._create_help_label(handoff_tooltip), 0, 1, Qt.AlignLeft)
        summary_layout.addWidget(self.handoff_enabled_checkbox, 0, 3)

        self.handoff_model_switch_checkbox = QCheckBox()
        model_tooltip = "Refresh and inject the summary when switching the active model."
        summary_layout.addWidget(self._create_context_setting_label(
            "On model switch:", "7-on-model-switch"), 1, 0)
        summary_layout.addWidget(self._create_help_label(model_tooltip), 1, 1, Qt.AlignLeft)
        summary_layout.addWidget(self.handoff_model_switch_checkbox, 1, 3)

        self.summary_max_tokens_spinbox = QSpinBox()
        self.summary_max_tokens_spinbox.setRange(200, 8000)
        self.summary_max_tokens_spinbox.setSingleStep(100)
        self.summary_max_tokens_spinbox.setFixedWidth(80)
        max_tokens_tooltip = "Token budget requested from the summarizer model."
        summary_layout.addWidget(self._create_context_setting_label(
            "Summary max tokens:", "8-summary-max-tokens"), 2, 0)
        summary_layout.addWidget(self._create_help_label(max_tokens_tooltip), 2, 1, Qt.AlignLeft)
        summary_layout.addWidget(self.summary_max_tokens_spinbox, 2, 3)

        self.summary_threshold_spinbox = QDoubleSpinBox()
        self.summary_threshold_spinbox.setRange(0.1, 1.0)
        self.summary_threshold_spinbox.setSingleStep(0.05)
        self.summary_threshold_spinbox.setDecimals(2)
        self.summary_threshold_spinbox.setFixedWidth(80)
        threshold_tooltip = (
            "Refresh the summary when a completion not yet covered by the "
            "summary reaches this fraction of the model window. A near-full "
            "safety net: routine refreshes come from 'Summary refresh at'."
        )
        summary_layout.addWidget(self._create_context_setting_label(
            "Refresh threshold:", "9-refresh-threshold"), 3, 0)
        summary_layout.addWidget(self._create_help_label(threshold_tooltip), 3, 1, Qt.AlignLeft)
        summary_layout.addWidget(self.summary_threshold_spinbox, 3, 3)

        self.summary_model_lineedit = QLineEdit()
        self.summary_model_lineedit.setPlaceholderText(
            "provider:model (empty = current session model)")
        summary_model_tooltip = (
            "Optional cheaper model for handoff summaries. Use "
            "'provider:model' (for example 'openai:gpt-5-mini'), or a bare "
            "model id to keep the active provider. Empty uses the session model."
        )
        summary_layout.addWidget(self._create_context_setting_label(
            "Summarizer model:", "10-summarizer-model"), 4, 0)
        summary_layout.addWidget(self._create_help_label(summary_model_tooltip), 4, 1, Qt.AlignLeft)
        summary_layout.addWidget(self.summary_model_lineedit, 4, 2, 1, 2)

        self.summary_refresh_tokens_spinbox = QSpinBox()
        self.summary_refresh_tokens_spinbox.setRange(0, 1_000_000)
        self.summary_refresh_tokens_spinbox.setSingleStep(5000)
        self.summary_refresh_tokens_spinbox.setFixedWidth(100)
        refresh_tokens_tooltip = (
            "Refresh the rolling summary once this many tokens of new journal "
            "events are not covered yet. Works independently of the model "
            "window, so long sessions keep a current summary on 1M+ windows. "
            "0 = only the refresh threshold above."
        )
        summary_layout.addWidget(self._create_context_setting_label(
            "Summary refresh at:", "12-summary-refresh-at"), 5, 0)
        summary_layout.addWidget(self._create_help_label(refresh_tokens_tooltip), 5, 1, Qt.AlignLeft)
        summary_layout.addWidget(self.summary_refresh_tokens_spinbox, 5, 3)

        page_layout.addWidget(summary_group)

        compact_group = QGroupBox("Automatic compaction")
        compact_layout = QGridLayout(compact_group)
        compact_layout.setColumnStretch(2, 1)

        self.auto_compact_checkbox = QCheckBox()
        auto_compact_tooltip = (
            "When the context reaches the token cap below, or free space in "
            "the model window drops to the response buffer (~15% of the "
            "window, at least 16k tokens), older events are compacted into the "
            "session summary automatically once the current step finishes. "
            "Chat, Terminal and Browser only."
        )
        compact_layout.addWidget(self._create_context_setting_label(
            "Auto-compact on low headroom:", "11-auto-compact-on-low-headroom"), 0, 0)
        compact_layout.addWidget(self._create_help_label(auto_compact_tooltip), 0, 1, Qt.AlignLeft)
        compact_layout.addWidget(self.auto_compact_checkbox, 0, 3)

        self.auto_compact_tokens_spinbox = QSpinBox()
        self.auto_compact_tokens_spinbox.setRange(0, 1_000_000)
        self.auto_compact_tokens_spinbox.setSingleStep(10000)
        self.auto_compact_tokens_spinbox.setFixedWidth(100)
        cap_tooltip = (
            "Compact older events into the summary once the context reaches "
            "this many tokens, even when the model window is far from full. "
            "Keeps requests smaller and cheaper. 0 = only the free-space rule."
        )
        compact_layout.addWidget(self._create_context_setting_label(
            "Auto-compact at:", "13-auto-compact-at"), 1, 0)
        compact_layout.addWidget(self._create_help_label(cap_tooltip), 1, 1, Qt.AlignLeft)
        compact_layout.addWidget(self.auto_compact_tokens_spinbox, 1, 3)

        page_layout.addWidget(compact_group)

        page_layout.addStretch(1)
        return page

    def _create_about_link(self, text, url):
        """Creates a label that opens an external URL in the default browser."""
        label = QLabel(f'<a href="{url}">{text}</a>')
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(True)
        label.setToolTip(f"Open {url}")
        return label

    def _create_about_section(self):
        """About page kept at its natural height, pinned in the navigation."""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(12)

        group = QGroupBox("About")
        layout = QVBoxLayout(group)
        layout.setSpacing(14)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)
        logo_label = QLabel()
        logo_label.setPixmap(QIcon(resource_path("assets", "ico.ico")).pixmap(QSize(64, 64)))
        header_layout.addWidget(logo_label, 0, Qt.AlignTop)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        name_label = QLabel("Logos")
        name_font = name_label.font()
        name_font.setPointSize(name_font.pointSize() + 8)
        name_font.setBold(True)
        name_label.setFont(name_font)
        version_label = QLabel(f"Version {APP_VERSION}")
        # A source run also knows its commit; an installed build shows the
        # version alone, because there is no repository to ask.
        commit = get_commit()
        if commit:
            version_label.setText(f"Version {APP_VERSION} · build {commit}")
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

        page_layout.addWidget(group)
        page_layout.addStretch(1)
        return page

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

        from src.browser.policy import NETWORK_ACCESS_LABELS, NETWORK_ACCESS_MODES
        self.browser_network_combo = QComboBox()
        self.browser_network_combo.setFixedWidth(230)
        for mode in NETWORK_ACCESS_MODES:
            self.browser_network_combo.addItem(NETWORK_ACCESS_LABELS[mode], mode)
        network_tooltip = (
            "Any address (default): this is a normal browser, so localhost, "
            "local network addresses and file:// pages open like any other "
            "site.\n"
            "Internet and this computer: refuses private LAN addresses "
            "(192.168.x.x, 10.x.x.x, name.local) but still allows localhost.\n"
            "Internet only: refuses localhost and LAN addresses.\n"
            "The stricter modes only limit which addresses the agent may open "
            "on its own; they exist for pages you do not trust, because a page "
            "can try to talk the agent into visiting a router or a local file."
        )
        browser_layout.addWidget(QLabel("Network access:"), 1, 0)
        browser_layout.addWidget(self._create_help_label(network_tooltip), 1, 1, Qt.AlignLeft)
        browser_layout.addWidget(self.browser_network_combo, 1, 3)

        self.browser_headless_checkbox = QCheckBox()
        headless_tooltip = "Run the controlled browser without a visible window. Applies on next browser start."
        browser_layout.addWidget(QLabel("Headless:"), 2, 0)
        browser_layout.addWidget(self._create_help_label(headless_tooltip), 2, 1, Qt.AlignLeft)
        browser_layout.addWidget(self.browser_headless_checkbox, 2, 3)

        self.browser_timeout_spinbox = QSpinBox()
        self.browser_timeout_spinbox.setRange(5000, 120000)
        self.browser_timeout_spinbox.setSingleStep(5000)
        self.browser_timeout_spinbox.setSuffix(" ms")
        self.browser_timeout_spinbox.setFixedWidth(100)
        timeout_tooltip = "Action timeout for browser tools. Applies on next browser start."
        browser_layout.addWidget(QLabel("Action timeout:"), 3, 0)
        browser_layout.addWidget(self._create_help_label(timeout_tooltip), 3, 1, Qt.AlignLeft)
        browser_layout.addWidget(self.browser_timeout_spinbox, 3, 3)

        from src.browser import runtime as browser_runtime
        engine_tooltip = (
            "Chromium is the real browser the Browser panel drives. It is not "
            "packaged with Logos because of its size, so it is downloaded once "
            f"({browser_runtime.DOWNLOAD_SIZE}) into a per-user folder and reused "
            "by every later browser session. Downloading needs an internet "
            "connection; the Browser panel offers the same download on first use."
        )
        self.browser_engine_label = QLabel()
        self.browser_engine_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.browser_engine_button = QPushButton("Download…")
        self.browser_engine_button.setFixedWidth(140)
        self.browser_engine_button.clicked.connect(self._download_browser_engine)
        engine_row = QHBoxLayout()
        engine_row.setContentsMargins(0, 0, 0, 0)
        engine_row.addWidget(self.browser_engine_label)
        engine_row.addStretch(1)
        engine_row.addWidget(self.browser_engine_button)
        browser_layout.addWidget(QLabel("Browser engine:"), 4, 0)
        browser_layout.addWidget(self._create_help_label(engine_tooltip), 4, 1, Qt.AlignLeft)
        browser_layout.addLayout(engine_row, 4, 2, 1, 2)
        self._refresh_browser_engine_row()

        return self._page(group)

    def _refresh_browser_engine_row(self):
        """Shows whether the Chromium build the Browser panel needs is installed."""
        from src.browser import runtime as browser_runtime
        revision = browser_runtime.chromium_revision() or "?"
        if browser_runtime.is_chromium_installed():
            self.browser_engine_label.setText(f"Chromium {revision} · installed")
            self.browser_engine_label.setToolTip(str(browser_runtime.chromium_executable() or ""))
            self.browser_engine_button.setText("Reinstall…")
        else:
            self.browser_engine_label.setText(f"Chromium {revision} · not installed")
            self.browser_engine_label.setToolTip(str(browser_runtime.browsers_dir()))
            self.browser_engine_button.setText("Download…")

    def _download_browser_engine(self):
        """Runs the one-time Chromium download from Settings → Browser."""
        from ui.browser_engine_dialog import BrowserEngineDialog
        dialog = BrowserEngineDialog(self.window())
        dialog.exec()
        self._refresh_browser_engine_row()

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

        from src.computer.terminal_policy import ACCESS_MODE_LABELS
        self.terminal_access_combo = QComboBox()
        self.terminal_access_combo.setFixedWidth(170)
        for mode in ("ask", "auto", "full"):
            self.terminal_access_combo.addItem(ACCESS_MODE_LABELS[mode], mode)
        access_tooltip = (
            "Ask every time (default): read-only shell commands and edits "
            "inside the project run freely; commands that change state, "
            "network access, sensitive files (.env, keys, .ssh) and paths "
            "outside the project ask first.\n"
            "Auto approve: only risky commands (destructive, privileged, "
            "system-level), sensitive files and paths outside the project ask.\n"
            "Full access: never ask.\n"
            "Applies to the next action; no restart."
        )
        terminal_layout.addWidget(QLabel("Access permissions:"), 0, 0)
        terminal_layout.addWidget(self._create_help_label(access_tooltip), 0, 1, Qt.AlignLeft)
        terminal_layout.addWidget(self.terminal_access_combo, 0, 3)

        terminal_layout.addWidget(QLabel("Shell timeout:"), 1, 0)
        terminal_layout.addWidget(self._create_help_label(shell_timeout_tooltip), 1, 1, Qt.AlignLeft)
        terminal_layout.addWidget(self.shell_timeout_spinbox, 1, 3)

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
        terminal_layout.addWidget(QLabel("Detach to background:"), 2, 0)
        terminal_layout.addWidget(self._create_help_label(shell_detach_tooltip), 2, 1, Qt.AlignLeft)
        terminal_layout.addWidget(self.shell_detach_spinbox, 2, 3)

        return self._page(group)

    def _create_mcp_section(self):
        """Creates the MCP Servers settings section."""
        self.mcp_page = MCPSettingsPage(self.settings_manager, self.controller)
        return self.mcp_page

    def _create_skills_section(self):
        """Creates the Agent Skills settings section."""
        group = QGroupBox("Skills")
        layout = QVBoxLayout(group)

        intro = QLabel(
            "Skills are folders with a SKILL.md file that teach the Terminal and "
            "Browser agents a specific workflow. Logos ships a built-in set that "
            "works out of the box; a project or user skill with the same name "
            "overrides it. Folder layouts used by Claude Code, Codex, Cursor and "
            "Qoder are read as-is; the project scope wins over the user scope.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.skills_list = QListWidget()
        self.skills_list.setMinimumHeight(170)
        self.skills_list.setTextElideMode(Qt.ElideRight)
        self.skills_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.skills_list.setToolTip(
            "Skills found in the project, user and built-in folders. The agent "
            "sees each name and description and loads the full instructions on "
            "demand.")
        layout.addWidget(self.skills_list)

        catalogue_row = QHBoxLayout()
        self.skills_catalogue_button = QPushButton("Browse Catalogue...")
        self.skills_catalogue_button.setToolTip(
            "Search ClawHub and install community skills into "
            "~/.logos/skills.")
        self.skills_catalogue_button.clicked.connect(
            self._open_skill_catalogue)
        catalogue_row.addWidget(self.skills_catalogue_button)
        catalogue_row.addStretch()
        layout.addLayout(catalogue_row)

        buttons = QHBoxLayout()
        self.skills_project_button = QPushButton("Project Folder")
        self.skills_project_button.setToolTip(
            "Creates and opens <project>/.logos/skills in the file manager.")
        self.skills_project_button.clicked.connect(
            lambda: self._open_skills_dir("project"))
        buttons.addWidget(self.skills_project_button)

        self.skills_user_button = QPushButton("User Folder")
        self.skills_user_button.setToolTip(
            "Creates and opens ~/.logos/skills in the file manager.")
        self.skills_user_button.clicked.connect(
            lambda: self._open_skills_dir("user"))
        buttons.addWidget(self.skills_user_button)

        self.skills_reload_button = QPushButton("Reload")
        self.skills_reload_button.setToolTip("Rescan the skill folders.")
        self.skills_reload_button.clicked.connect(self._refresh_skills)
        buttons.addWidget(self.skills_reload_button)
        layout.addLayout(buttons)

        hint = QLabel(
            "A skill needs a SKILL.md with `name` and `description` in its YAML "
            "frontmatter. Extra files (scripts, references, assets) stay in the "
            "skill folder and are read on demand.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9E9E9E;")
        layout.addWidget(hint)

        self._refresh_skills()
        return group

    def _skills_project_root(self) -> str:
        return str(getattr(self.controller, "project_root", "") or "")

    def _open_skill_catalogue(self):
        """Opens the ClawHub browser; refreshes the list after an install."""
        dialog = SkillCatalogDialog(self)
        dialog.installed.connect(self._refresh_skills)
        dialog.exec()

    def _refresh_skills(self):
        """Re-reads installed skills from the project, user and built-in folders."""
        if not hasattr(self, "skills_list"):
            return
        project_root = self._skills_project_root()
        self.skills_project_button.setEnabled(bool(project_root))
        self.skills_list.clear()
        try:
            items = skills_store.discover(project_root)
        except Exception as e:  # noqa: BLE001 - settings must still open
            logging.warning(f"Could not list skills: {e}")
            items = []
        if not items:
            placeholder = QListWidgetItem("No skills installed.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.skills_list.addItem(placeholder)
            return
        for skill in items:
            entry = QListWidgetItem(
                f"{skill['name']} — {skill['description']}")
            entry.setToolTip(
                f"{skill['description']}\n\n{skill['path']}\n"
                f"Scope: {skill['scope']} · Source: {skill['source']}")
            entry.setData(Qt.UserRole, skill["path"])
            self.skills_list.addItem(entry)

    def _open_skills_dir(self, scope: str):
        """Creates and opens a skills root for the project or the user."""
        if scope == "project":
            project_root = self._skills_project_root()
            if not project_root:
                QMessageBox.information(
                    self, "No active project",
                    "Open a project first, then its skills folder becomes available.")
                return
            path = os.path.join(project_root, ".logos", "skills")
        else:
            path = os.path.join(os.path.expanduser("~"), ".logos", "skills")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(
                self, "Could not create the folder", f"{path}\n\n{e}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

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


