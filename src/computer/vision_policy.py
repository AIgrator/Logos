"""Deterministic safety policy for Vision/CUA actions.

The model's prompt is advisory only. This module turns the current desktop
context (foreground window/process) and the structured action into explicit
checks before input is injected. It is intentionally conservative: unknown
context is never treated as safe when an allowlist is configured.

Nothing here executes input or talks to the GUI; ``vision_handler`` owns the
approval dialog and the actual performers.
"""
from __future__ import annotations

import logging
import os
import re
import sys

from src.browser.policy import is_sensitive_text

_TERMINAL_PROCESSES = {
    "cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe", "wt.exe",
    "windowsterminal.exe", "bash.exe", "wsl.exe", "wslhost.exe",
    "mintty.exe", "putty.exe", "kitty.exe", "alacritty.exe", "wezterm.exe",
    "gnome-terminal", "konsole", "xterm", "tmux", "screen",
}
_TERMINAL_TITLE = re.compile(
    r"\b(command prompt|powershell|windows powershell|terminal|cmd\.exe|"
    r"bash|wsl|console)\b",
    re.IGNORECASE,
)
_DESTRUCTIVE_TEXT = re.compile(
    r"(\brm\s+-[a-z]*r|\brmdir\b|\brd\s+/s|\bdel\s+/[a-z]|\bdel\s+|"
    r"\bformat\s+[a-z]:|\bdiskpart\b|\bclear-recyclebin\b|\bremove-item\b|"
    r"\bshutdown\b|\breboot\b|\bpoweroff\b|\bhalt\b|\bmkfs\b|\bdd\s+if=)",
    re.IGNORECASE,
)
_SHELL_COMMAND = re.compile(
    r"^\s*(powershell(\.exe)?|pwsh(\.exe)?|cmd(\.exe)?\s+/c|bash|sh|zsh|"
    r"python(\.exe)?\s+-c|pip\s|npm\s|git\s|rm\s|del\s|format\s|diskpart\b)",
    re.IGNORECASE,
)
_RISKY_TITLE = re.compile(
    r"\b(delete|remove|format|uninstall|reset this pc|disk management|"
    r"device manager|registry editor|system properties|network connections|"
    r"firewall|bitlocker|user account control|windows security|installer|"
    r"setup|repair|checkout|payment|purchase|pay|send|submit|publish|"
    r"confirm|order)\b",
    re.IGNORECASE,
)
_CREDENTIAL_TITLE = re.compile(
    r"\b(password|passcode|passwd|pin|login|sign in|log in|credential|"
    r"checkout|payment|credit card|debit card|card number|cvv|cvc|bank|"
    r"secret|token|api key|private key|seed phrase|recovery phrase)\b",
    re.IGNORECASE,
)
_APP_LAUNCH_TYPES = {"click", "double_click", "triple_click", "move", "drag",
                     "scroll", "type", "keypress", "hold_key",
                     "left_mouse_down", "left_mouse_up"}
_UWP_HOST_PROCESSES = {"applicationframehost.exe", "applicationframehost"}
_WINDOWS_SHELL_PROCESSES = {
    "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
}


def _empty_window_info() -> dict:
    return {"title": "", "process": "", "pid": 0, "aumid": ""}


def _uwp_child_pid(hwnd, parent_pid: int) -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        fallback = 0
        target = 0
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def inspect(child, _lparam):
            nonlocal fallback, target
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
            if not pid.value or int(pid.value) == int(parent_pid):
                return True
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, class_name, 256)
            if class_name.value == "Windows.UI.Core.CoreWindow":
                target = int(pid.value)
                return False
            if not fallback:
                fallback = int(pid.value)
            return True

        callback = callback_type(inspect)
        user32.EnumChildWindows(hwnd, callback, 0)
        return target or fallback
    except Exception as e:
        logging.warning(f"UWP child window probe failed: {e}")
        return 0


def _process_aumid(pid: int) -> str:
    if not pid or sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        getter = getattr(k32, "GetApplicationUserModelId", None)
        if getter is None:
            return ""
        getter.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                           wintypes.LPWSTR]
        getter.restype = wintypes.LONG
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ""
        try:
            length = wintypes.DWORD(0)
            result = getter(handle, ctypes.byref(length), None)
            if result not in (0, 122) or not length.value:
                return ""
            buf = ctypes.create_unicode_buffer(length.value)
            result = getter(handle, ctypes.byref(length), buf)
            return buf.value if result == 0 else ""
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return ""


def _window_info(hwnd) -> dict:
    """Title/process/pid/AUMID for a native window handle."""
    if not hwnd:
        return _empty_window_info()
    try:
        import ctypes

        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = _process_name(pid.value)
        info = {"title": title, "process": process,
                "pid": int(pid.value), "aumid": _process_aumid(pid.value)}
        if process.lower() in _UWP_HOST_PROCESSES:
            child_pid = _uwp_child_pid(hwnd, int(pid.value))
            child_process = _process_name(child_pid)
            if child_pid and child_process:
                info.update({
                    "process": child_process,
                    "pid": child_pid,
                    "aumid": _process_aumid(child_pid),
                    "host_process": process,
                    "host_pid": int(pid.value),
                })
        return info
    except Exception as e:
        logging.warning(f"window probe failed: {e}")
        return _empty_window_info()


def _windows_foreground() -> dict:
    """Best-effort foreground window info; empty dict on non-Windows/failure."""
    if sys.platform != "win32":
        return _empty_window_info()
    try:
        import ctypes

        return _window_info(ctypes.windll.user32.GetForegroundWindow())
    except Exception as e:
        logging.warning(f"foreground window probe failed: {e}")
        return _empty_window_info()


def _windows_window_at(x: float, y: float) -> dict:
    """Root window under a screen point (desktop/taskbar included)."""
    if sys.platform != "win32":
        return _empty_window_info()
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        point = wintypes.POINT(int(x), int(y))
        hwnd = user32.WindowFromPoint(point)
        if hwnd:
            root = user32.GetAncestor(hwnd, 2)  # GA_ROOT
            if root:
                hwnd = root
        return _window_info(hwnd)
    except Exception as e:
        logging.warning(f"window-at-point probe failed: {e}")
        return _empty_window_info()


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    try:
        import psutil

        return psutil.Process(int(pid)).name() or ""
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                 int(pid))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if k32.QueryFullProcessImageNameW(handle, 0, buf,
                                               ctypes.byref(size)):
                return os.path.basename(buf.value) or ""
        finally:
            k32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def active_window(x: float | None = None, y: float | None = None) -> dict:
    """Window context for the action gate.

    Mouse actions pass their target point: the window under the cursor is
    what matters (a click on the desktop/taskbar is Explorer even when an
    unrelated app is the foreground window). Keyboard actions fall back to
    the foreground window.
    """
    info = _windows_foreground()
    if x is not None and y is not None:
        target = _windows_window_at(x, y)
        if target.get("pid") or target.get("title") or target.get("process"):
            info = target
    info["own"] = bool(info.get("pid") and info["pid"] == os.getpid())
    return info


def _basename(value: str) -> str:
    text = str(value or "").strip().strip('"')
    for sep in ("\\", "/"):
        text = text.rsplit(sep, 1)[-1]
    return text.lower()


def _app_tokens(app) -> set[str]:
    if isinstance(app, str):
        values = [app]
    elif isinstance(app, dict):
        values = [app.get("name"), app.get("appid"), app.get("unique_id"),
                  app.get("path")]
    else:
        values = []
    tokens = set()
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        tokens.add(text)
        base = _basename(text)
        if base:
            tokens.add(base)
            if base.endswith(".exe"):
                tokens.add(base[:-4])
    return tokens


def _action_keys(action: dict) -> set[str]:
    keys = action.get("keys") or action.get("key")
    if isinstance(keys, str):
        keys = [keys]
    out: set[str] = set()
    for key in keys or []:
        for part in re.split(r"[+\s]+", str(key).strip().lower()):
            if part:
                out.add(part)
    return out


def _is_run_dialog_hotkey(action: dict) -> bool:
    kind = str(action.get("type") or "").lower()
    if kind not in ("hotkey", "keypress"):
        return False
    keys = _action_keys(action)
    win = bool(keys & {"win", "meta", "lwin", "rwin", "windows"})
    return win and "r" in keys


def _is_start_button_point(x: float, y: float) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        point = wintypes.POINT(int(x), int(y))
        hmon = user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return False
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return False
        mon, work = info.rcMonitor, info.rcWork
        if not (mon.left <= x < mon.right and mon.top <= y < mon.bottom):
            return False
        margin = 120
        if work.bottom < mon.bottom and y >= work.bottom and x <= work.left + margin:
            return True
        if work.top > mon.top and y < work.top and x <= work.left + margin:
            return True
        if work.left > mon.left and x < work.left and y <= work.top + margin:
            return True
        if work.right < mon.right and x >= work.right and y <= work.top + margin:
            return True
    except Exception as e:
        logging.warning(f"taskbar probe failed: {e}")
    return False


def _app_allowed(context: dict, allowed_apps, *,
                 system_surfaces_allowed: bool = False) -> bool:
    """Whether the foreground app is inside the configured allowlist.

    Empty allowlist means "no app restriction" (the legacy default). Our own
    window and Explorer are always allowed so the agent can see the desktop
    and use file dialogs; everything else must match a selected app.
    """
    apps = [a for a in (allowed_apps or []) if a]
    process = _basename(context.get("process"))
    title = str(context.get("title") or "").lower()
    aumid = str(context.get("aumid") or "").strip().lower()
    if context.get("own") or process in ("explorer.exe", "explorer"):
        return True
    if process in _WINDOWS_SHELL_PROCESSES:
        return bool(system_surfaces_allowed)
    if not apps:
        return True
    if not process and not title and not aumid:
        return False
    for app in apps:
        tokens = _app_tokens(app)
        for token in tokens:
            if not token:
                continue
            if aumid and (aumid == token or aumid.startswith(token + "!")):
                return True
            if process and (process == token or process == _basename(token)):
                return True
            if title and token in title:
                return True
    return False


def _is_terminal(context: dict) -> bool:
    process = _basename(context.get("process"))
    title = str(context.get("title") or "")
    return process in _TERMINAL_PROCESSES or bool(_TERMINAL_TITLE.search(title))


def action_point(action: dict):
    """Screen point a mouse action targets, or None for keyboard actions."""
    if not isinstance(action, dict):
        return None
    kind = str(action.get("type") or "").lower()
    if kind == "drag":
        path = action.get("path") or []
        if path and isinstance(path[0], dict):
            point = path[0]
        else:
            return None
    elif kind in ("click", "double_click", "triple_click", "move", "scroll"):
        point = action
    else:
        return None
    try:
        return float(point["x"]), float(point["y"])
    except Exception:
        return None


def _action_text(action: dict) -> str:
    if not isinstance(action, dict):
        return ""
    return str(action.get("text") or "")


def _action_label(action: dict) -> str:
    if not isinstance(action, dict):
        return "action"
    kind = str(action.get("type") or "?")
    if kind == "type":
        return f"type ({len(_action_text(action))} chars)"
    if kind in ("click", "double_click", "triple_click", "move"):
        return f"{kind} ({action.get('x')}, {action.get('y')})"
    if kind == "keypress":
        keys = action.get("keys") or []
        return f"keypress ({','.join(str(k) for k in keys)})"
    if kind == "hold_key":
        keys = action.get("keys") or []
        return f"hold_key ({','.join(str(k) for k in keys)})"
    if kind == "drag":
        return "drag"
    if kind == "scroll":
        return "scroll"
    return kind


def _check(code: str, message: str, *, hard: bool = False,
           action_index: int = 0) -> dict:
    return {"code": code, "message": message, "hard": bool(hard),
            "action_index": int(action_index)}


def classify_action(action: dict, context: dict | None = None, *,
                    allowed_apps=None, shell_allowed: bool = False,
                    system_surfaces_allowed: bool = False,
                    screen_point=None,
                    action_index: int = 0) -> list[dict]:
    """Returns hard blocks and approval checks for one structured action."""
    if not isinstance(action, dict):
        return [_check("malformed_action", "Malformed computer action.", hard=True,
                       action_index=action_index)]
    context = context or {}
    kind = str(action.get("type") or "").lower()
    label = _action_label(action)
    title = str(context.get("title") or "")
    text = _action_text(action)
    checks: list[dict] = []

    if not system_surfaces_allowed:
        if _is_run_dialog_hotkey(action):
            checks.append(_check(
                "system_surface_blocked",
                "Windows Search / Start menu is disabled in Vision settings; "
                "refusing Win+R.",
                hard=True, action_index=action_index))
        if (screen_point is not None
                and kind in ("click", "double_click", "triple_click",
                             "left_mouse_down", "left_mouse_up")
                and _is_start_button_point(*screen_point)):
            checks.append(_check(
                "system_surface_blocked",
                "Windows Search / Start menu is disabled in Vision settings; "
                "refusing the taskbar Start button.",
                hard=True, action_index=action_index))

    mutating = kind in _APP_LAUNCH_TYPES
    if mutating and not _app_allowed(
            context, allowed_apps,
            system_surfaces_allowed=system_surfaces_allowed):
        checks.append(_check(
            "app_not_allowed",
            f"Foreground app '{context.get('process') or context.get('title') or 'unknown'}' "
            f"is outside Vision allowed_apps; refusing {label}.",
            hard=True, action_index=action_index))

    terminal = _is_terminal(context)
    if terminal and not shell_allowed and kind in (
            "type", "keypress", "hold_key"):
        checks.append(_check(
            "shell_disabled",
            f"Shell commands are disabled in Vision settings; refusing {label} "
            f"in terminal '{title or context.get('process') or 'terminal'}'.",
            hard=True, action_index=action_index))
    elif not shell_allowed and kind == "type" and _SHELL_COMMAND.search(text):
        checks.append(_check(
            "shell_disabled",
            "Shell commands are disabled in Vision settings; refusing to type "
            "a shell command.",
            hard=True, action_index=action_index))

    risky_title = bool(_RISKY_TITLE.search(title))
    if kind == "type" and _DESTRUCTIVE_TEXT.search(text):
        checks.append(_check(
            "destructive_command",
            f"{label} contains a destructive command; explicit approval required.",
            action_index=action_index))
    if risky_title and kind in ("click", "double_click", "triple_click",
                                "keypress", "hotkey"):
        checks.append(_check(
            "irreversible_action",
            f"{label} in window '{title[:120]}' may delete/install/send/pay; "
            f"explicit approval required.",
            action_index=action_index))
    if _CREDENTIAL_TITLE.search(title) and kind in (
            "type", "keypress", "hold_key"):
        checks.append(_check(
            "credential_input",
            f"{label} targets a credential/payment window ('{title[:120]}'); "
            f"explicit approval required.",
            action_index=action_index))
    if kind == "type" and is_sensitive_text(text):
        checks.append(_check(
            "sensitive_text",
            f"{label} looks like a password/secret/credential value; "
            f"explicit approval required.",
            action_index=action_index))
    return checks


def classify_actions(actions, context: dict | None = None, *,
                     allowed_apps=None, shell_allowed: bool = False,
                     system_surfaces_allowed: bool = False) -> list[dict]:
    out: list[dict] = []
    for index, action in enumerate(actions or []):
        out.extend(classify_action(
            action, context, allowed_apps=allowed_apps,
            shell_allowed=shell_allowed,
            system_surfaces_allowed=system_surfaces_allowed,
            action_index=index))
    return out


def redact_action(action: dict) -> dict:
    """Copy of an action safe for logs/traces (never echoes typed text)."""
    if not isinstance(action, dict):
        return {"value": str(action)[:200]}
    safe = {}
    for key, value in action.items():
        if key == "text":
            safe[key] = f"<redacted:{len(str(value or ''))}>"
        elif key == "_result" and isinstance(value, dict):
            safe[key] = {k: v for k, v in value.items() if k != "image_b64"}
        else:
            safe[key] = value
    return safe


def redact_answer(question: str, answer: str) -> str:
    if is_sensitive_text(question, answer):
        return "***"
    return answer
