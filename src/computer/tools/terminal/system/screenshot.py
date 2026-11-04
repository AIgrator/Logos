import logging
import os
import time
from datetime import datetime

from src.computer import screen_capture
from src.computer.computer import tool_meta

# Keep the project folder from growing without bound: a screenshot per UI
# iteration adds up quickly. Newest files win, the same policy mcp_outputs uses.
MAX_KEPT_SCREENSHOTS = 200

_REGION_KEYS = ("left", "top", "right", "bottom")


@tool_meta(
    tool={
        "name": "screenshot",
        "description": (
            "Captures what is actually on screen and returns the saved PNG path, "
            "so you never have to guess how an app, a window or a rendered page looks. "
            "Call it with no arguments for the whole primary display, with window_title "
            "(a case-insensitive part of the title) or process_id for one window, or with "
            "the four region coordinates for a rectangle. Logos hides itself while a "
            "screen or region is captured, so its own window does not cover the app; pass "
            "hide_app=false to include it. The image is attached to your next turn, so look "
            "at it before deciding what to do next. Use it after launching the app you are "
            "building, after a UI change to verify the result, or when the user asks how "
            "something looks."),
        "arguments": {
            "window_title": {
                "type": "STRING",
                "description": ("Part of the title of one visible window, case-insensitive "
                                "(e.g. 'Notepad', 'catalog.html'). Wins over process_id. "
                                "Ignored when the four region coordinates are given. "
                                "Omit to capture the screen."),
                "required": False
            },
            "process_id": {
                "type": "INTEGER",
                "description": ("PID of the process whose window should be captured. Use it "
                                "when you started the app yourself and know its PID."),
                "required": False
            },
            "left": {
                "type": "INTEGER",
                "description": ("X of the region's top-left corner in physical screen pixels. "
                                "Requires top, right and bottom."),
                "required": False
            },
            "top": {
                "type": "INTEGER",
                "description": "Y of the region's top-left corner in physical screen pixels.",
                "required": False
            },
            "right": {
                "type": "INTEGER",
                "description": "X of the region's bottom-right corner in physical screen pixels.",
                "required": False
            },
            "bottom": {
                "type": "INTEGER",
                "description": "Y of the region's bottom-right corner in physical screen pixels.",
                "required": False
            },
            "hide_app": {
                "type": "BOOLEAN",
                "description": ("Hide Logos' own window during a screen or region capture so it "
                                "does not cover the app (default true). Ignored for a window "
                                "capture."),
                "required": False
            }
        },
        "returns": {
            "type": "OBJECT",
            "description": "Where the PNG was saved, its size and whether the model can see it.",
            "properties": {
                "path": {"type": "STRING", "description": "Absolute path of the saved PNG."},
                "relative_path": {"type": "STRING", "description": "Same file relative to the project root, when inside it."},
                "width": {"type": "INTEGER", "description": "Image width in pixels."},
                "height": {"type": "INTEGER", "description": "Image height in pixels."},
                "bytes": {"type": "INTEGER", "description": "Size of the PNG file."},
                "target": {"type": "STRING", "description": "What was captured: a window, a region or the screen."},
                "method": {"type": "STRING", "description": "Capture route: printwindow, screen or region."},
                "attached": {"type": "BOOLEAN", "description": "True when the image rides along with the next request."},
                "note": {"type": "STRING", "description": "Why the image could not be attached, when it was not."},
                "hint": {"type": "STRING", "description": "Other windows that also matched the title, left untouched."}
            }
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def screenshot(self, window_title: str | None = None, process_id: int | None = None,
               left: int | None = None, top: int | None = None,
               right: int | None = None, bottom: int | None = None,
               hide_app: bool = True) -> dict:
    """
    Captures a window, a screen rectangle or the whole primary display as PNG.
    """
    region_values = {"left": left, "top": top, "right": right, "bottom": bottom}
    given = {key: value for key, value in region_values.items() if value is not None}
    if given and len(given) != 4:
        raise ValueError(
            "Action 'screenshot' failed: a region needs all four coordinates "
            "(left, top, right, bottom); got " + ", ".join(sorted(given)) + ".")
    for key, value in list(region_values.items()):
        if value is None:
            continue
        try:
            region_values[key] = int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"Action 'screenshot' failed: '{key}' must be a whole number "
                "of screen pixels.") from None

    if given:
        if (region_values["right"] <= region_values["left"]
                or region_values["bottom"] <= region_values["top"]):
            raise ValueError(
                "Action 'screenshot' failed: the region needs right > left and "
                "bottom > top, where (left, top) is the top-left corner and "
                "(right, bottom) the bottom-right one.")

    title = str(window_title or "").strip()
    try:
        pid = int(process_id) if process_id not in (None, "") else 0
    except (TypeError, ValueError):
        raise ValueError(
            "Action 'screenshot' failed: 'process_id' must be a number.") from None

    hide = bool(hide_app)
    is_region = bool(given)
    target_window = None
    other_windows: list = []

    if not is_region and (title or pid):
        target_window, other_windows = screen_capture.find_window(title, pid)
        if target_window is None:
            wanted = f"'{title}'" if title else f"pid {pid}"
            available = screen_capture.window_titles(12)
            listed = "; ".join(available) if available else "(none)"
            raise ValueError(
                f"Action 'screenshot' failed: no visible window matches {wanted}. "
                f"Visible windows: {listed}. Call the tool without window_title/"
                "process_id for the whole screen, or with a shorter part of the title.")

    hidden_windows: list = []
    try:
        if is_region:
            if hide:
                hidden_windows = _hide_app()
            try:
                image = screen_capture.capture_region(
                    region_values["left"], region_values["top"],
                    region_values["right"], region_values["bottom"])
            except ValueError as error:
                raise ValueError(f"Action 'screenshot' failed: {error}") from None
            target = ("region {},{} \u2192 {},{}".format(
                region_values["left"], region_values["top"],
                region_values["right"], region_values["bottom"]))
            method = "region"
        elif target_window is not None:
            if hide and target_window.pid == os.getpid():
                raise ValueError(
                    "Action 'screenshot' failed: the matching window belongs to Logos "
                    "itself, and hide_app would hide the very thing being captured. "
                    "Pass hide_app=false to capture Logos, or pick another window.")
            image, method = screen_capture.capture_window(target_window)
            target = f"window '{target_window.title}' (pid {target_window.pid})"
        else:
            if hide:
                hidden_windows = _hide_app()
            image = screen_capture.capture_screen()
            target = f"screen {image.width}x{image.height}"
            method = "screen"
    finally:
        if hidden_windows:
            _show_app(hidden_windows)

    directory = _screenshots_dir(self)
    os.makedirs(directory, exist_ok=True)
    name = "screenshot_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3] + ".png"
    path = os.path.join(directory, name)
    image.save(path, "PNG")
    _prune(directory)

    attachable = _model_sees_images(getattr(self, "settings_manager", None))
    if attachable:
        setter = getattr(self, "set_pending_screenshot", None)
        if callable(setter):
            setter(path)

    result = {
        "path": path,
        "width": int(image.width),
        "height": int(image.height),
        "bytes": os.path.getsize(path),
        "target": target,
        "method": method,
        "attached": attachable,
    }
    relative = _relative_to_project(path, getattr(self, "working_directory", ""))
    if relative:
        result["relative_path"] = relative
    if not attachable:
        result["note"] = (
            "The active model cannot receive images, so this PNG is only an artifact "
            "on disk; switch to a model with image input to actually see it.")
    if other_windows:
        result["hint"] = ("Other matching windows were left alone: "
                          + "; ".join(window.title for window in other_windows[:5]))
    logging.info(f"screenshot: {target} -> {path} ({image.width}x{image.height})")
    return result


def _hide_app() -> list:
    """Hide this process's own main window(s) so it does not cover the target.

    The tool runs on the tool worker thread, so the hide/show calls are queued
    to the GUI thread and waited for, the same way the Vision screenshot does.
    """
    windows = _app_windows()
    _set_visible(windows, False)
    if windows:
        # Give the compositor a moment to repaint the exposed area.
        time.sleep(0.3)
    return windows


def _show_app(windows: list) -> None:
    _set_visible(windows, True)
    if windows:
        time.sleep(0.1)


def _app_windows() -> list:
    try:
        from PySide6.QtWidgets import QApplication, QMainWindow
    except Exception:
        return []
    app = QApplication.instance()
    if app is None:
        return []
    try:
        return [widget for widget in app.topLevelWidgets()
                if isinstance(widget, QMainWindow) and widget.isVisible()]
    except Exception:
        return []


def _set_visible(windows: list, visible: bool) -> None:
    try:
        from PySide6.QtCore import QMetaObject, Qt, QThread
    except Exception:
        return
    method = "show" if visible else "hide"
    for widget in windows:
        try:
            if widget.thread() is QThread.currentThread():
                if visible:
                    widget.show()
                else:
                    widget.hide()
                continue
            QMetaObject.invokeMethod(widget, method, Qt.BlockingQueuedConnection)
        except Exception:
            logging.debug("could not %s the app window during a screenshot",
                          method, exc_info=True)


def _screenshots_dir(computer) -> str:
    """``<project>/.logos/screenshots`` when a project is open, else the app's."""
    root = str(getattr(computer, "working_directory", "") or "")
    if root and os.path.isdir(root):
        try:
            from src.project_manager import PROJECT_DIRNAME
        except Exception:
            PROJECT_DIRNAME = ".logos"
        return os.path.join(root, PROJECT_DIRNAME, "screenshots")
    settings_manager = getattr(computer, "settings_manager", None)
    try:
        configured = settings_manager.get("APP_SCREENSHOTS_DIR") if settings_manager else ""
    except Exception:
        configured = ""
    if configured:
        return str(configured)
    return os.path.join(os.getcwd(), "screenshots")


def _relative_to_project(path: str, root: str) -> str:
    if not root:
        return ""
    try:
        common = os.path.commonpath([os.path.abspath(path), os.path.abspath(root)])
    except ValueError:
        return ""
    if os.path.normcase(common) != os.path.normcase(os.path.abspath(root)):
        return ""
    return os.path.relpath(path, root)


def _prune(directory: str, keep: int = MAX_KEPT_SCREENSHOTS) -> None:
    try:
        files = [os.path.join(directory, name) for name in os.listdir(directory)
                 if name.lower().endswith(".png")]
        if len(files) <= keep:
            return
        files.sort(key=os.path.getmtime, reverse=True)
        for stale in files[keep:]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except OSError:
        pass


def _model_sees_images(settings_manager) -> bool:
    """Whether a screenshot is worth attaching to the next request.

    The catalogue is the only trustworthy source, but it lags behind: a model
    the user configured by hand (an older Gemini, a fresh release) has no entry
    at all, and refusing to attach there would make the tool useless for the
    one model that is actually answering. So only a catalogue entry that
    explicitly lists modalities *without* ``image`` blocks the attachment;
    everything else rides along, the same assumption Browser mode makes with
    its own screenshots. A text-only model then fails the request loudly
    instead of the agent silently working blind.
    """
    if settings_manager is None:
        return False
    try:
        provider_id = str(settings_manager.get("active_provider") or "")
        providers = settings_manager.get("llm_providers") or {}
        model_id = str(((providers.get(provider_id) or {}).get("model")) or "")
    except Exception:
        return False
    if not provider_id or not model_id:
        return False
    try:
        from src.context.tokens import ModelMetadata
        modalities = ModelMetadata.input_modalities(provider_id, model_id)
    except Exception:
        logging.debug("could not read input modalities for %s::%s",
                      provider_id, model_id, exc_info=True)
        return True
    if modalities:
        return "image" in modalities
    return True
