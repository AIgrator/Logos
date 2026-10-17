"""
Custom Font ComboBox Widget for PySide6.

This module provides a QComboBox subclass that displays system fonts
with a preview of the font itself. It scans the OS font directories
directly (no third-party dependency) and registers each file with Qt
to resolve its family name.
"""
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QComboBox, QStyle, QStyledItemDelegate

# Sentinel stored when the user picks the built-in fallback font.
DEFAULT_FONT_SENTINEL = "default"
# Older settings may still hold the legacy cv2-era sentinel.
_LEGACY_SENTINELS = {DEFAULT_FONT_SENTINEL, "cv2_default"}

_FONT_EXTENSIONS = (".ttf", ".otf")

# --- Font Caching ---
_font_cache = None


def _font_directories() -> list:
    """Plattform-specific directories that hold installed font files."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        return [
            os.path.join(windir, "Fonts"),
            os.path.join(local, "Microsoft", "Windows", "Fonts"),
        ]
    if sys.platform == "darwin":
        return [
            "/System/Library/Fonts",
            "/Library/Fonts",
            os.path.join(home, "Library", "Fonts"),
        ]
    return [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.join(home, ".fonts"),
        os.path.join(home, ".local", "share", "fonts"),
    ]


def _iter_font_files() -> list:
    """Walks the OS font directories and yields .ttf/.otf paths."""
    found = []
    for directory in _font_directories():
        if not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            for name in files:
                if name.lower().endswith(_FONT_EXTENSIONS):
                    found.append(os.path.join(root, name))
    return sorted(found)


def get_system_fonts():
    """
    Scans system font files and caches the result.
    Returns a dictionary mapping font names to their file paths.
    """
    global _font_cache
    if _font_cache is not None:
        return _font_cache

    font_map = {}
    for font_path in _iter_font_files():
        try:
            # Add font to the Qt font database to get its family name
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id != -1:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if not families:
                    continue
                family = families[0]
                # Prefer user-friendly names over file names
                if family not in font_map:  # Avoid overwriting with bold/italic variants
                    font_map[family] = font_path
        except Exception:
            continue  # Ignore corrupted or problematic fonts

    _font_cache = dict(sorted(font_map.items()))
    return _font_cache


class FontItemDelegate(QStyledItemDelegate):
    """
    A delegate to draw each QComboBox item with its corresponding font.
    """
    def paint(self, painter, option, index):
        # Manually handle drawing to avoid double-text rendering.

        # 1. Draw background and selection state
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        else:
            painter.fillRect(option.rect, option.palette.base())

        # 2. Draw the text with the correct font
        if index.isValid():
            font_name = index.data(Qt.DisplayRole)
            font_path = index.data(Qt.UserRole) # Get the path/data

            # Set the text color based on selection
            if option.state & QStyle.State_Selected:
                painter.setPen(option.palette.highlightedText().color())
            else:
                painter.setPen(option.palette.text().color())

            # Use the default application font for the "Default" option
            if font_path in _LEGACY_SENTINELS:
                font = QFont()
            else:
                font = QFont(font_name, 12) # Use a readable size for preview

            painter.setFont(font)

            # Define the drawing rectangle with padding
            text_rect = option.rect
            text_rect.setLeft(option.rect.left() + 5)
            painter.drawText(text_rect, Qt.AlignVCenter, font_name)


class FontComboBox(QComboBox):
    """
    A custom QComboBox that displays a list of system fonts with previews.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(FontItemDelegate(self))
        self.populate_fonts()

    def populate_fonts(self):
        """
        Populates the combo box with system fonts.
        """
        self.clear()
        # Add the default option first
        self.addItem("Default (built-in)", DEFAULT_FONT_SENTINEL)

        fonts = get_system_fonts()
        for name, path in fonts.items():
            self.addItem(name, path)

    def get_selected_font_path(self):
        """
        Returns the file path of the currently selected font.
        """
        return self.currentData()

    def set_selected_font(self, font_path_to_select):
        """
        Selects a font in the combo box based on its file path.
        """
        if font_path_to_select in _LEGACY_SENTINELS:
            self.setCurrentIndex(0)
            return

        index = self.findData(font_path_to_select)
        if index != -1:
            self.setCurrentIndex(index)
        else:
            # If the font path is not found, add it as a temporary item
            # This can happen if settings point to a font that's been uninstalled
            font_name = f"Not Found: {os.path.basename(font_path_to_select)}"
            self.addItem(font_name, font_path_to_select)
            self.setCurrentIndex(self.count() - 1)
