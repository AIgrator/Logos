# Error Message Style
ERROR_MESSAGE_STYLE = """
    color: #ff3333;
    background: transparent;
    font-weight: bold;
"""
"""
This module contains stylesheets and SVG icon data for the application.
"""

# ------------------- Stylesheets -------------------

# Tooltip Style
TOOLTIP_STYLE = """
    QLabel {
        background-color: #282c34;
        color: white;
        padding: 5px;
        border: 1px solid #1c1e22;
        border-radius: 3px;
    }
"""

# Remove Button Style
REMOVE_BUTTON_STYLE = """
    QPushButton {
        background-color: transparent;
        border: none;
    }
    QPushButton:hover {
        background-color: #dddddd;
        border-radius: 10px;
    }
"""

# Attachment Chip Style
ATTACHMENT_CHIP_STYLE = """
    AttachmentChip {
        background-color: #3c3c3c;
        border-radius: 10px;
    }
"""
ATTACHMENT_CHIP_LABEL_STYLE = """
    QLabel {
        color: #f0f0f0;
        background: transparent;
    }
"""
ATTACHMENT_CHIP_BUTTON_STYLE = """
    QPushButton {
        background-color: transparent;
        border: none;
        border-radius: 8px;
        padding: 0px;
    }
    QPushButton:hover {
        background-color: #555555;
    }
"""


# Title Bar Button Style
TITLE_BAR_BUTTON_STYLE = """
    QPushButton {
        background-color: transparent;
        border: none;
        border-radius: 4px;
    }
    QPushButton:hover {
        background-color: #555555;
    }
"""

# Dark Theme Style
DARK_STYLE = """
    QWidget {
        background-color: #2b2b2b;
        color: #f0f0f0;
        font-family: "Segoe UI", "Roboto", "Helvetica Neue", Arial, sans-serif;
    }
    QMainWindow, QDialog {
        background-color: #2b2b2b;
    }
    QTextEdit, QLineEdit {
        background-color: #3c3c3c;
        border: 1px solid #555;
        border-radius: 4px;
        padding: 5px;
    }
    QPushButton {
        background-color: #4a4a4a;
        color: #f0f0f0;
        border: 1px solid #555;
        padding: 8px 16px;
        border-radius: 4px;
    }
    QPushButton:hover {
        background-color: #dddddd;
        color: #000000;
    }
    QPushButton:pressed {
        background-color: #3a3a3a;
    }
    QPushButton:disabled {
        background-color: #444;
        color: #888;
    }
    QScrollArea {
        border: none;
    }
    QScrollBar:vertical {
        border: none;
        background: #2b2b2b;
        width: 10px;
        margin: 0px 0px 0px 0px;
    }
    QScrollBar::handle:vertical {
        background: #5a5a5a;
        min-height: 20px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical:hover {
        background: #6a6a6a;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        border: none;
        background: none;
        height: 0;
    }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: none;
    }
    QScrollBar:horizontal {
        border: none;
        background: #2b2b2b;
        height: 10px;
        margin: 0px 0px 0px 0px;
    }
    QScrollBar::handle:horizontal {
        background: #5a5a5a;
        min-width: 20px;
        border-radius: 5px;
    }
    QScrollBar::handle:horizontal:hover {
        background: #6a6a6a;
    }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
        border: none;
        background: none;
        width: 0;
    }
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
        background: none;
    }
    QTabWidget::pane {
        border-top: 1px solid #555;
    }
    QTabBar::tab {
        background: #3c3c3c;
        padding: 8px;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        margin-right: 1px;
    }
    QTabBar::tab:selected {
        background: #2b2b2b;
        border-bottom: 2px solid #5a9bcf;
    }
    QTabBar::tab:!selected {
        color: #aaa;
    }

    QPushButton#PilotButton {
        color: #f0f0f0;
    }

    QPushButton#PilotButton:hover {
        background-color: #dddddd;
        color: #000000;
    }

    QPushButton#CancelButton:hover {
        background-color: #dddddd;
        color: #000000;
    }
    QToolTip {
        background-color: #282c34;
        color: white;
        padding: 5px;
        border: 1px solid #1c1e22;
        border-radius: 3px;
    }
"""

# Position Grid Button Style in Settings
POSITION_BUTTON_STYLE = """
    QPushButton {
        background-color: #3E3E3E;
        border: 1px solid #555555;
        border-radius: 4px;
    }
    QPushButton:hover {
        background-color: #4A4A4A;
    }
    QPushButton:checked {
        background-color: #007ACC;
        border: 1px solid #005A9E;
    }
"""

# Progress Bar Style
PROGRESS_BAR_STYLE = """
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
"""

# Language List Style for Settings
LANGUAGE_LIST_STYLE = """
QListWidget {
    background-color: #2b2b2b;
}
QListWidget::item:disabled {
    background-color: #424242;
    color: #888888;
}
"""
