"""
This module contains the OCRLanguageWidget, a self-contained component
for selecting EasyOCR languages with complex dependency management.
"""
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QAbstractItemView
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

from settings_manager import SettingsManager


class OCRLanguageWidget(QWidget):
    """A widget for selecting OCR languages with built-in validation logic."""

    def __init__(self, settings_manager: SettingsManager, theme_manager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.theme_manager = theme_manager

        # --- Language Group Logic ---
        self._is_programmatically_changing_lang = False

        # Create mappings from language code to its group key and type.
        self.lang_code_to_group_key = {}
        self.lang_code_to_group_type = {}
        for group_key, group_data in self.settings_manager.EASYOCR_LANGUAGE_GROUPS.items():
            group_type = group_data['type']
            for code in group_data['languages'].keys():
                self.lang_code_to_group_key[code] = group_key
                self.lang_code_to_group_type[code] = group_type

        self.cyrillic_codes = set(
            self.settings_manager.EASYOCR_LANGUAGE_GROUPS['cyrillic']['languages'].keys()
        )
        # --- End Language Group Logic ---

        self._setup_ui()
        self.theme_manager.theme_changed.connect(self._update_theme)

    def _update_theme(self):
        """Updates the widget's styles when the application theme changes."""
        self.language_list_widget.setStyleSheet(self.theme_manager.get_style("LANGUAGE_LIST_STYLE"))
        # Force a redraw of item backgrounds to apply new disabled colors
        self._update_language_selection_logic()

    def _setup_ui(self):
        """Initializes the user interface for the widget."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.language_list_widget = QListWidget()
        self.language_list_widget.setStyleSheet(self.theme_manager.get_style("LANGUAGE_LIST_STYLE")) # Apply the fix
        self.language_list_widget.setSelectionMode(QAbstractItemView.MultiSelection)
        self.language_list_widget.setToolTip("Select one or more languages for text recognition (OCR).")
        self.language_list_widget.setFixedHeight(100)

        for lang_name, lang_code in sorted(self.settings_manager.EASYOCR_LANGUAGES.items()):
            item = QListWidgetItem(lang_name)
            item.setData(Qt.UserRole, lang_code)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.language_list_widget.addItem(item)

        self.language_list_widget.itemChanged.connect(self._update_language_selection_logic)
        layout.addWidget(self.language_list_widget)

        self.language_warning_label = QLabel(
            "Warning: Selecting languages from different script groups (e.g., Latin, Japanese)\n"
            "will download multiple models and increase memory usage."
        )
        self.language_warning_label.setStyleSheet("font-size: 9pt; color: #FFA000;")
        self.language_warning_label.setVisible(False)
        layout.addWidget(self.language_warning_label)

    def load_languages(self, selected_codes: list):
        """
        Loads the given list of language codes into the widget, updating checkboxes.
        """
        self._is_programmatically_changing_lang = True
        for i in range(self.language_list_widget.count()):
            item = self.language_list_widget.item(i)
            lang_code = item.data(Qt.UserRole)
            item.setCheckState(Qt.Checked if lang_code in selected_codes else Qt.Unchecked)
        self._is_programmatically_changing_lang = False

        # Trigger the logic update after loading the initial state
        self._update_language_selection_logic()

    def get_selected_languages(self) -> list:
        """
        Returns a list of selected language codes, ensuring consistency
        (e.g., adding 'en' if a Cyrillic language is selected).
        """
        selected_lang_codes = [
            self.language_list_widget.item(i).data(Qt.UserRole)
            for i in range(self.language_list_widget.count())
            if self.language_list_widget.item(i).checkState() == Qt.Checked
        ]

        # Defensively ensure English is included if any Cyrillic language is selected.
        has_cyrillic = any(lang in self.cyrillic_codes for lang in selected_lang_codes)
        if has_cyrillic and 'en' not in selected_lang_codes:
            selected_lang_codes.append('en')

        return selected_lang_codes

    def _update_language_selection_logic(self, changed_item=None):
        """
        Enforces language selection rules by actively managing checkbox states.
        When a dominant language group is selected, incompatible languages are
        automatically deselected.
        """
        if self._is_programmatically_changing_lang:
            return
        self._is_programmatically_changing_lang = True

        selected_codes = {self.language_list_widget.item(i).data(Qt.UserRole)
                          for i in range(self.language_list_widget.count())
                          if self.language_list_widget.item(i).checkState() == Qt.Checked}

        # 1. Determine the dominant group based on current selection
        dominant_type = None
        dominant_group_key = None
        if any(self.lang_code_to_group_type.get(c) == 'strict' for c in selected_codes):
            dominant_type = 'strict'
            for code in selected_codes:
                if self.lang_code_to_group_type.get(code) == 'strict':
                    dominant_group_key = self.lang_code_to_group_key.get(code)
                    break
        elif any(self.lang_code_to_group_type.get(c) == 'cyrillic' for c in selected_codes):
            dominant_type = 'cyrillic'

        # 2. Correct the selection: uncheck any incompatible languages
        if dominant_type:
            codes_to_uncheck = set()
            for code in selected_codes:
                is_english = code == 'en'
                is_in_dominant_group = False
                if dominant_type == 'cyrillic':
                    is_in_dominant_group = self.lang_code_to_group_type.get(code) == 'cyrillic'
                elif dominant_type == 'strict':
                    is_in_dominant_group = self.lang_code_to_group_key.get(code) == dominant_group_key

                if not is_in_dominant_group and not is_english:
                    codes_to_uncheck.add(code)

            if codes_to_uncheck:
                for i in range(self.language_list_widget.count()):
                    item = self.language_list_widget.item(i)
                    if item.data(Qt.UserRole) in codes_to_uncheck:
                        item.setCheckState(Qt.Unchecked)
                selected_codes -= codes_to_uncheck

        # 3. Auto-select and lock English for Cyrillic if necessary
        is_cyrillic_selected = any(self.lang_code_to_group_type.get(c) == 'cyrillic' for c in selected_codes)
        english_item = next((self.language_list_widget.item(i) for i in range(self.language_list_widget.count())
                             if self.language_list_widget.item(i).data(Qt.UserRole) == 'en'), None)
        if english_item and is_cyrillic_selected and english_item.checkState() == Qt.Unchecked:
            english_item.setCheckState(Qt.Checked)
            selected_codes.add('en')

        # 4. Update the enabled/disabled state of all items
        default_bg = self.language_list_widget.palette().color(QPalette.Base)
        disabled_bg = QColor(self.theme_manager.get_style("LIST_ITEM_DISABLED_BG"))
        for i in range(self.language_list_widget.count()):
            item = self.language_list_widget.item(i)
            item_code = item.data(Qt.UserRole)
            item_type = self.lang_code_to_group_type.get(item_code)
            item_group_key = self.lang_code_to_group_key.get(item_code)

            is_selectable = False
            if not dominant_type:
                is_selectable = True  # Latin mode, all are selectable
            elif dominant_type == 'cyrillic':
                is_selectable = (item_type == 'cyrillic') or (item_code == 'en')
            elif dominant_type == 'strict':
                is_selectable = (item_group_key == dominant_group_key) or (item_code == 'en')

            is_english_locked = item_code == 'en' and is_cyrillic_selected

            if is_selectable and not is_english_locked:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setBackground(default_bg)
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                item.setBackground(disabled_bg)

        # 5. Update performance warning
        final_selected_groups = {self.lang_code_to_group_key.get(code) for code in selected_codes if code != 'en'}
        self.language_warning_label.setVisible(len(final_selected_groups) > 1)

        self._is_programmatically_changing_lang = False
