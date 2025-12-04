# src/vision/annotator.py
import logging
from PIL import Image, ImageDraw, ImageFont
import cv2
import numpy as np
import os

from supervision.detection.core import Detections
from supervision.draw.color import Color, ColorPalette

# --- Base Class for Annotation ---

class BaseAnnotator:
    """
    A base class for annotating images, providing common font handling logic.
    """
    def __init__(self, font_path: str = "cv2_default", font_size: int = 16, text_padding: int = 2):
        self.font = self._load_font(font_path, font_size)
        self.text_padding = text_padding

    def _load_font(self, font_path: str, font_size: int) -> ImageFont.FreeTypeFont:
        """
        Loads a TTF font using Pillow, falling back to a default if not found.
        """
        if font_path and font_path != "cv2_default" and os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, font_size)
            except OSError:
                logging.warning(f"Font at '{font_path}' not found or failed to load. Falling back to default Pillow font.")
        return ImageFont.load_default()

# --- Annotator for Detection Boxes ---

class BoxAnnotator(BaseAnnotator):
    """
    Annotates detection results (bounding boxes and labels) on an image.
    Inherits font handling from BaseAnnotator and uses Pillow for all drawing.
    """
    def __init__(
        self,
        color: Color | ColorPalette = ColorPalette.DEFAULT,
        thickness: int = 3,
        text_color: Color = Color.BLACK,
        font_path: str = "cv2_default",
        font_size: int = 16,
        text_padding: int = 2,
    ):
        super().__init__(font_path, font_size, text_padding)
        self.color: Color | ColorPalette = color
        self.thickness: int = thickness
        self.text_color: Color = text_color

    def annotate(
        self,
        scene: np.ndarray,
        detections: Detections,
        labels: list[str] | None = None,
        skip_label: bool = False,
        image_size: tuple[int, int] | None = None,
        custom_colors: list[tuple[int, int, int]] | None = None,
    ) -> np.ndarray:
        """
        Draws bounding boxes on the frame using Pillow.
        """
        scene_pil = Image.fromarray(cv2.cvtColor(scene, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(scene_pil)

        for i in range(len(detections)):
            x1, y1, x2, y2 = detections.xyxy[i].astype(int)
            class_id = detections.class_id[i] if detections.class_id is not None else None
            idx = class_id if class_id is not None else i

            color_rgb = custom_colors[i][::-1] if custom_colors and i < len(custom_colors) else self.color.by_idx(idx).as_rgb()

            draw.rectangle([(x1, y1), (x2, y2)], outline=color_rgb, width=self.thickness)
            if skip_label:
                continue

            text = f"{class_id}" if (labels is None or len(detections) != len(labels)) else labels[i]

            try:
                bbox = draw.textbbox((0, 0), text, font=self.font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
            except AttributeError:
                text_width, text_height = draw.textsize(text, font=self.font)

            bg_x1, bg_y1, bg_x2, bg_y2 = self._get_optimal_label_pos(text_width, text_height, x1, y1, x2, y2, detections, image_size)

            text_x = bg_x1 + self.text_padding
            text_y = bg_y1 + self.text_padding

            draw.rectangle([(bg_x1, bg_y1), (bg_x2, bg_y2)], fill=color_rgb)

            luminance = 0.299 * color_rgb[0] + 0.587 * color_rgb[1] + 0.114 * color_rgb[2]
            text_color_rgb = (0, 0, 0) if luminance > 160 else (255, 255, 255)

            draw.text((text_x, text_y), text, font=self.font, fill=text_color_rgb)

        return cv2.cvtColor(np.array(scene_pil), cv2.COLOR_RGB2BGR)

    def _get_optimal_label_pos(self, text_width, text_height, x1, y1, x2, y2, detections, image_size):
        """
        Checks for overlap of the text background with other detection boxes and finds an optimal position.
        """
        padding = self.text_padding
        # Top-left (default)
        bg = [x1, y1 - 2 * padding - text_height, x1 + 2 * padding + text_width, y1]
        if not self._is_overlap(detections, bg, image_size):
            return bg

        # Top-right
        bg = [x2 - 2 * padding - text_width, y1 - 2 * padding - text_height, x2, y1]
        if not self._is_overlap(detections, bg, image_size):
            return bg

        # Fallback to top-left
        return [x1, y1 - 2 * padding - text_height, x1 + 2 * padding + text_width, y1]

    def _is_overlap(self, detections, bg_box, image_size):
        if image_size is None:
            return False
        if bg_box[0] < 0 or bg_box[2] > image_size[0] or bg_box[1] < 0 or bg_box[3] > image_size[1]:
            return True
        for i in range(len(detections)):
            if self._IoU(bg_box, detections.xyxy[i].astype(int)) > 0.3:
                return True
        return False

    def _IoU(self, box1, box2):
        x1 = max(box1[0], box2[0])
        y1 = max(box1[1], box2[1])
        x2 = min(box1[2], box2[2])
        y2 = min(box1[3], box2[3])
        intersection = max(0, x2 - x1) * max(0, y2 - y1)

        box1_area = (box1[2] - box1[0]) * (box1[3] - box1[1])
        if box1_area > 0:
            return intersection / box1_area
        return 0

# --- Annotator for LLM Intent ---

class IntentAnnotator(BaseAnnotator):
    """
    Draws the intended LLM action on a screenshot.
    Inherits font handling from BaseAnnotator.
    """
    def __init__(self, font_path: str = "cv2_default", font_size: int = 18):
        super().__init__(font_path, font_size)
        # Configuration
        self.CLICK_FILL_COLOR = (255, 0, 0, 80)
        self.CLICK_OUTLINE_COLOR = (255, 0, 0, 180)
        self.DRAG_FILL_COLOR = (128, 0, 128, 80)   # Semi-transparent purple
        self.DRAG_OUTLINE_COLOR = (128, 0, 128, 180)
        self.DROP_FILL_COLOR = (255, 165, 0, 80)  # Semi-transparent orange
        self.DROP_OUTLINE_COLOR = (255, 165, 0, 180)
        self.LIST_BG_COLOR = (0, 0, 0, 150) # Semi-transparent black for the list background
        self.TEXT_COLOR = (255, 255, 255)
        self.LINE_HEIGHT = self.font.getbbox("A")[3] + 10 # Add some padding between lines

    def annotate(self, image_np: np.ndarray, tool_calls: list, base_position: tuple[int, int] | None = None) -> np.ndarray:
        """
        Draws all intended actions for a task on a screenshot.
        - Draws visual indicators (like boxes) for spatial actions (e.g., click).
        - Draws a text list of all actions, positioned near the base_position or top-left.
        """
        image_pil = Image.fromarray(image_np).convert('RGBA')
        overlay = Image.new('RGBA', image_pil.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        action_descriptions = []

        try:
            # First pass: Draw spatial elements and collect text descriptions
            for tool_call in tool_calls:
                action_name = tool_call.name
                args = tool_call.arguments

                if action_name == 'click':
                    box = [args['x1'], args['y1'], args['x2'], args['y2']]
                    center_x = (args['x1'] + args['x2']) / 2
                    center_y = (args['y1'] + args['y2']) / 2
                    self._draw_click(draw, box, center_x, center_y)
                elif action_name == 'drag_and_drop':
                    start_box = [args['x1_start'], args['y1_start'], args['x2_start'], args['y2_start']]
                    end_box = [args['x1_end'], args['y1_end'], args['x2_end'], args['y2_end']]
                    self._draw_drag_area(draw, start_box)
                    self._draw_drop_area(draw, end_box)

                action_descriptions.append(self._get_action_description(tool_call))

            # Second pass: Draw the list of text descriptions
            if action_descriptions:
                self._draw_action_list(draw, action_descriptions, image_pil.size, base_position)

        except Exception as e:
            logging.error(f"Error drawing intent for tool calls: {e}")

        composited_image = Image.alpha_composite(image_pil, overlay)
        return np.array(composited_image.convert('RGB'))

    def _get_action_description(self, tool_call) -> str:
        """Generates a concise, human-readable string for a single tool call."""
        name = tool_call.name
        args = tool_call.arguments

        if name == 'click':
            return "Click"
        elif name == 'drag_and_drop':
            return "Drag & Drop"
        elif name == 'write_text':
            text = args.get('text', '')
            display_text = text if len(text) < 20 else text[:17] + "..."
            return f"Type: '{display_text}'"
        elif name == 'press_key':
            return f"Press: {args.get('key')}"
        elif name == 'hotkey':
            keys = "+".join(args.get('keys', []))
            return f"Hotkey: {keys}"
        elif name == 'launch_app':
            return f"Launch: {args.get('app_name')}"
        else:
            # Generic fallback for other actions
            return f"Action: {name}"

    def _draw_action_list(self, draw, descriptions: list, image_size: tuple[int, int], base_pos: tuple[int, int] | None = None):
        """Draws a list of action descriptions, adjusting position to stay on screen."""
        if not descriptions:
            return

        # Determine the size of the background box
        max_width = 0
        for text in descriptions:
            try:
                bbox = draw.textbbox((0, 0), text, font=self.font)
                text_width = bbox[2] - bbox[0]
                if text_width > max_width:
                    max_width = text_width
            except AttributeError: # Fallback for older Pillow versions
                text_width, _ = draw.textsize(text, font=self.font)
                if text_width > max_width:
                    max_width = text_width

        box_width = max_width + 20  # 10px padding on each side
        box_height = (len(descriptions) * self.LINE_HEIGHT) + 10 # 5px padding top/bottom

        # Determine the position of the box
        if base_pos:
            box_x = base_pos[0] + 15  # Start slightly to the right of the click
            box_y = base_pos[1] + 15  # Start slightly below the click
        else:
            box_x, box_y = 10, 10 # Default to top-left

        # Adjust position to keep the box on screen
        if box_x + box_width > image_size[0]:
            box_x = base_pos[0] - box_width - 15 if base_pos else image_size[0] - box_width - 10
        if box_y + box_height > image_size[1]:
            box_y = base_pos[1] - box_height - 15 if base_pos else image_size[1] - box_height - 10

        # Ensure it doesn't go off the top-left either
        box_x = max(10, box_x)
        box_y = max(10, box_y)

        # Draw the semi-transparent background
        draw.rectangle([box_x, box_y, box_x + box_width, box_y + box_height], fill=self.LIST_BG_COLOR)

        # Draw each line of text
        current_y = box_y + 5
        for text in descriptions:
            draw.text((box_x + 10, current_y), text, fill=self.TEXT_COLOR, font=self.font)
            current_y += self.LINE_HEIGHT

    def _draw_click(self, draw, box, x, y):
        draw.rectangle(box, fill=self.CLICK_FILL_COLOR, outline=self.CLICK_OUTLINE_COLOR, width=2)
        radius = 5
        draw.ellipse([(x - radius, y - radius), (x + radius, y + radius)], fill=self.CLICK_OUTLINE_COLOR)

    def _draw_label_with_background(self, draw, text, position):
        """Draws a text label with a semi-transparent black background, centered at the given position."""
        try:
            bbox = draw.textbbox((0, 0), text, font=self.font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
        except AttributeError:
            text_width, text_height = draw.textsize(text, font=self.font)

        padding = 5
        bg_width = text_width + padding * 2
        bg_height = text_height + padding * 2

        bg_x1 = position[0] - bg_width / 2
        bg_y1 = position[1] - bg_height / 2
        bg_x2 = position[0] + bg_width / 2
        bg_y2 = position[1] + bg_height / 2

        draw.rectangle([bg_x1, bg_y1, bg_x2, bg_y2], fill=self.LIST_BG_COLOR)
        draw.text((bg_x1 + padding, bg_y1 + padding), text, fill=self.TEXT_COLOR, font=self.font)

    def _draw_drag_area(self, draw, box):
        """Draws a purple box and a 'Drag' label at its center."""
        draw.rectangle(box, fill=self.DRAG_FILL_COLOR, outline=self.DRAG_OUTLINE_COLOR, width=2)
        center_x = (box[0] + box[2]) / 2
        center_y = (box[1] + box[3]) / 2
        self._draw_label_with_background(draw, "Drag", (center_x, center_y))

    def _draw_drop_area(self, draw, box):
        """Draws an orange box and a 'Drop' label at its center."""
        draw.rectangle(box, fill=self.DROP_FILL_COLOR, outline=self.DROP_OUTLINE_COLOR, width=2)
        center_x = (box[0] + box[2]) / 2
        center_y = (box[1] + box[3]) / 2
        self._draw_label_with_background(draw, "Drop", (center_x, center_y))

