"""
Detects OS-level blocks on *screen capture*.

Symmetric to input_access: the vision agent's second silent failure mode is
capture. Instead of erroring, screen grabs come back **black or empty**, so
the model "sees" nothing, loops forever and burns turns. This module turns
that into a cause + fix.

Per-platform reality (as of 2026):

  Windows - mss works (Desktop Duplication / BitBlt). No gate. (DRM-protected
            surfaces still come back black, but that is content, not a block.)
  macOS   - TCC Screen Recording. Without it CGWindowListCreateImage /
            screencapture return black frames. Detect with
            CGPreflightScreenCaptureAccess(). Note: after granting the user
            usually must restart the app for it to take effect.
  Linux   - X11 works. Wayland compositors do not expose the framebuffer to
            clients; mss goes through X11/XWayland and yields the (empty) X
            root for native apps. Needs grim/wayshot or the xdg-desktop-portal
            Screenshot API.

Any probe failure returns None ("assume usable") so a broken probe can never
break a run.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureBlock:
    """Why screen capture cannot return usable pixels right now."""

    platform: str
    reason: str            # one-line cause, shown to the user
    fix: str               # what the user should do about it
    detail: str = ""       # tech detail, logs only
    hard: bool = True      # True = captures are useless; False = may be blank

    def chat_message(self) -> str:
        return (f"Screen capture is blocked. {self.reason} "
                f"To fix it, {self.fix}")


def capture_block():
    """Return a CaptureBlock if screen capture is (probably) unusable, else None."""
    try:
        if sys.platform == "win32":
            return None
        if sys.platform == "darwin":
            return _macos_capture_block()
        if sys.platform.startswith("linux"):
            return _linux_capture_block()
    except Exception as e:  # never let the probe break the caller
        logging.warning(f"capture-access probe failed: {e}")
    return None


# ---------------------------------------------------------------------------
# macOS - TCC Screen Recording
# ---------------------------------------------------------------------------

def _macos_capture_block() -> CaptureBlock | None:
    import ctypes
    import ctypes.util

    path = (ctypes.util.find_library("CoreGraphics")
            or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    lib = ctypes.CDLL(path)
    preflight = lib.CGPreflightScreenCaptureAccess
    preflight.restype = ctypes.c_bool
    preflight.argtypes = []
    if preflight():
        return None
    return CaptureBlock(
        platform="darwin",
        reason=("macOS Screen Recording permission is not granted, so screen "
                "captures come back black/empty."),
        fix=("open System Settings > Privacy & Security > Screen Recording and "
             "enable Logos (or the terminal that launched it), then restart "
             "Logos."),
    )


# ---------------------------------------------------------------------------
# Linux - X11 vs Wayland
# ---------------------------------------------------------------------------

def _linux_capture_block() -> CaptureBlock | None:
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    x11 = bool(os.environ.get("DISPLAY"))
    if wayland and not x11:
        return CaptureBlock(
            platform="linux-wayland",
            reason=("this is a Wayland session; the X11 capture path has no "
                    "access to native Wayland windows."),
            fix=("install grim or wayshot, or use the xdg-desktop-portal "
                 "Screenshot API, or run an X11 session."),
        )
    if wayland and x11:
        return CaptureBlock(
            platform="linux-wayland-xwayland",
            reason=("this is a Wayland session; captures go through XWayland "
                    "and are usually black/empty for native Wayland windows."),
            fix=("install grim or wayshot, or use the xdg-desktop-portal "
                 "Screenshot API, or run an X11 session."),
            hard=False,
        )
    if x11:
        return None  # X11: mss grabs the framebuffer directly
    return CaptureBlock(
        platform="linux",
        reason="there is no graphical session (DISPLAY/WAYLAND_DISPLAY unset).",
        fix="run Logos inside a desktop session.",
    )
