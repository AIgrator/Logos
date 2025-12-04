from src.computer.tools.vision.click import click
from src.computer.tools.vision.double_click import double_click
from src.computer.tools.vision.drag_and_drop import drag_and_drop
from src.computer.tools.vision.hotkey import hotkey
from src.computer.tools.vision.key_down import key_down
from src.computer.tools.vision.key_up import key_up
from src.computer.tools.vision.launch_app import launch_app
from src.computer.tools.vision.press_key import press_key
from src.computer.tools.vision.scroll import scroll
from src.computer.tools.vision.scroll_page import scroll_page
from src.computer.tools.vision.show_warning import show_warning
from src.computer.tools.vision.wait_element import wait_element
from src.computer.tools.vision.write_text import write_text
from src.computer.tools.common.communicate_to_user import communicate_to_user
from src.computer.tools.common.display_message import display_message
from src.computer.tools.common.finish import finish
from src.computer.tools.common.get_clipboard_content import get_clipboard_content

TOOLS_METADATA = {
    "click": click,
    "double_click": double_click,
    "drag_and_drop": drag_and_drop,
    "hotkey": hotkey,
    "key_down": key_down,
    "key_up": key_up,
    "launch_app": launch_app,
    "press_key": press_key,
    "scroll": scroll,
    "scroll_page": scroll_page,
    "show_warning": show_warning,
    "wait_element": wait_element,
    "write_text": write_text,
    "communicate_to_user": communicate_to_user,
    "display_message": display_message,
    "finish": finish,
    "get_clipboard_content": get_clipboard_content,
}

def get_tools_metadata():
    return TOOLS_METADATA
