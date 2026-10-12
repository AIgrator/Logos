from src.computer.tools.browser.actions import (
    browser_click,
    browser_close,
    browser_fill,
    browser_goto,
    browser_press,
    browser_read,
    browser_snapshot,
)
from src.computer.tools.common.communicate_to_user import communicate_to_user
from src.computer.tools.common.finish import finish

TOOLS_METADATA = {
    "browser_goto": browser_goto,
    "browser_snapshot": browser_snapshot,
    "browser_click": browser_click,
    "browser_fill": browser_fill,
    "browser_press": browser_press,
    "browser_read": browser_read,
    "browser_close": browser_close,
    "communicate_to_user": communicate_to_user,
    "finish": finish,
}

BROWSER_TOOL_NAMES = frozenset(TOOLS_METADATA.keys())


def get_tools_metadata():
    return TOOLS_METADATA
