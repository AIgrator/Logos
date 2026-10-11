"""Low-level Windows keyboard input helpers (verified by experiment).

Findings (Oct 2026, Windows 11 + RU layout):
- pyautogui.hotkey() silently drops Win-key combos.
- pyautogui keyboard output is layout-dependent (VkKeyScanW fails for
  Latin chars under RU layout) and flaky for system dialogs.
- Raw keybd_event with medium gaps (~30ms) opens Win+R reliably.
- KEYEVENTF_UNICODE per-character injection types any script
  (Latin + Cyrillic confirmed visually) regardless of layout.
- The Run dialog does not always take keyboard focus on synthetic open:
  callers must ensure focus (see ensure_run_dialog_focus()) before typing.
"""
import ctypes
import logging
import time

logger = logging.getLogger(__name__)

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


def _user32():
    return ctypes.windll.user32
def send_text_unicode(text: str, interval: float = 0.02) -> None:
    """Type arbitrary text via KEYEVENTF_UNICODE (layout-independent)."""
    user32 = _user32()

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [('wVk', ctypes.c_ushort), ('wScan', ctypes.c_ushort),
                    ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                    ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        _fields_ = [('type', ctypes.c_ulong), ('ki', KEYBDINPUT)]

    for ch in text:
        for up in (False, True):
            extra = ctypes.c_ulong(0)
            ki = KEYBDINPUT(0, ord(ch),
                            KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                            0, ctypes.pointer(extra))
            inp = INPUT(1, ki)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        time.sleep(interval)


def find_run_dialog():
    """Return the Run dialog hwnd or 0."""
    user32 = _user32()
    return (user32.FindWindowW('#32770', 'Выполнить')
            or user32.FindWindowW('#32770', 'Run'))


def _run_edit_control(dlg):
    """Find the editable control inside the Run dialog."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_child(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        _user32().GetClassNameW(hwnd, cls, 256)
        if cls.value in ('Edit', 'ComboBoxEx32'):
            found.append(hwnd)
        return True

    _user32().EnumChildWindows(dlg, enum_child, 0)
    return found[0] if found else 0


def ensure_run_dialog_focus(timeout: float = 3.0) -> bool:
    """Bring the Run dialog up and force keyboard focus into its edit field.

    Returns True when the focused control is an Edit/ComboBoxEx32 inside
    the dialog. Used after Win+R because synthetic opening does not always
    transfer focus (verified: focus may stay on a CoreWindow/Chrome).
    """
    import time as _time
    user32 = _user32()
    deadline = _time.time() + timeout
    dlg = 0
    while _time.time() < deadline:
        dlg = find_run_dialog()
        if dlg:
            break
        _time.sleep(0.25)
    if not dlg:
        return False
    try:
        user32.SetForegroundWindow(dlg)
    except Exception:
        pass
    _time.sleep(0.2)
    edit = _run_edit_control(dlg)
    if edit:
        try:
            user32.SetFocus(edit)
        except Exception:
            pass
        _time.sleep(0.2)
    # Verify via GetGUIThreadInfo
    from ctypes import wintypes

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                    ('hwndActive', wintypes.HWND), ('hwndFocus', wintypes.HWND),
                    ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                    ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND),
                    ('rcCaret', wintypes.RECT)]
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    user32.GetGUIThreadInfo(0, ctypes.byref(info))
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(info.hwndFocus or 0, cls, 256)
    ok = cls.value in ('Edit', 'ComboBoxEx32', 'ComboBox')
    logger.info(f"Run dialog focus: hwnd={info.hwndFocus} class={cls.value} ok={ok}")
    return ok


# ---------------------------------------------------------------------------
# pynput-based input (primary path on Windows).
#
# Verified by experiment (Oct 2026): pyautogui's keyboard layer silently
# drops input on this setup (Win combos, layout-dependent chars, Run-dialog
# typing), while pynput press/release per character works everywhere
# (Notepad, browser address bar, Run dialog, both RU and EN layouts).
# ---------------------------------------------------------------------------

_PYNPUT_SPECIAL_KEYS = {
    'enter': 'enter', 'esc': 'esc', 'escape': 'esc', 'tab': 'tab',
    'space': 'space', 'backspace': 'backspace', 'delete': 'delete',
    'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right',
    'home': 'home', 'end': 'end', 'pageup': 'pageup', 'pagedown': 'pagedown',
    'capslock': 'caps_lock', 'shift': 'shift', 'ctrl': 'ctrl', 'alt': 'alt',
    'win': 'cmd', 'winleft': 'cmd', 'winright': 'cmd',
    'f1': 'f1', 'f2': 'f2', 'f3': 'f3', 'f4': 'f4', 'f5': 'f5',
    'f6': 'f6', 'f7': 'f7', 'f8': 'f8', 'f9': 'f9', 'f10': 'f10',
    'f11': 'f11', 'f12': 'f12',
}


def _pynput_controller():
    from pynput.keyboard import Controller
    return Controller()


def send_text_pynput(text: str, interval: float = 0.02) -> None:
    """Type text character by character via pynput (any alphabet)."""
    kb = _pynput_controller()
    import time as _time
    for ch in text:
        kb.press(ch)
        kb.release(ch)
        if interval:
            _time.sleep(interval)


def press_key_pynput(key_name: str) -> None:
    """Press a single named key via pynput."""
    from pynput.keyboard import Key
    kb = _pynput_controller()
    mapped = _PYNPUT_SPECIAL_KEYS.get(key_name.lower())
    if mapped is None:
        raise ValueError(f"Cannot map key '{key_name}' for pynput.")
    key = getattr(Key, mapped, None)
    if key is None:
        # Single printable character.
        kb.press(key_name)
        kb.release(key_name)
        return
    kb.press(key)
    kb.release(key)


def hotkey_pynput(keys: list, interval: float = 0.05) -> None:
    """Press a key combination via pynput (modifiers held properly)."""
    from pynput.keyboard import Key
    kb = _pynput_controller()
    import time as _time
    resolved = []
    for k in keys:
        mapped = _PYNPUT_SPECIAL_KEYS.get(k.lower())
        if mapped is not None and hasattr(Key, mapped):
            resolved.append(getattr(Key, mapped))
        elif len(k) == 1:
            resolved.append(k)
        else:
            raise ValueError(f"Cannot map key '{k}' for pynput.")
    pressed = []
    try:
        for k in resolved:
            kb.press(k)
            pressed.append(k)
            _time.sleep(interval)
    finally:
        for k in reversed(pressed):
            try:
                kb.release(k)
            except Exception:
                pass
