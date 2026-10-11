"""
This module acts as the high-level interface for the vision system.
It orchestrates screen analysis by using the capture and parser adapter modules.
"""
import logging
from PIL import Image
from PySide6.QtWidgets import QWidget

from .parser_adapter import ParserAdapter
from .capture import Capture
from .grounding import create_backend

class VisionManager:
    """
    Manages the entire vision pipeline, from capturing the screen to parsing UI elements.
    This class acts as a facade, providing a simple interface to the complex underlying systems.
    """
    def __init__(self):
        self.parser_adapter = ParserAdapter()
        self.grounding = None  # GroundingBackend; created in initialize_parser_async
        self.capture_handler = Capture()
        self.parser_initialized = False

    @property
    def grounding_backend_name(self) -> str:
        if self.grounding is not None:
            return self.grounding.name
        return "local"

    def update_capture_settings(self, new_settings: dict):
        """
        Updates settings for the capture module only. This is a quick operation.
        """
        logging.info("Vision capture settings updated.")
        self.capture_handler.update_settings(new_settings)

    def initialize_parser_async(self, new_settings: dict):
        """
        Initializes the parser. This is a long-running, blocking operation.
        The grounding backend is selected via settings (default: local).
        """
        if self.parser_initialized:
            logging.info("Parser is already initialized.")
            return

        backend_name = (new_settings.get("vision", {}) or {}).get("grounding_backend", "local")
        self.grounding = create_backend(backend_name)
        logging.info(f"Starting heavy initialization of grounding backend '{self.grounding.name}'...")
        if self.grounding.name == "local":
            # Local backend reuses the shared ParserAdapter instance so OCR
            # re-initialization from settings keeps working unchanged.
            self.parser_adapter.initialize_parser(new_settings)
            # Keep the adapter reachable through the backend as well.
            try:
                self.grounding._adapter = self.parser_adapter
            except AttributeError:
                pass
        else:
            self.grounding.initialize(new_settings)
        self.parser_initialized = True
        logging.info(f"Grounding backend '{self.grounding.name}' has been successfully initialized.")

    def reinitialize_ocr(self, new_settings: dict, status_callback=None):
        """Proxy method to re-initialize OCR in the parser adapter."""
        if self.parser_adapter:
            self.parser_adapter.reinitialize_ocr(new_settings, status_callback)

    def analyze_screen(self, window: QWidget, settings_manager, screenshot: Image.Image = None) -> tuple:
        """
        Captures a clean screenshot if not provided, analyzes it, and returns the results.
        """
        logging.info("Starting screen analysis...")
        clean_screenshot = screenshot if screenshot else self.get_clean_screenshot(window)

        if not clean_screenshot:
            logging.error("Screen analysis failed because screenshot could not be taken or was not provided.")
            return None, None, None

        if not self.parser_initialized:
            logging.warning("Parser not initialized yet. Returning clean screenshot only.")
            return clean_screenshot, None, None

        # Delegate analysis task to the parser adapter
        _, annotated_frame, parsed_content = self.parser_adapter.analyze_image(clean_screenshot, settings_manager)

        if parsed_content is None:
            logging.warning("Screen analysis did not return expected results.")
            return clean_screenshot, None, None

        logging.info("Screen analysis completed successfully.")
        return clean_screenshot, annotated_frame, parsed_content

    def get_clean_screenshot(self, window: QWidget):
        """Provides direct access to the capture module's screenshot function."""
        return self.capture_handler.get_clean_screenshot(window)

    def compress_image_to_bytes(self, image: Image.Image) -> bytes:
        """Provides direct access to the capture module's image compression function."""
        return self.capture_handler.compress_image_to_bytes(image)
