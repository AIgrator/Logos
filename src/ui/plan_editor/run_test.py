import json
import os
import sys

from PySide6.QtWidgets import QApplication

# Add the parent directory of 'tests' (the project root) to the Python path.
# This allows Python to find the 'tests' package.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, project_root)

# Now we can import from the package directly.
from tests.plan_editor_refactoring.main_dialog import PlanEditorDialog  # noqa: E402

# A simple dark theme for better visuals during testing
DARK_STYLE = """
    QWidget {
        background-color: #2b2b2b;
        color: #f0f0f0;
        font-family: "Segoe UI";
        font-size: 10pt;
    }
    QDialog {
        background-color: #3c3c3c;
    }
    QPushButton {
        background-color: #555;
        border: 1px solid #777;
        padding: 6px 12px;
        border-radius: 4px;
    }
    QPushButton:hover {
        background-color: #666;
    }
    QPushButton:pressed {
        background-color: #777;
    }
    QGraphicsView {
        border: none;
        background-color: #2b2b2b;
    }
    QScrollBar:vertical {
        border: none;
        background: #3c3c3c;
        width: 10px;
        margin: 0px 0px 0px 0px;
    }
    QScrollBar::handle:vertical {
        background: #555;
        min-height: 20px;
        border-radius: 5px;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
    }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: none;
    }
"""

def main():
    """
    Loads the test plan data and displays the PlanEditorDialog.
    """
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)

    try:
        # Construct an absolute path to the test data file relative to this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        data_file = os.path.join(script_dir, 'test_data.json')
        with open(data_file, encoding='utf-8') as f:
            plan_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error loading test_data.json: {e}")
        # In case of error, start with an empty plan
        plan_data = []

    # Create and show the editor dialog
    # We pass None as parent for this standalone test
    editor = PlanEditorDialog(parent=None, plan=plan_data)

    # We use exec() to run it as a modal dialog
    result = editor.exec()

    if result == PlanEditorDialog.Accepted:
        print("Editor accepted. Final plan:")
        final_plan = editor.get_plan()
        print(json.dumps(final_plan, indent=2))
    else:
        print("Editor cancelled.")

    sys.exit()


if __name__ == '__main__':
    main()
