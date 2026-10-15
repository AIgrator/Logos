"""
This module acts as an adapter for the OmniParser library, providing a simplified
interface for analyzing UI screenshots. It handles initialization of the parser,
manages different parsing modes (local vs. API-based), and formats the output.
"""
import base64
import io
import logging
import json
from PIL import Image
import numpy as np
import sys
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

# Conditional import for the actual parser library
try:
    from src.omniparser.util.omniparser import Omniparser as OmniParser
    OMNIPARSER_AVAILABLE = True
except ImportError as e:
    logging.error(f"OmniParser library not found. Local parsing will be disabled. Error: {e}", exc_info=True)
    OmniParser = None
    OMNIPARSER_AVAILABLE = False

class ParserAdapter:
    def __init__(self):
        self.parser_instance = None
        self.is_initialized = False
        self.omniparser_settings = {}

    def initialize_parser(self, global_settings: dict):
        """Initializes the OmniParser instance if not already done."""
        if self.is_initialized:
            return

        self.omniparser_settings = global_settings.get('omniparser', {})
        mode = self.omniparser_settings.get('mode', 'local')

        if mode == "local":
            if not OMNIPARSER_AVAILABLE:
                logging.error("Cannot initialize OmniParser in 'local' mode: library is not available.")
                self.is_initialized = True
                return

            try:
                local_parser_config = {
                    'som_model_path': self.omniparser_settings.get('model_path'),
                    'caption_model_name': self.omniparser_settings.get('caption_model_name'),
                    'caption_model_path': self.omniparser_settings.get('caption_model_path'),
                    'box_threshold': self.omniparser_settings.get('box_threshold'),
                    'ocr_languages': self.omniparser_settings.get('ocr_languages', ['en'])
                }

                logging.info(f"Initializing OmniParser with caption model: {local_parser_config['caption_model_name']}")
                self.parser_instance = OmniParser(local_parser_config)
                logging.info("OmniParser initialized successfully in local mode.")
            except Exception as e:
                logging.error(f"Failed to initialize OmniParser: {e}", exc_info=True)
                self.parser_instance = None

        self.is_initialized = True

    def reinitialize_ocr(self, new_settings: dict, status_callback=None):
        """
        Specifically re-initializes the OCR component with new languages.
        This is intended to be called from the settings window to trigger downloads.
        """
        if not self.parser_instance:
            logging.error("Cannot re-initialize OCR: parser instance is not available.")
            return

        new_parser_settings = new_settings.get('omniparser', {})
        new_languages = new_parser_settings.get('ocr_languages', ['en'])

        # Update internal settings cache to stay in sync
        self.omniparser_settings = new_parser_settings

        try:
            self.parser_instance.update_languages(new_languages, status_callback)
        except Exception as e:
            logging.error(f"Failed to re-initialize OCR languages: {e}", exc_info=True)

    def update_settings(self, new_settings: dict):
        """Updates the parser's settings and re-initializes it if necessary."""
        new_parser_settings = new_settings.get('omniparser', {})
        mode_changed = new_parser_settings.get("mode") != self.omniparser_settings.get("mode")

        # Always update the internal settings reference
        self.omniparser_settings = new_parser_settings

        if mode_changed:
            logging.info("Parser mode changed. Re-initializing...")
            self.is_initialized = False
            # The main initialization logic will pick up the new settings on next use

    def _save_trace_log(self, data):
        """Saves the provided data to the trace log file if enabled."""
        if self.omniparser_settings.get("save_trace_log"):
            trace_path = self.omniparser_settings.get("trace_log_path", "logs/parser_trace.json")
            try:
                with open(trace_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                logging.info(f"Trace log saved to {trace_path}")
            except Exception as e:
                logging.error(f"Failed to save trace log: {e}", exc_info=True)

    def analyze_image(self, image_input, settings_manager) -> tuple[str | None, np.ndarray | None, list | None]:
        """
        Analyzes the given UI screenshot, cleans the data, converts coordinates,
        and returns the annotated image and structured, clean data.
        """
        if not self.is_initialized:
            logging.error("Parser not initialized. Call initialize_parser first.")
            return None, None, None

        pil_image = self._get_pil_image(image_input)
        if not pil_image:
            return None, None, None

        image_base64 = self._pil_to_base64(pil_image)
        if not image_base64:
            return None, None, None

        mode = self.omniparser_settings.get('mode', 'local')
        if mode == "local":
            return self._analyze_local(image_base64, pil_image, settings_manager)
        else:
            logging.error(f"Invalid PARSER_MODE: {mode}")
            return None, None, None

    def _get_pil_image(self, image_input):
        if isinstance(image_input, Image.Image):
            return image_input
        if isinstance(image_input, str):
            try:
                image_bytes = base64.b64decode(image_input)
                return Image.open(io.BytesIO(image_bytes))
            except Exception as e:
                logging.error(f"Failed to convert base64 to PIL Image: {e}", exc_info=True)
                return None
        logging.error(f"Invalid input type for analyze_image: {type(image_input)}. Expected PIL Image or base64 string.")
        return None

    def _pil_to_base64(self, pil_image):
        try:
            buffered = io.BytesIO()
            pil_image.save(buffered, format="PNG")
            return base64.b64encode(buffered.getvalue()).decode('utf-8')
        except Exception as e:
            logging.error(f"Failed to convert PIL Image to base64: {e}", exc_info=True)
            return None

    def _analyze_local(self, image_base64, pil_image, settings_manager):
        if not self.parser_instance:
            logging.error("Local parser is not available. Cannot analyze image.")
            return None, None, None

        try:
            annotated_image_b64, annotated_frame_np, raw_parsed_content = self.parser_instance.parse(
                image_base64,
                settings_manager=settings_manager
            )

            clean_parsed_content = self._clean_and_transform(raw_parsed_content, pil_image.size)

            self._save_trace_log(clean_parsed_content)
            return annotated_image_b64, annotated_frame_np, clean_parsed_content
        except Exception as e:
            logging.error(f"An error occurred during image analysis with OmniParser: {e}", exc_info=True)
            return None, None, None

    def _clean_and_transform(self, raw_content, image_size):
        if not raw_content:
            return []

        clean_content = []
        img_width, img_height = image_size
        for item in raw_content:
            if not all(k in item for k in ['type', 'content', 'bbox']):
                continue

            rel_box = item['bbox']
            if not (isinstance(rel_box, list) and len(rel_box) == 4 and all(isinstance(c, int | float) for c in rel_box)):
                continue

            clean_item = {
                "type": item.get('type', 'unknown'),
                "content": item.get('content', ''),
                "location": {
                    "top_left": {"x": int(float(rel_box[0]) * img_width), "y": int(float(rel_box[1]) * img_height)},
                    "bottom_right": {"x": int(float(rel_box[2]) * img_width), "y": int(float(rel_box[3]) * img_height)}
                }
            }
            clean_content.append(clean_item)
        return clean_content
