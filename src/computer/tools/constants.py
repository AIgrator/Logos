import sys

# Full list of valid key strings compatible with pyautogui.press/hotkey
KEYBOARD_KEYS = [
    '\t', '\n', '\r', ' ', '!', '"', '#', '$', '%', '&', "'", '(',
    ')', '*', '+', ',', '-', '.', '/', '0', '1', '2', '3', '4', '5', '6', '7',
    '8', '9', ':', ';', '<', '=', '>', '?', '@', '[', '\\', ']', '^', '_', '`',
    'a', 'b', 'c', 'd', 'e','f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o',
    'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', '{', '|', '}', '~',
    'accept', 'add', 'alt', 'altleft', 'altright', 'apps', 'backspace',
    'browserback', 'browserfavorites', 'browserforward', 'browserhome',
    'browserrefresh', 'browsersearch', 'browserstop', 'capslock', 'clear',
    'convert', 'ctrl', 'ctrlleft', 'ctrlright', 'decimal', 'del', 'delete',
    'divide', 'down', 'end', 'enter', 'esc', 'escape', 'execute', 'f1', 'f10',
    'f11', 'f12', 'f13', 'f14', 'f15', 'f16', 'f17', 'f18', 'f19', 'f2', 'f20',
    'f21', 'f22', 'f23', 'f24', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9',
    'final', 'fn', 'hanguel', 'hangul', 'hanja', 'help', 'home', 'insert', 'junja',
    'kana', 'kanji', 'launchapp1', 'launchapp2', 'launchmail',
    'launchmediaselect', 'left', 'modechange', 'multiply', 'nexttrack',
    'nonconvert', 'num0', 'num1', 'num2', 'num3', 'num4', 'num5', 'num6',
    'num7', 'num8', 'num9', 'numlock', 'pagedown', 'pageup', 'pause', 'pgdn',
    'pgup', 'playpause', 'prevtrack', 'print', 'printscreen', 'prntscrn',
    'prtsc', 'prtscr', 'return', 'right', 'scrolllock', 'select', 'separator',
    'shift', 'shiftleft', 'shiftright', 'sleep', 'space', 'stop', 'subtract', 'tab',
    'up', 'volumedown', 'volumemute', 'volumeup', 'win', 'winleft', 'winright', 'yen',
    'command', 'option', 'optionleft',
    'optionright'
]

# A curated map of display names to pyautogui key strings for use in UI components.
UI_KEY_COMMON = {
    'A': 'a', 'B': 'b', 'C': 'c', 'D': 'd', 'E': 'e', 'F': 'f', 'G': 'g', 'H': 'h', 'I': 'i',
    'J': 'j', 'K': 'k', 'L': 'l', 'M': 'm', 'N': 'n', 'O': 'o', 'P': 'p', 'Q': 'q', 'R': 'r',
    'S': 's', 'T': 't', 'U': 'u', 'V': 'v', 'W': 'w', 'X': 'x', 'Y': 'y', 'Z': 'z',
    '0': '0', '1': '1', '2': '2', '3': '3', '4': '4', '5': '5', '6': '6', '7': '7', '8': '8', '9': '9',
    'F1': 'f1', 'F2': 'f2', 'F3': 'f3', 'F4': 'f4', 'F5': 'f5', 'F6': 'f6',
    'F7': 'f7', 'F8': 'f8', 'F9': 'f9', 'F10': 'f10', 'F11': 'f11', 'F12': 'f12',
    'Enter': 'enter', 'Tab': '\t', 'Esc': 'escape', 'Space': 'space', 'Delete': 'delete',
    'Backspace': 'backspace', 'Home': 'home', 'End': 'end', 'PageUp': 'pageup', 'PageDown': 'pagedown',
    'Insert': 'insert', 'PrintScreen': 'printscreen', 'ScrollLock': 'scrolllock', 'Pause': 'pause',
    'Menu': 'apps', 'Up': 'up', 'Down': 'down', 'Left': 'left', 'Right': 'right',
}
UI_KEY_SYMBOLS = {
    # Symbols
    '-': '-', '=': '=', '[': '[', ']': ']', ';': ';', '\\': '\\',
    ',': ',', '.': '.', '/': '/', '`': '`', "'": "'",
    # Shifted Symbols
    '!': '!', '@': '@', '#': '#', '$': '$', '%': '%', '^': '^',
    '&': '&', '*': '*', '(': '(', ')': ')', '_': '_', '+': '+',
}
UI_KEY_MAP = {**UI_KEY_COMMON, **UI_KEY_SYMBOLS}
UI_MODIFIER_KEYS = [
    'alt', 'altleft', 'altright', 'ctrl', 'ctrlleft', 'ctrlright', 'shift', 'shiftleft',
    'shiftright', 'win', 'winleft', 'winright', 'command', 'option', 'optionleft',
    'optionright', 'left', 'right', 'up', 'down',
]

SHELL_COMMAND_EXAMPLE = "'dir', 'type file.txt'" if sys.platform == "win32" else "'ls -l', 'cat file.txt'"
