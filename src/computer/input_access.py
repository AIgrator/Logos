"""
Detects OS-level blocks on *synthetic* mouse/keyboard input.

The vision agent moves the real cursor and injects key events. Every modern
OS gates that behind a permission or a security boundary, and when the gate
is closed the calls fail **silently** (no exception, no error code): the
cursor simply does not move and clicks vanish. From the model's point of
view the action "succeeded", which is why the run looks catatonic.

This module is a best-effort probe that turns that silent failure into a
human-readable cause + fix, so the user understands WHY vision mode can't
drive the mouse and how to unblock it.

Per-platform reality (as of 2026):

  Windows - UIPI (User Interface Privilege Isolation). A process at a lower
            integrity level cannot move the cursor (SetCursorPos) or inject
            input (SendInput) while a window of a *higher* integrity level
            (any app started with "Run as administrator") is focused or under
            the target point. The calls fail with no error. Fix: run Logos
            elevated too, or keep elevated windows out of focus.
  macOS   - TCC Accessibility. CGEventPost is ignored unless the app is
            trusted in System Settings > Privacy & Security > Accessibility.
            Detect with AXIsProcessTrusted(). (Screen capture additionally
            needs the Screen Recording permission, which is a separate gate.)
  Linux   - X11 allows XTest injection for the same user. Wayland, by design,
            gives a client no API to drive other apps: it needs a uinput
            helper (ydotool/dotool) or a compositor portal. With XWayland the
            old XTest path reaches X11 clients only, never native Wayland apps.

Every probe is wrapped so a broken probe can never itself break a run: on any
error (or unknown platform) `input_block()` returns None, i.e. "assume usable".
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class InputBlock:
    """Why synthetic input cannot reach the desktop right now."""

    platform: str
    reason: str            # one-line cause, shown to the user
    fix: str               # what the user should do about it
    culprit: str = ""      # offending app/window, when known (Windows UIPI)
    detail: str = ""       # tech detail, logs only
    hard: bool = True      # True = injection surely fails, False = may fail

    def chat_message(self) -> str:
        return (f"Mouse/keyboard control is blocked. {self.reason} "
                f"To fix it, {self.fix}")


class InputBlockedError(RuntimeError):
    """Hard OS block on synthetic input (Windows UIPI, macOS TCC).

    Raised by the vision handler so the task stops immediately and the user
    is told to restart Logos as administrator — this cannot be fixed mid-task.
    """


def input_block(x: float | None = None, y: float | None = None):
    """Return an InputBlock if synthetic input is (probably) blocked, else None.

    `x, y` are optional *screen* coordinates of the point the agent is about
    to act on; when given, the window under that point is also checked (a
    click is delivered there, not necessarily to the foreground window).
    """
    try:
        if sys.platform == "win32":
            return _windows_block(x, y)
        if sys.platform == "darwin":
            return _macos_block()
        if sys.platform.startswith("linux"):
            return _linux_block()
    except Exception as e:  # never let the probe break the caller
        logging.warning(f"input-access probe failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Windows - integrity levels (UIPI)
# ---------------------------------------------------------------------------

# Integrity RIDs from winnt.h. MEDIUM (0x2000) is a normal user process.
_INTEGRITY_MEDIUM = 0x2000


def _proc_integrity_rid(pid: int) -> int | None:
    """Integrity level (last SID sub-authority) of a process, or None."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TokenIntegrityLevel = 25

    k32 = ctypes.windll.kernel32
    adv = ctypes.windll.advapi32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_ulong)
    adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            need = wintypes.DWORD(0)
            adv.GetTokenInformation(tok, TokenIntegrityLevel, None, 0,
                                    ctypes.byref(need))
            if not need.value:
                return None
            buf = ctypes.create_string_buffer(need.value)
            if not adv.GetTokenInformation(tok, TokenIntegrityLevel, buf,
                                           need.value, ctypes.byref(need)):
                return None
            # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES Label; }
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
            count = adv.GetSidSubAuthorityCount(sid).contents.value
            if not count:
                return None
            return int(adv.GetSidSubAuthority(sid, count - 1).contents.value)
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


def _hwnd_pid(hwnd) -> int:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _proc_name(pid: int) -> str:
    """Executable basename of a process, or '?' if unreadable."""
    import ctypes
    import os as _os
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return _os.path.basename(buf.value) or "?"
        return "?"
    finally:
        k32.CloseHandle(h)


def _window_title(hwnd) -> str:
    import ctypes

    user32 = ctypes.windll.user32
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _windows_block(x, y) -> InputBlock | None:
    import ctypes
    from ctypes.wintypes import POINT

    user32 = ctypes.windll.user32
    mine = _proc_integrity_rid(os.getpid())
    if mine is None:
        return None

    targets: list[tuple[str, int]] = []
    fg = user32.GetForegroundWindow()
    if fg:
        targets.append(("foreground", fg))
    if x is not None and y is not None:
        hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
        if hwnd and hwnd != fg:
            targets.append(("under the target point", hwnd))

    for where, hwnd in targets:
        pid = _hwnd_pid(hwnd)
        if not pid:
            continue
        rid = _proc_integrity_rid(pid)
        if rid is not None and rid > mine:
            exe = _proc_name(pid)
            title = _window_title(hwnd)
            culprit = f'{exe} (PID {pid})'
            if title:
                culprit += f', window "{title}"'
            return InputBlock(
                platform="win32",
                reason=("Windows UIPI blocks a non-elevated process from "
                        "moving the cursor or injecting input while an "
                        "elevated (\"Run as administrator\") window is "
                        f"{where}. The offending app is {culprit}."),
                fix=("restart Logos as administrator so it runs elevated too "
                     "(this cannot be fixed without a restart)."),
                culprit=culprit,
                detail=(f"integrity ours=0x{mine:04x} "
                        f"({where}) pid={pid} 0x{rid:04x}"),
            )
    return None


# ---------------------------------------------------------------------------
# macOS - TCC Accessibility
# ---------------------------------------------------------------------------

def _macos_block() -> InputBlock | None:
    import ctypes
    import ctypes.util

    path = (ctypes.util.find_library("ApplicationServices")
            or "/System/Library/Frameworks/ApplicationServices.framework/"
               "ApplicationServices")
    lib = ctypes.CDLL(path)
    trusted = lib.AXIsProcessTrusted
    trusted.restype = ctypes.c_bool
    trusted.argtypes = []
    if trusted():
        return None
    return InputBlock(
        platform="darwin",
        reason=("macOS Accessibility permission is not granted, so synthetic "
                "mouse/keyboard events are silently dropped."),
        fix=("open System Settings > Privacy & Security > Accessibility and "
             "enable Logos (or the terminal app that launched it)."),
    )


# ---------------------------------------------------------------------------
# Linux - X11 vs Wayland
# ---------------------------------------------------------------------------

def _linux_block() -> InputBlock | None:
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    x11 = bool(os.environ.get("DISPLAY"))
    if wayland and not x11:
        return InputBlock(
            platform="linux-wayland",
            reason=("Wayland gives an application no API to drive another "
                    "app's pointer/keyboard."),
            fix=("install and run a uinput helper such as ydotool or dotool "
                 "(needs /dev/uinput and the user in the input group), or run "
                 "an X11 session."),
        )
    if wayland and x11:
        return InputBlock(
            platform="linux-wayland-xwayland",
            reason=("this is a Wayland session; the XTest path reaches only "
                    "X11 (XWayland) clients, not native Wayland apps."),
            fix=("use ydotool/dotool to drive native Wayland apps, or run an "
                 "X11 session."),
            hard=False,
        )
    if x11:
        return None  # X11: XTest injection works for the same user
    return InputBlock(
        platform="linux",
        reason="there is no graphical session (DISPLAY/WAYLAND_DISPLAY unset).",
        fix="run Logos inside a desktop session.",
    )
