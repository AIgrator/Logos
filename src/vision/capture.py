"""
This module is responsible for screen capture functionalities using an object-oriented approach.
"""
import time
import io
import logging
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QMetaObject, Qt, QThread
import mss
from PIL import Image

class Capture:
    """
    Handles all screen capture and image compression operations.
    This class encapsulates settings and provides a clear interface for capturing the screen.
    """
    def __init__(self, initial_settings: dict = None):
        """
        Initializes the Capture handler with optional settings.
        """
        self.settings = initial_settings if initial_settings is not None else {}
        logging.info(f"Capture module initialized. WebP quality: {self.settings.get('webp_quality', 80)}")

    def update_settings(self, new_settings: dict):
        """Updates the settings for the capture module."""
        self.settings = new_settings
        logging.info(f"Capture module settings updated. WebP quality: {self.settings.get('webp_quality')}")

    def get_screenshot(self) -> Image.Image:
        """
        Captures the entire screen and returns it as a Pillow Image object.

        Returns:
            Image.Image: The captured screenshot.
        """
        with mss.mss() as sct:
            sct_img = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            return img

    def get_clean_screenshot(self, window: QWidget) -> Image.Image:
        """
        Hides the specified window, takes a screenshot, and returns it as a PIL Image.
        This method is thread-safe and automatically detects if it's called from the
        main GUI thread or a background thread to avoid deadlocks.

        Args:
            window (QWidget): The window to hide and show.

        Returns:
            Image.Image: The screenshot as a PIL Image object, or None on failure.
        """
        is_main_thread = QThread.currentThread() == window.thread()

        def hide_window():
            if is_main_thread:
                window.hide()
            else:
                QMetaObject.invokeMethod(window, "hide", Qt.BlockingQueuedConnection)

        def show_window():
            if is_main_thread:
                window.show()
            else:
                QMetaObject.invokeMethod(window, "show", Qt.BlockingQueuedConnection)

        try:
            hide_window()
            time.sleep(0.3)
            screenshot = self.get_screenshot()
            return screenshot
        except Exception as e:
            logging.error(f"Failed to get a clean screenshot: {e}")
            return None
        finally:
            show_window()

    def compress_image_to_bytes(self, image: Image.Image) -> bytes:
        """
        Compresses a PIL Image into WebP format in memory, using instance settings.

        Args:
            image (Image.Image): The image to compress.

        Returns:
            bytes: The compressed screenshot in WebP format.
        """
        if not image:
            return None
        try:
            use_lossless = self.settings.get("webp_lossless", True)
            quality = self.settings.get("webp_quality", 80)

            logging.info(f"Compressing image with WebP. Lossless: {use_lossless}, Quality: {quality if not use_lossless else 'N/A'}")

            buffer = io.BytesIO()
            image.save(buffer, "webp", lossless=use_lossless, quality=quality)
            return buffer.getvalue()
        except Exception as e:
            logging.error(f"Failed to compress image: {e}")
            return None
