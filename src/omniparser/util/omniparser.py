from .utils import get_som_labeled_img, get_caption_model_processor, get_yolo_model, check_ocr_box
import torch
from PIL import Image
import io
import base64
import logging

class Omniparser:
    def __init__(self, config: dict):
        self.config = config
        self.ocr_languages = config.get('ocr_languages', ['en'])  # Store languages from config
        device = 'cuda' if torch.cuda.is_available() else 'cpu'

        self.som_model = get_yolo_model(model_path=config['som_model_path'])
        self.caption_model_processor = get_caption_model_processor(model_name=config['caption_model_name'], model_name_or_path=config['caption_model_path'], device=device)
        logging.info('Omniparser initialized.')

    def update_languages(self, lang_codes: list, status_callback=None):
        """
        Updates the OCR languages and re-initializes the EasyOCR reader.
        This will trigger a download for any new, uncached languages.
        """
        from .utils import get_ocr_reader
        logging.info(f"Updating OCR languages to: {lang_codes}")
        self.ocr_languages = lang_codes

        # The single, correct call to initialize the reader with all specified languages.
        get_ocr_reader(lang_list=self.ocr_languages, status_callback=status_callback)
        logging.info("OCR language update complete.")

    def parse(self, image_base64: str, settings_manager=None):
        image_bytes = base64.b64decode(image_base64)
        image = Image.open(io.BytesIO(image_bytes))
        #ogging.debug(f'OmniParser: processing image size: {image.size}')

        box_overlay_ratio = max(image.size) / 3200
        draw_bbox_config = {
            'text_scale': 0.8 * box_overlay_ratio,
            'text_thickness': max(int(2 * box_overlay_ratio), 1),
            'text_padding': max(int(3 * box_overlay_ratio), 1),
            'thickness': max(int(3 * box_overlay_ratio), 1),
        }

        # Get dynamic settings from the manager if available
        text_threshold = 0.8 # Default value
        if settings_manager:
            omniparser_settings = settings_manager.get('omniparser', {})
            text_threshold = omniparser_settings.get('text_threshold', 0.8)

        # Pass the configured languages to the OCR function
        easyocr_args = {
            'text_threshold': text_threshold,
            'lang_list': self.ocr_languages
        }
        (text, ocr_bbox), _ = check_ocr_box(image, display_img=False, output_bb_format='xyxy', easyocr_args=easyocr_args)
        dino_labled_img, annotated_frame, parsed_content_list, speed_metrics = get_som_labeled_img(
            image,
            self.som_model,
            box_threshold=self.config['box_threshold'],
            output_coord_in_ratio=True,
            ocr_bbox=ocr_bbox,
            draw_bbox_config=draw_bbox_config,
            caption_model_processor=self.caption_model_processor,
            ocr_text=text,
            use_local_semantics=True,
            iou_threshold=0.7,
            scale_img=False,
            batch_size=128,
            settings_manager=settings_manager
        )

        # Format and log the performance metrics
        total_time = speed_metrics.get('preprocess', 0) + speed_metrics.get('inference', 0) + speed_metrics.get('postprocess', 0)
        logging.info(
            f"Found {len(parsed_content_list)} elements in {total_time:.1f}ms "
            f"(preprocess: {speed_metrics.get('preprocess', 0):.1f}ms, "
            f"inference: {speed_metrics.get('inference', 0):.1f}ms, "
            f"postprocess: {speed_metrics.get('postprocess', 0):.1f}ms) "
            f"{image.size[0]}x{image.size[1]}"
        )

        return dino_labled_img, annotated_frame, parsed_content_list
