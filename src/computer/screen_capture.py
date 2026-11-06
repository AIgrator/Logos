"""Screen, window and region capture for the Terminal mode ``screenshot`` tool.

Window capture is Windows-only: ``PrintWindow`` with ``PW_RENDERFULLCONTENT``
(the only way to get a window that is covered by other windows), falling back
to a ``BitBlt`` of the window DC and finally to grabbing the window rectangle
off the screen. Screen and region grabs go through mss on every platform.

All coordinates are physical pixels, which is what the app sees because
``main.py`` asks for Per-Monitor V2 DPI awareness before ``QApplication``
exists, and every grab here uses the same physical space.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from dataclasses import dataclass

from PIL import Image

PW_RENDERFULLCONTENT = 0x00000002
SW_RESTORE = 9
SRCCOPY = 0x00CC0020
BI_RGB = 0
DWMWA_CLOAKED = 14


@dataclass(frozen=True)
class WindowInfo:
    """One visible top-level window."""

    hwnd: int
    title: str
    pid: int
    width: int
    height: int


if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    _user32.GetWindowDC.restype = wintypes.HDC
    _user32.GetWindowDC.argtypes = [wintypes.HWND]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _user32.PrintWindow.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL

    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                              wintypes.DWORD]
    _gdi32.BitBlt.restype = wintypes.BOOL
    _gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                 ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    _gdi32.GetDIBits.restype = ctypes.c_int

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", _BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3),
        ]

    try:
        _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        _dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                  ctypes.c_void_p, wintypes.DWORD]
        _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    except OSError:  # pragma: no cover - DWM is present on every supported Windows
        _dwmapi = None


# -- windows -----------------------------------------------------------------


def _window_rect(hwnd) -> tuple[int, int, int, int]:
    """(left, top, width, height) of a window in physical pixels."""
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0, 0, 0
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _is_cloaked(hwnd) -> bool:
    """True for the invisible UWP host windows that still enumerate as visible."""
    if _dwmapi is None:
        return False
    try:
        value = wintypes.DWORD(0)
        result = _dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return result == 0 and bool(value.value)
    except Exception:
        return False


def list_windows() -> list[WindowInfo]:
    """Every visible, titled, non-miniature top-level window.

    A minimized window is listed too (with its taskbar rectangle), because the
    user can ask for a screenshot of it; ``capture_window`` restores it before
    reading the pixels.
    """
    if sys.platform != "win32":
        return []
    windows: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            left, top, width, height = _window_rect(hwnd)
            if not _user32.IsIconic(hwnd) and (width < 32 or height < 32):
                return True
            windows.append(WindowInfo(int(hwnd), title, int(pid.value), width, height))
        except Exception:
            logging.debug("window enumeration callback failed", exc_info=True)
        return True

    _user32.EnumWindows(callback_type(_callback), 0)
    return windows


def window_titles(limit: int = 12) -> list[str]:
    """Titles of the visible windows, for "no such window" hints."""
    seen: list[str] = []
    for window in list_windows():
        if window.title not in seen:
            seen.append(window.title)
        if len(seen) >= limit:
            break
    return seen


def find_window(title: str = "", pid: int = 0) -> tuple[WindowInfo | None, list[WindowInfo]]:
    """Best match for a title substring and/or a process id, plus the rest.

    Ranking is exact title, then title prefix, then any substring; ties go to
    the larger window, because a tiny always-on-top helper is rarely the one
    the user means.
    """
    wanted = (title or "").strip().casefold()
    candidates = [
        window for window in list_windows()
        if (not pid or window.pid == int(pid))
        and (not wanted or wanted in window.title.casefold())
    ]
    if not candidates:
        return None, []

    def rank(window: WindowInfo):
        folded = window.title.casefold()
        if wanted and folded == wanted:
            tier = 0
        elif wanted and folded.startswith(wanted):
            tier = 1
        else:
            tier = 2
        return (tier, -window.width * window.height)

    candidates.sort(key=rank)
    return candidates[0], candidates[1:]


# -- capture -----------------------------------------------------------------


def _is_blank(image: Image.Image) -> bool:
    """A single-color frame means the window refused to render itself."""
    try:
        extrema = image.convert("RGB").getextrema()
        return all(low == high for low, high in extrema)
    except Exception:
        return False


def _bitmap_to_image(hdc_mem, bitmap, width: int, height: int) -> Image.Image | None:
    header = _BITMAPINFO()
    header.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.bmiHeader.biWidth = width
    header.bmiHeader.biHeight = -height  # negative == top-down rows
    header.bmiHeader.biPlanes = 1
    header.bmiHeader.biBitCount = 32
    header.bmiHeader.biCompression = BI_RGB
    buffer = ctypes.create_string_buffer(width * height * 4)
    copied = _gdi32.GetDIBits(hdc_mem, bitmap, 0, height, buffer,
                              ctypes.byref(header), 0)
    if copied != height:
        return None
    return Image.frombytes("RGB", (width, height), bytes(buffer), "raw", "BGRX")


def _capture_print_window(hwnd: int) -> Image.Image | None:
    """Ask the window to draw itself into a memory bitmap."""
    _left, _top, width, height = _window_rect(hwnd)
    if width <= 0 or height <= 0:
        return None
    hdc_window = _user32.GetWindowDC(hwnd)
    if not hdc_window:
        return None
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_window)
    bitmap = _gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    previous = None
    try:
        if not hdc_mem or not bitmap:
            return None
        previous = _gdi32.SelectObject(hdc_mem, bitmap)
        if not _user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT):
            logging.debug("PrintWindow refused hwnd %s; trying BitBlt", hwnd)
            if not _gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_window,
                                 0, 0, SRCCOPY):
                return None
        return _bitmap_to_image(hdc_mem, bitmap, width, height)
    except Exception:
        logging.debug("PrintWindow capture failed for hwnd %s", hwnd, exc_info=True)
        return None
    finally:
        try:
            if previous:
                _gdi32.SelectObject(hdc_mem, previous)
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            if hdc_mem:
                _gdi32.DeleteDC(hdc_mem)
            _user32.ReleaseDC(hwnd, hdc_window)
        except Exception:
            logging.debug("GDI cleanup failed for hwnd %s", hwnd, exc_info=True)


def _capture_window_on_screen(window: WindowInfo) -> Image.Image | None:
    """Bring the window forward and grab its rectangle off the screen."""
    try:
        if _user32.IsIconic(window.hwnd):
            _user32.ShowWindow(window.hwnd, SW_RESTORE)
        _user32.SetForegroundWindow(window.hwnd)
        time.sleep(0.35)
        left, top, width, height = _window_rect(window.hwnd)
        if width <= 0 or height <= 0:
            return None
        return capture_region(left, top, left + width, top + height)
    except Exception:
        logging.debug("screen fallback failed for hwnd %s", window.hwnd, exc_info=True)
        return None


def is_minimized(window: WindowInfo) -> bool:
    """True when the window is minimized (its rect is the taskbar sliver)."""
    if sys.platform != "win32":
        return False
    try:
        return bool(_user32.IsIconic(window.hwnd))
    except Exception:
        return False


def capture_window(window: WindowInfo) -> tuple[Image.Image, str]:
    """Capture one window; returns the image and the method that produced it."""
    if sys.platform != "win32":
        raise RuntimeError(
            "Window capture is only implemented on Windows. "
            "Grab the screen or a region instead.")
    if _user32.IsIconic(window.hwnd):
        # A minimized window reports its taskbar rectangle (-25600, -25600,
        # 159x27 or so) and PrintWindow happily paints that sliver of caption
        # buttons, so the caller would get a tiny "screenshot" that passes
        # every blank check. Restore it first - the screen fallback already
        # did - and capture the real window.
        _user32.ShowWindow(window.hwnd, SW_RESTORE)
        time.sleep(0.35)
        if _user32.IsIconic(window.hwnd):
            raise RuntimeError(
                f"The window '{window.title}' (pid {window.pid}) is minimized "
                "and could not be restored. Restore it, then ask again.")
    image = _capture_print_window(window.hwnd)
    if image is not None and not _is_blank(image):
        return image, "printwindow"
    image = _capture_window_on_screen(window)
    if image is not None and not _is_blank(image):
        return image, "screen"
    raise RuntimeError(
        f"Could not read pixels from the window '{window.title}' "
        f"(pid {window.pid}). It may be minimized, on a locked desktop or "
        "protected by DRM; restore it, or capture the screen/region instead.")


def _grab(bbox: dict) -> Image.Image:
    import mss

    with mss.mss() as sct:
        shot = sct.grab(bbox)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def capture_screen() -> Image.Image:
    """The primary display."""
    import mss

    with mss.mss() as sct:
        monitors = list(sct.monitors or [])
        if len(monitors) > 1:
            bbox = monitors[1]
        elif monitors:
            bbox = monitors[0]
        else:
            bbox = {"left": 0, "top": 0, "width": 0, "height": 0}
    if int(bbox.get("width") or 0) <= 0 or int(bbox.get("height") or 0) <= 0:
        raise RuntimeError("No display is available to capture.")
    return _grab(bbox)


def capture_region(left: int, top: int, right: int, bottom: int) -> Image.Image:
    """A rectangle in physical screen pixels: top-left and bottom-right corners."""
    left, top, right, bottom = int(left), int(top), int(right), int(bottom)
    if right <= left or bottom <= top:
        raise ValueError(
            "The region needs right > left and bottom > top, where "
            "(left, top) is the top-left corner and (right, bottom) the "
            "bottom-right one.")
    import mss

    with mss.mss() as sct:
        monitors = list(sct.monitors or [])
        # monitors[0] is the virtual screen: the union of every display, which
        # is also the coordinate space a screenshot occupies.
        area = monitors[0] if monitors else {}
        area_left = int(area.get("left") or 0)
        area_top = int(area.get("top") or 0)
        area_right = area_left + int(area.get("width") or 0)
        area_bottom = area_top + int(area.get("height") or 0)
        if (area_right > area_left and area_bottom > area_top and
                (left < area_left or top < area_top
                 or right > area_right or bottom > area_bottom)):
            raise ValueError(
                f"The region {left},{top} -> {right},{bottom} is outside the "
                f"screen, which covers x={area_left}..{area_right} and "
                f"y={area_top}..{area_bottom} in physical pixels.")
        shot = sct.grab({"left": left, "top": top,
                         "width": right - left, "height": bottom - top})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
