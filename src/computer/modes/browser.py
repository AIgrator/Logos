from functools import wraps

from src.computer.tools.browser.actions import (
    browser_click,
    browser_close,
    browser_console,
    browser_eval,
    browser_fill,
    browser_find,
    browser_frames,
    browser_goto,
    browser_handle_dialog,
    browser_hover,
    browser_navigate_back,
    browser_network,
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
from src.computer.tools.common.open_browser import open_browser
from src.computer.tools.common.open_terminal import open_terminal
from src.computer.tools.common.search_tools import search_tools
from src.computer.tools.common.update_plan import update_plan
from src.computer.tools.common.use_skill import use_skill
from src.computer.tools.terminal.fs.list_directory import list_directory
from src.computer.tools.terminal.fs.read_files import read_files
from src.computer.tools.terminal.fs.search_file_content import search_file_content


def _tolerant_fs_tool(tool):
    """Browser's flat strict schema lists every tool's argument as required.

    Drop keys that do not belong to this tool (and null optionals) before
    calling the shared filesystem implementation, which has a strict signature.
    """
    allowed = tuple(
        (getattr(tool, "__tool_meta__", {}).get("tool", {}).get("arguments") or {})
        .keys())

    @wraps(tool)
    def wrapper(self, **kwargs):
        filtered = {
            name: value for name, value in kwargs.items()
            if name in allowed and value is not None
        }
        return tool(self, **filtered)

    return wrapper


def _browser_read_files(tool):
    """read_files accepts a list; the flat browser schema also exposes `path`."""
    @wraps(tool)
    def wrapper(self, **kwargs):
        paths = kwargs.get("paths")
        if not paths and kwargs.get("path"):
            paths = [kwargs["path"]]
        if isinstance(paths, str):
            paths = [paths]
        extra = {
            name: kwargs[name] for name in ("offset", "limit")
            if kwargs.get(name) is not None
        }
        return tool(self, paths=paths, **extra)

    return wrapper


TOOLS_METADATA = {
    "browser_goto": browser_goto,
    "browser_snapshot": browser_snapshot,
    "browser_click": browser_click,
    "browser_fill": browser_fill,
    "browser_select_option": browser_select_option,
    "browser_hover": browser_hover,
    "browser_press": browser_press,
    "browser_read": browser_read,
    "browser_console": browser_console,
    "browser_network": browser_network,
    "browser_screenshot": browser_screenshot,
    "browser_find": browser_find,
    "browser_wait_for": browser_wait_for,
    "browser_navigate_back": browser_navigate_back,
    "browser_tabs": browser_tabs,
    "browser_frames": browser_frames,
    "browser_eval": browser_eval,
    "browser_handle_dialog": browser_handle_dialog,
    "browser_close": browser_close,
    "read_files": _browser_read_files(read_files),
    "list_directory": _tolerant_fs_tool(list_directory),
    "search_file_content": _tolerant_fs_tool(search_file_content),
    "communicate_to_user": communicate_to_user,
    "finish": finish,
    "open_browser": open_browser,
    "open_terminal": open_terminal,
    "search_tools": search_tools,
    "update_plan": update_plan,
    "use_skill": use_skill,
}

BROWSER_TOOL_NAMES = frozenset(TOOLS_METADATA.keys())


def get_tools_metadata():
    return TOOLS_METADATA
