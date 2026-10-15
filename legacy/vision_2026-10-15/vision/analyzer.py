"""
This module encapsulates the logic for analyzing a screenshot.
"""
import logging
from PIL import Image

class VisionAnalyzer:
    def __init__(self, vision_manager, settings_manager):
        self.vision_manager = vision_manager
        self.settings_manager = settings_manager

    def analyze_screen(self, screenshot: Image.Image) -> tuple[Image.Image | None, object | None, list[str] | None]:
        """
        Runs the parser on a pre-captured screenshot.

        Args:
            screenshot: The pre-captured screenshot (Pillow Image).

        Returns:
            A tuple containing:
            - The clean screenshot (Pillow Image).
            - The annotated frame (object, depends on parser).
            - The parsed content (list of strings).
        """
        if not self.vision_manager or not self.vision_manager.parser_initialized:
            logging.warning("VisionAnalyzer: VisionManager not initialized. Skipping analysis.")
            return None, None, None

        try:
            # Pass the screenshot directly to the manager's analysis function
            return self.vision_manager.analyze_screen(None, self.settings_manager, screenshot=screenshot)
        except Exception as e:
            logging.error(f"An error occurred during screen analysis: {e}", exc_info=True)
            return None, None, None
