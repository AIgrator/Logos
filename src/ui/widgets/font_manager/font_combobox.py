"""
Custom Font ComboBox Widget for PySide6.

This module provides a QComboBox subclass that displays system fonts
with a preview of the font itself. It uses matplotlib.font_manager
to reliably find system fonts and their file paths.
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QComboBox, QStyle, QStyledItemDelegate

# Attempt to import matplotlib for font finding
try:
    import matplotlib.font_manager as fm
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# --- Font Caching ---
_font_cache = None

def get_system_fonts():
    """
    Scans system fonts using matplotlib.font_manager and caches the result.
    Returns a dictionary mapping font names to their file paths.
    """
    global _font_cache
    if _font_cache is not None:
        return _font_cache

    if not MATPLOTLIB_AVAILABLE:
        _font_cache = {}
        return _font_cache

    font_map = {}
    try:
        font_paths = fm.findSystemFonts(fontpaths=None, fontext='ttf')
        for font_path in sorted(font_paths):
            try:
                # Add font to the Qt font database to get its family name
                font_id = QFontDatabase.addApplicationFont(font_path)
                if font_id != -1:
                    family = QFontDatabase.applicationFontFamilies(font_id)[0]
                    # Prefer user-friendly names over file names
                    if family not in font_map: # Avoid overwriting with bold/italic variants
                         font_map[family] = font_path
            except Exception:
                continue # Ignore corrupted or problematic fonts
    except Exception as e:
        print(f"Could not get system fonts using matplotlib: {e}")

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
            if font_path == "cv2_default":
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
        self.addItem("Default (Fast, Latin only)", "cv2_default")

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
        if font_path_to_select == "cv2_default":
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
