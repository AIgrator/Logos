"""
This module defines a simple worker for taking a screenshot in a background thread.
This prevents the UI from freezing or flickering while the main window is hidden.
"""
import logging
from PySide6.QtCore import QObject, Signal
from .capture import Capture

class ScreenshotWorker(QObject):
    """
    A worker that performs the screenshot operation in a background thread.
    """
    screenshot_finished = Signal(object)
    screenshot_failed = Signal(str)

    def __init__(self, window):
        """
        Args:
            window: The main application window to be hidden during the capture.
        """
        super().__init__()
        self.window = window
        # Note: The Capture object is created inside run() to ensure it's
        # instantiated and used within the same thread, avoiding potential
        # cross-thread issues with graphics libraries like mss.

    def run(self):
        """
        The main worker method that performs the screenshot.
        """
        try:
            logging.info("ScreenshotWorker started in background thread.")
            # It's safer to instantiate Capture here to ensure it lives in the worker thread
            capture_handler = Capture()
            screenshot = capture_handler.get_clean_screenshot(self.window)
            if screenshot:
                self.screenshot_finished.emit(screenshot)
            else:
                self.screenshot_failed.emit("The capture operation returned no image.")
        except Exception as e:
            logging.error(f"ScreenshotWorker failed: {e}", exc_info=True)
            self.screenshot_failed.emit(str(e))
