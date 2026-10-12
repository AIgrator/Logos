from src.computer.tools.browser.actions import (
    browser_click,
    browser_close,
    browser_fill,
    browser_find,
    browser_goto,
    browser_handle_dialog,
    browser_hover,
    browser_navigate_back,
    browser_press,
    browser_read,
    browser_screenshot,
    browser_select_option,
    browser_snapshot,
    browser_tabs,
    browser_wait_for,
)
from src.computer.tools.common.communicate_to_user import communicate_to_user
from src.computer.tools.common.finish import finish

TOOLS_METADATA = {
    "browser_goto": browser_goto,
    "browser_snapshot": browser_snapshot,
    "browser_click": browser_click,
    "browser_fill": browser_fill,
    "browser_select_option": browser_select_option,
    "browser_hover": browser_hover,
    "browser_press": browser_press,
    "browser_read": browser_read,
    "browser_screenshot": browser_screenshot,
    "browser_find": browser_find,
    "browser_wait_for": browser_wait_for,
    "browser_navigate_back": browser_navigate_back,
    "browser_tabs": browser_tabs,
    "browser_handle_dialog": browser_handle_dialog,
    "browser_close": browser_close,
    "communicate_to_user": communicate_to_user,
    "finish": finish,
}

BROWSER_TOOL_NAMES = frozenset(TOOLS_METADATA.keys())


def get_tools_metadata():
    return TOOLS_METADATA
