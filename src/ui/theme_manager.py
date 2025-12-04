# src/ui/theme_manager.py
from PySide6.QtCore import QObject, QRect, Signal, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleOption

LIGHT_THEME = "light"
DARK_THEME = "dark"

# --- Палитры ---
def create_dark_palette():
    """Creates a standard dark palette."""
    dark_palette = QPalette()
    disabled_text = QColor(128, 128, 128)
    disabled_bg = QColor("#424242")

    dark_palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    dark_palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    dark_palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    dark_palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
    dark_palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
    dark_palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    dark_palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    dark_palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
    dark_palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    dark_palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)

    # Set colors for disabled state
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, disabled_bg)

    return dark_palette

def create_light_palette():
    """Creates a standard light palette."""
    light_palette = QPalette()
    light_palette.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
    light_palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.black)
    light_palette.setColor(QPalette.ColorRole.Base, Qt.GlobalColor.white)
    light_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(245, 245, 245))
    light_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 220))
    light_palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.black)
    light_palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.black)
    light_palette.setColor(QPalette.ColorRole.Button, QColor(240, 240, 240))
    light_palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.black)
    light_palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    light_palette.setColor(QPalette.ColorRole.Link, QColor(0, 0, 255))
    light_palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    light_palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)

    # Set colors for disabled state
    disabled_text = QColor(128, 128, 128)
    disabled_button = QColor(210, 210, 210)
    light_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    light_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    light_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    light_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, disabled_button)

    return light_palette

# --- Прокси-стиль для темной темы ---
class DarkThemeProxyStyle(QProxyStyle):
    """
    A proxy style to apply specific visual fixes for a dark theme,
    such as drawing borders around checkboxes, without replacing the
    entire native drawing routine.
    """
    def __init__(self, style):
        super().__init__(style)
        # Use a slightly lighter color for borders for better visibility
        self.border_pen = QPen(QColor("#f0f0f0"))
        self.border_pen_disabled = QPen(QColor("#808080"))

    def drawPrimitive(self, element: QStyle.PrimitiveElement, option: QStyleOption,
                      painter: QPainter, widget=None):
        """
        Override to draw a border around checkbox indicators after the
        base style has drawn the element.
        """
        # Let the base style draw the element first (e.g., the checkmark)
        super().drawPrimitive(element, option, painter, widget)

        # After drawing, add our custom border if it's a checkbox indicator
        if element == QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            painter.save()

            # Choose pen color based on whether the widget is enabled
            if option.state & QStyle.StateFlag.State_Enabled:
                painter.setPen(self.border_pen)
            else:
                painter.setPen(self.border_pen_disabled)

            # Draw a rectangle around the indicator
            # We adjust the rect slightly to ensure the border is pixel-perfect
            rect = QRect(option.rect.x(), option.rect.y(), option.rect.width() - 1, option.rect.height() - 1)
            painter.drawRect(rect)

            painter.restore()

# --- Наборы QSS-стилей для каждой темы ---
STYLES = {
    LIGHT_THEME: {
        "TOOLTIP_STYLE": "QLabel { background-color: #ffffe0; color: black; "
                         "border: 1px solid #b0b0b0; border-radius: 3px; padding: 5px; }",
        "TITLE_BAR_BUTTON_STYLE": """
            QPushButton { background-color: transparent; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #d0d0d0; }
        """,
        "REMOVE_BUTTON_STYLE": """
            QPushButton { background-color: transparent; border: none; }
            QPushButton:hover { background-color: #cccccc; border-radius: 10px; }
        """,
        "POSITION_BUTTON_STYLE": """
            QPushButton { background-color: #E0E0E0; border: 1px solid #BDBDBD; border-radius: 4px; }
            QPushButton:hover { background-color: #CACACA; }
            QPushButton:checked { background-color: #007ACC; border: 1px solid #005A9E; }
        """,
        "PROGRESS_BAR_STYLE": """
            QProgressBar {
                border: 1px solid #BDBDBD;
                border-radius: 8px;
                background-color: #F5F5F5;
                height: 18px;
                text-align: center;
                color: #333;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1: 0, y1: 0, x2: 1, y1: 0, stop: 0 #4dabf7, stop: 1 #1e88e5);
                border-radius: 8px;
            }
        """,
        "LANGUAGE_LIST_STYLE": """
            QListWidget { background-color: #FFFFFF; }
            QListWidget::item:disabled { background-color: #E0E0E0; color: #A0A0A0; }
        """,
        "LIST_ITEM_DISABLED_BG": "#E0E0E0",
        "ERROR_MESSAGE_STYLE": "color: #D32F2F; background: transparent; font-weight: bold;",
        "ICON_COLOR": "black",
        "ICON_COLOR_DISABLED": "#a0a0a0",
        "ICON_COLOR_HOVER": "gray",
        "NAV_ICON_COLOR_CHECKED": "white",
        "DOC_LINK_STYLE": "QLabel { color: #0000FF; }",
        # Terminal styles
        "TERMINAL_CONTAINER_BG": "#f6f8fa",
        "TERMINAL_CONTAINER_BORDER": "#d18616",
        "TERMINAL_HEADER_COLOR": "#24292e",
        "TERMINAL_SUBHEADER_COLOR": "#586069",
        "TERMINAL_DIVIDER_COLOR": "#e1e4e8",
        "TERMINAL_PRE_BG": "#ffffff",
        "TERMINAL_PRE_COLOR": "#24292e",
        "TERMINAL_LINE_NUM_COLOR": "#6a737d",
        "TERMINAL_DIFF_ADD_BG": "#e6ffed",
        "TERMINAL_DIFF_REMOVE_BG": "#ffeef0",
        "TERMINAL_WRITE_HEADER_COLOR": "#0366d6",
        "TERMINAL_READ_FILE_BORDER": "#0366d6",
        "TERMINAL_READ_FILE_ICON": "#0366d6",
    },
    DARK_THEME: {
        "TOOLTIP_STYLE": "QLabel { background-color: #282c34; color: white; "
                         "padding: 5px; border: 1px solid #1c1e22; border-radius: 3px; }",
        "TITLE_BAR_BUTTON_STYLE": """
            QPushButton { background-color: transparent; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #555555; }
        """,
        "REMOVE_BUTTON_STYLE": """
            QPushButton { background-color: transparent; border: none; }
            QPushButton:hover { background-color: #dddddd; border-radius: 10px; }
        """,
        "POSITION_BUTTON_STYLE": """
            QPushButton { background-color: #3E3E3E; border: 1px solid #555555; border-radius: 4px; }
            QPushButton:hover { background-color: #4A4A4A; }
            QPushButton:checked { background-color: #007ACC; border: 1px solid #005A9E; }
        """,
        "PROGRESS_BAR_STYLE": """
            QProgressBar {
                border: 1px solid #555;
                border-radius: 8px;
                background-color: #e0e0e0;
                height: 18px;
                text-align: center;
                color: #333;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1: 0, y1: 0, x2: 1, y1: 0, stop: 0 #4dabf7, stop: 1 #1e88e5);
                border-radius: 8px;
            }
        """,
        "LANGUAGE_LIST_STYLE": """
            QListWidget { background-color: #2b2b2b; }
            QListWidget::item:disabled { background-color: #424242; color: #888888; }
        """,
        "LIST_ITEM_DISABLED_BG": "#424242",
        "ERROR_MESSAGE_STYLE": "color: #ff3333; background: transparent; font-weight: bold;",
        "ICON_COLOR": "white",
        "ICON_COLOR_DISABLED": "#808080",
        "ICON_COLOR_HOVER": "#cccccc",
        "NAV_ICON_COLOR_CHECKED": "black",
        "DOC_LINK_STYLE": "QLabel { color: #2a82da; }",
        # Terminal styles
        "TERMINAL_CONTAINER_BG": "#2a2a2a",
        "TERMINAL_CONTAINER_BORDER": "#ffc107",
        "TERMINAL_HEADER_COLOR": "#e0e0e0",
        "TERMINAL_SUBHEADER_COLOR": "#cccccc",
        "TERMINAL_DIVIDER_COLOR": "#444",
        "TERMINAL_PRE_BG": "#1e1e1e",
        "TERMINAL_PRE_COLOR": "#d4d4d4",
        "TERMINAL_LINE_NUM_COLOR": "#888",
        "TERMINAL_DIFF_ADD_BG": "#2b4d2b",
        "TERMINAL_DIFF_REMOVE_BG": "#4d2b2b",
        "TERMINAL_WRITE_HEADER_COLOR": "#a0e0ff",
        "TERMINAL_READ_FILE_BORDER": "#007bff",
        "TERMINAL_READ_FILE_ICON": "#007bff",
    }
}

class ThemeManager(QObject):
    theme_changed = Signal()

    def __init__(self, settings_manager):
        super().__init__()
        self.settings_manager = settings_manager
        self.current_theme = self.settings_manager.get("theme", DARK_THEME)
        self.base_style_name = "Fusion" # Базовый стиль

    def apply_theme(self, theme_name):
        if theme_name not in [LIGHT_THEME, DARK_THEME]:
            return

        app = QApplication.instance()
        self.current_theme = theme_name

        if theme_name == DARK_THEME:
            app.setStyle(self.base_style_name)
            app.setPalette(create_dark_palette())
            app.setStyle(DarkThemeProxyStyle(app.style()))
        else: # LIGHT_THEME
            app.setStyle(self.base_style_name)
            app.setPalette(create_light_palette())

        self.theme_changed.emit()

    def get_style(self, key):
        return STYLES[self.current_theme].get(key, "")

    def get_current_theme_name(self):
        return self.current_theme
