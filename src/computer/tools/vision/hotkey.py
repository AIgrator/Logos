import ctypes
import logging
import time
import pyautogui

from src.computer.computer import tool_meta
from src.computer.tools.constants import KEYBOARD_KEYS

# Set of key names that require the raw keybd_event path (see below).
_WIN_KEYS = frozenset({"win", "winleft", "winright"})

_VK_FALLBACK = {
    "win": 0x5B, "winleft": 0x5B, "winright": 0x5C,
    "ctrl": 0x11, "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "alt": 0x12, "altleft": 0xA4, "altright": 0xA5,
    "shift": 0x10, "shiftleft": 0xA0, "shiftright": 0xA1,
}

_KEYEVENTF_KEYUP = 0x0002


def _vk_code(key_name: str) -> int | None:
    """Virtual-key code for a KEYBOARD_KEYS name.

    Single alphanumeric characters map to their uppercase ASCII code
    (layout-independent: works regardless of the active keyboard layout,
    unlike VkKeyScanW which fails for Latin letters under RU layout).
    Named keys come from pyautogui's mapping with a builtin fallback.
    """
    if len(key_name) == 1 and key_name.isalnum():
        return ord(key_name.upper())
    try:
        from pyautogui._pyautogui_win import keyboardMapping
        code = keyboardMapping.get(key_name)
        if isinstance(code, int) and 0 < code < 0xFF:
            return code & 0xFF
    except Exception:
        pass
    if key_name in _VK_FALLBACK:
        return _VK_FALLBACK[key_name]
    if len(key_name) == 1:
        try:
            code = ctypes.windll.user32.VkKeyScanW(ctypes.wintypes.WCHAR(key_name))
            if code != -1:
                return code & 0xFF
        except Exception:
            pass
    return None


def _press_win_combo(keys: list) -> None:
    """Send a Win-key combination via raw keybd_event.

    Verified by experiment (Oct 2026): pyautogui.hotkey() and even manual
    pyautogui.keyDown/keyUp silently drop Win-key combos on this setup.
    Medium gaps (~30ms) are required: no gaps -> dialog never opens,
    long holds -> Start menu steals focus (CoreWindow).
    """
    user32 = ctypes.windll.user32
    codes = []
    for k in keys:
        vk = _vk_code('winleft' if k == 'win' else k)
        if vk is None:
            raise ValueError(f"Cannot map key '{k}' to a virtual-key code.")
        codes.append(vk)
    try:
        for vk in codes:
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.03)
        time.sleep(0.03)
    finally:
        for vk in reversed(codes):
            try:
                user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
            time.sleep(0.02)
    # Special case with verification: Win+R must leave an open, FOCUSED
    # Run dialog behind (synthetic opening often leaves focus elsewhere).
    if [k.lower() for k in keys] == ['win', 'r']:
        from src.computer.tools.win_keys import ensure_run_dialog_focus
        if not ensure_run_dialog_focus():
            raise ValueError(
                "Action 'hotkey' failed: Win+R did not leave the Run dialog focused. "
                "Example: {'name': 'hotkey', 'arguments': {'description': 'Open Run', 'keys': ['win', 'r']}}"
            )

@tool_meta(
    tool={
        "name": "hotkey",
        "description": "Presses a combination of keys (hotkey).(e.g., ['ctrl', 's'], ['ctrl', 'c'], ['alt', 'f4'])",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of the hotkey action (e.g., 'Pressing Ctrl+S to save.')", "required": True},
            "keys": {"type": "ARRAY", "items": {"type": "STRING", "enum": KEYBOARD_KEYS}, "description": "A list of key names to press in combination, e.g., ['ctrl', 's'], ['ctrl', 'c'], ['alt', 'f4'].", "required": True}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def hotkey(self, **kwargs):
    keys = kwargs.get('keys')
    if not isinstance(keys, list) or not keys:
        raise ValueError("Action 'hotkey' failed: The 'keys' argument must be a non-empty list of strings. Example: {'name': 'hotkey', 'arguments': {'description': 'Save file', 'keys': ['ctrl', 's']}}")

    try:
        for k in keys:
            if k not in KEYBOARD_KEYS:
                raise ValueError(f"Invalid key '{k}' in hotkey. See KEYBOARD_KEYS for supported values.")
        logging.info(f"Performing hotkey: {keys}")
        if any(k in _WIN_KEYS for k in keys):
            _press_win_combo(keys)
        elif len(keys) == 1:
            from src.computer.tools.win_keys import press_key_pynput
            try:
                press_key_pynput(keys[0])
            except Exception:
                pyautogui.press(keys[0])
        else:
            try:
                from src.computer.tools.win_keys import hotkey_pynput
                hotkey_pynput(keys)
            except Exception as e:
                logging.warning(f"pynput hotkey failed for {keys}, falling back to pyautogui: {e}")
                pyautogui.hotkey(*keys)
    except Exception as e:
        logging.error(f"Error pressing hotkey '{keys}': {e}")
        raise
