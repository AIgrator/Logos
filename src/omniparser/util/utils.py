# from ultralytics import YOLO
import logging
import io
import base64
import time
import sys
from contextlib import redirect_stdout
from PIL import Image
import cv2
import numpy as np
# %matplotlib inline
from matplotlib import pyplot as plt
import easyocr
import torch
from collections.abc import Callable
from torchvision.transforms import ToPILImage
import supervision as sv
from vision.annotator import BoxAnnotator
import pandas as pd
from sklearn.cluster import DBSCAN
import math
import colorsys

# --- START PROGRESS HOOK CONFIGURATION ---

class RedirectStream:
    """
    A stream-like object to redirect stdout, parse EasyOCR download status,
    and forward the original output to the console.
    """
    def __init__(self, status_callback: Callable[[str], None], original_stdout):
        self.status_callback = status_callback
        self.original_stdout = original_stdout
        # Buffer to hold partial lines for status detection
        self.buffer = ""

    def write(self, text):
        """
        This method is called for every print/write to stdout.
        It writes to the original stdout and checks for status changes.
        """
        # Forward the output to the original console
        self.original_stdout.write(text)
        self.original_stdout.flush()

        # Accumulate text for line detection
        self.buffer += text

        # Split into lines and process each
        lines = self.buffer.split('\n')
        self.buffer = lines[-1]  # Keep the last incomplete line as buffer

        for line in lines[:-1]:  # Process complete lines
            line = line.strip()
            if "Downloading" in line:
                if self.status_callback:
                    self.status_callback('downloading')
            elif "Download complete" in line:
                if self.status_callback:
                    self.status_callback('complete')

    def flush(self):
        # This stream does not need to be flushed, but we flush the original.
        self.original_stdout.flush()

# --- END PROGRESS HOOK CONFIGURATION ---


# --- START LANGUAGE CONFIGURATION ---
# Global cache for OCR readers to avoid re-initializing them
_ocr_readers_cache = {}

def get_ocr_reader(lang_list=None, status_callback: Callable[[str], None] = None):
    if lang_list is None:
        lang_list = ['en']
    """
    Initializes and caches an EasyOCR.Reader for a given list of languages.
        Uses a sorted tuple of languages as the cache key to ensure consistency.

        Args:
        lang_list (list): A list of language codes to be recognized by EasyOCR.
        status_callback (Callable[[str], None], optional): A callback function
            that receives status updates ('downloading' or 'complete').
    """
    # Create a consistent key for the cache, e.g., ('en', 'ru')
    use_gpu = torch.cuda.is_available()
    cache_key = (tuple(sorted(lang_list)), use_gpu)

    if cache_key not in _ocr_readers_cache:
        logging.info(f"Initializing EasyOCR reader for languages: {lang_list} on {'GPU' if use_gpu else 'CPU'}...")

        # If a status callback is provided, redirect stdout to capture status
        if status_callback:
            original_stdout = sys.stdout
            redirect_stream = RedirectStream(status_callback, original_stdout)
            with redirect_stdout(redirect_stream):
                try:
                    _ocr_readers_cache[cache_key] = easyocr.Reader(
                        lang_list, gpu=use_gpu, verbose=True, model_storage_directory='models/easyocr/'
                    )
                except Exception as e:
                    # Ensure errors during initialization are visible
                    logging.error(f"Failed to initialize EasyOCR Reader: {e}", exc_info=True)
                    original_stdout.write(f"\nError initializing EasyOCR: {e}\n")
                    raise
        else:
            # Initialize normally without redirection
            _ocr_readers_cache[cache_key] = easyocr.Reader(
                lang_list, gpu=use_gpu, verbose=False, model_storage_directory='models/easyocr/'
            )

        logging.info("Reader initialized.")

    return _ocr_readers_cache[cache_key]

# --- END LANGUAGE CONFIGURATION ---

# START: Classes and functions from GUI-Perceptual-Grouping
class ComposDF:
    def __init__(self, elements):
        self.compos_dataframe = self.load_compos_from_elements(elements)
        if 'class_origin' in self.compos_dataframe.columns:
            self.compos_dataframe['class'] = self.compos_dataframe['class_origin']
        self.compos_dataframe['id'] = list(range(len(self.compos_dataframe)))
        self.compos_dataframe.set_index('id', inplace=True)

        required_cols = {'column_max', 'column_min', 'row_max', 'row_min'}
        if required_cols.issubset(self.compos_dataframe.columns):
            self.compos_dataframe['width'] = self.compos_dataframe['column_max'] - self.compos_dataframe['column_min']
            self.compos_dataframe['height'] = self.compos_dataframe['row_max'] - self.compos_dataframe['row_min']
            self.compos_dataframe['area'] = self.compos_dataframe['width'] * self.compos_dataframe['height']
            self.compos_dataframe['center_column'] = (self.compos_dataframe['column_max'] + self.compos_dataframe['column_min']) / 2
            self.compos_dataframe['center_row'] = (self.compos_dataframe['row_max'] + self.compos_dataframe['row_min']) / 2

        self.compos_dataframe['parent'] = -1
        self.compos_dataframe['children'] = [[] for i in range(len(self.compos_dataframe))]
        self.compos_dataframe['group'] = -1
        self.compos_dataframe['pair_to'] = -1
        self.compos_dataframe['list_item'] = -1
        self.compos_dataframe['alignment_in_group'] = -1
        self.compos_dataframe['alignment_in_group'] = self.compos_dataframe['alignment_in_group'].astype(object)
        self.compos_dataframe['group_pair'] = -1

    def load_compos_from_elements(self, elements):
        compos = []
        for i, element in enumerate(elements):
            compo = {
                'id': i,
                'class': 'Text' if element['type'] == 'text' else 'Compo',
                'column_min': element['bbox'][0],
                'row_min': element['bbox'][1],
                'column_max': element['bbox'][2],
                'row_max': element['bbox'][3],
                'text_content': element.get('content', None)
            }
            compos.append(compo)
        return pd.DataFrame(compos)

    def select_by_class(self, classes, no_parent=False, replace=False):
        df = self.compos_dataframe
        df = df.loc[df['class'].isin(classes)]
        if no_parent:
            df = df.loc[df['parent'] == -1]
        if replace:
            self.compos_dataframe = df
        return df

    def cluster_dbscan_by_attr(self, attr, eps, min_samples=1):
        df = self.compos_dataframe
        if len(df) == 0:
            return
        cluster_name = 'cluster_' + attr
        X = np.array(df[attr]).reshape(-1, 1)
        clustering = DBSCAN(eps=eps, min_samples=min_samples).fit(X)
        df[cluster_name] = clustering.labels_

    def cluster_area_by_relational_size(self):
        df = self.compos_dataframe
        if len(df) == 0:
            return
        cluster_name = 'cluster_area'
        df[cluster_name] = -1
        cluster_id = 0
        for i in range(len(df)):
            compo_a = df.iloc[i]
            if compo_a[cluster_name] != -1:
                continue
            df.loc[compo_a.name, cluster_name] = cluster_id
            for j in range(i + 1, len(df)):
                compo_b = df.iloc[j]
                if compo_b[cluster_name] != -1:
                    continue
                area_a, area_b = compo_a.area, compo_b.area
                if area_a == 0 or area_b == 0:
                    continue
                if max(area_a, area_b) / min(area_a, area_b) < 1.5:
                    df.loc[compo_b.name, cluster_name] = cluster_id
            cluster_id += 1

    def group_by_clusters(self, cluster, alignment, new_groups=True):
        df = self.compos_dataframe
        if new_groups:
            df['group'] = -1
            df['alignment_in_group'] = -1
            df['alignment_in_group'] = df['alignment_in_group'].astype(object)
        groups = df.groupby(cluster).groups
        group_id = 0
        for i in groups:
            if i == -1 or len(groups[i]) == 1:
                continue
            df.loc[list(groups[i]), 'group'] = group_id
            df.loc[list(groups[i]), 'alignment_in_group'] = alignment
            group_id += 1

    def group_by_clusters_conflict(self, cluster, alignment):
        df = self.compos_dataframe
        groups = df.groupby(cluster).groups
        max_group_id = df['group'].max() + 1
        for i in groups:
            if i == -1:
                continue
            if df.loc[list(groups[i])]['group'].nunique() == 1 and df.loc[list(groups[i])]['group'].unique()[0] == -1:
                df.loc[list(groups[i]), 'group'] = max_group_id
                df.loc[list(groups[i]), 'alignment_in_group'] = alignment
                max_group_id += 1

    def check_group_of_two_compos_validity_by_areas(self):
        df = self.compos_dataframe
        groups = df.groupby('group').groups
        for i in groups:
            if i == -1 or len(groups[i]) != 2:
                continue
            compos_in_group = df.loc[list(groups[i])]
            area_a, area_b = compos_in_group.iloc[0].area, compos_in_group.iloc[1].area
            if area_a == 0 or area_b == 0:
                continue
            if max(area_a, area_b) / min(area_a, area_b) > 1.5:
                df.loc[list(groups[i]), 'group'] = -1
                df.loc[list(groups[i]), 'alignment_in_group'] = -1

def calc_angle(c1, c2, anchor='center'):
    angle = int(math.degrees(math.atan2(c1['center_row'] - c2['center_row'], c1['center_column'] - c2['center_column'])))
    if angle < 0:
        angle += 180
    if angle > 90:
        angle -= 180
    return angle

def calc_compos_y_distance(compo1, compo2):
    if max(compo1['row_min'], compo2['row_min']) < min(compo1['row_max'], compo2['row_max']):
        return -1
    return compo2['row_min'] - compo1['row_max'] if compo1['row_min'] < compo2['row_min'] else compo1['row_min'] - compo2['row_max']

def match_two_groups_by_angles_and_y_distance(g1, g2, diff_distance=1.2, diff_angle=10, match_thresh=0.7):
    alignment = g1.iloc[0]['alignment_in_group']
    g1_sort = g1.sort_values('center_column') if alignment == 'h' else g1.sort_values('center_row')
    g2_sort = g2.sort_values('center_column') if alignment == 'h' else g2.sort_values('center_row')

    if len(g1) != len(g2):
        return False

    angles_cen = [calc_angle(g1_sort.iloc[i], g2_sort.iloc[i], 'center') for i in range(len(g1_sort))]
    distances = [calc_compos_y_distance(g1_sort.iloc[i], g2_sort.iloc[i]) for i in range(len(g1_sort))]

    dist_match = sum(1 for i, d1 in enumerate(distances) if any(abs(d1 - d2) < 10 or max(d1, d2) < diff_distance * min(d1, d2) for j, d2 in enumerate(distances) if i != j))
    angle_match = sum(1 for i, a1 in enumerate(angles_cen) if any(abs(a1 - a2) < diff_angle for j, a2 in enumerate(angles_cen) if i != j))

    if dist_match >= len(distances) * match_thresh and angle_match >= len(angles_cen) * match_thresh:
        for i in range(len(g1_sort)):
            g1.loc[g1_sort.iloc[i].name, 'pair_to'] = g2_sort.iloc[i].name
            g2.loc[g2_sort.iloc[i].name, 'pair_to'] = g1_sort.iloc[i].name
        return True
    return False

def pair_matching_within_groups(groups_df):
    groups = [groups_df.loc[ids] for _, ids in groups_df.groupby('group').groups.items() if _ != -1]
    pair_id_counter = 0

    for i, g1 in enumerate(groups):
        for j in range(i + 1, len(groups)):
            g2 = groups[j]
            if g1.iloc[0]['alignment_in_group'] == g2.iloc[0]['alignment_in_group']:
                if match_two_groups_by_angles_and_y_distance(g1, g2):
                    p1_id = groups_df.loc[g1.index, 'group_pair'].unique()[0]
                    p2_id = groups_df.loc[g2.index, 'group_pair'].unique()[0]

                    if p1_id == -1 and p2_id == -1:
                        pair_id_counter += 1
                        groups_df.loc[g1.index, 'group_pair'] = pair_id_counter
                        groups_df.loc[g2.index, 'group_pair'] = pair_id_counter
                    elif p1_id != -1 and p2_id == -1:
                        groups_df.loc[g2.index, 'group_pair'] = p1_id
                    elif p1_id == -1 and p2_id != -1:
                        groups_df.loc[g1.index, 'group_pair'] = p2_id
                    elif p1_id != -1 and p2_id != -1 and p1_id != p2_id:
                        groups_df.loc[groups_df['group_pair'] == p2_id, 'group_pair'] = p1_id
    return groups_df

def recog_repetition_nontext(compos_df):
    compos_cp = ComposDF(elements=[])
    compos_cp.compos_dataframe = compos_df.copy()
    compos_cp.select_by_class(['Compo'], no_parent=False, replace=True)
    if len(compos_cp.compos_dataframe) == 0:
        return compos_cp.compos_dataframe

    compos_cp.cluster_dbscan_by_attr('center_column', eps=15)
    compos_cp.cluster_dbscan_by_attr('center_row', eps=15)
    compos_cp.cluster_area_by_relational_size()
    compos_cp.group_by_clusters(cluster=['cluster_area', 'cluster_center_column'], alignment='v')
    compos_cp.check_group_of_two_compos_validity_by_areas()
    compos_cp.group_by_clusters_conflict(cluster=['cluster_area', 'cluster_center_row'], alignment='h')
    compos_cp.check_group_of_two_compos_validity_by_areas()
    compos_cp.compos_dataframe.rename({'group': 'group_nontext'}, axis=1, inplace=True)
    return compos_cp.compos_dataframe

def group_elements(elements):

    def is_bbox_inside(bbox1, bbox2):
        """Check if bbox1 is inside bbox2."""
        return (bbox1[0] >= bbox2[0] and
                bbox1[1] >= bbox2[1] and
                bbox1[2] <= bbox2[2] and
                bbox1[3] <= bbox2[3])

    def _run_grouping_on_subset(subset_elements):
        """Applies the full Compos_DF grouping logic on a list of elements."""
        if len(subset_elements) < 2:
            for el in subset_elements:
                el['group_id'] = -1
                el['pair_id'] = -1
            return subset_elements

        compos = ComposDF(subset_elements)
        df = compos.compos_dataframe

        # Create a deep copy of elements for text processing to avoid side effects
        text_processing_elements = [dict(el) for el in subset_elements]

        df_nontext = recog_repetition_nontext(df.copy())

        text_compos = ComposDF(text_processing_elements)
        text_compos.select_by_class(['Text'], replace=True)
        text_compos.cluster_dbscan_by_attr('row_min', 10)
        text_compos.group_by_clusters('cluster_row_min', alignment='h')
        df_text = text_compos.compos_dataframe.rename({'group': 'group_text'}, axis=1)

        df['group_nontext'] = -1
        df['alignment_in_group'] = ''
        if 'group_nontext' in df_nontext.columns:
            df.update(df_nontext[['group_nontext', 'alignment_in_group']])
        df['group_text'] = -1
        if 'group_text' in df_text.columns:
            df.update(df_text[['group_text']])

        df['group'] = df.apply(lambda row: row['group_nontext'] if row.get('group_nontext', -1) != -1 else row.get('group_text', -1), axis=1)

        df = pair_matching_within_groups(df)

        # The df index is the original index from the subset_elements list.
        # We update the original list items with the new group/pair IDs.
        for index, row in df.iterrows():
            subset_elements[index]['group_id'] = row['group']
            subset_elements[index]['pair_id'] = row['group_pair']

        return subset_elements

    containers = [el for el in elements if el.get('type') == 'container']
    other_elements = [el for el in elements if el.get('type') != 'container']

    final_elements = []
    processed_indices = set()
    next_group_id = 0

    for container in containers:
        elements_inside = []
        indices_inside = []

        for i, el in enumerate(other_elements):
            if i in processed_indices:
                continue
            if is_bbox_inside(el['bbox'], container['bbox']):
                elements_inside.append(el)
                indices_inside.append(i)

        if elements_inside:
            grouped_subset = _run_grouping_on_subset(elements_inside)

            # Remap group IDs to be globally unique
            group_id_map = {}
            for el in grouped_subset:
                old_group_id = el.get('group_id', -1)
                if old_group_id != -1:
                    if old_group_id not in group_id_map:
                        group_id_map[old_group_id] = next_group_id
                        next_group_id += 1
                    el['group_id'] = group_id_map[old_group_id]

            final_elements.extend(grouped_subset)
            processed_indices.update(indices_inside)

        # Add the container itself back to the final list
        final_elements.append(container)

    # Add remaining elements that were not in any container, with default group IDs
    for i, el in enumerate(other_elements):
        if i not in processed_indices:
            el['group_id'] = -1
            el['pair_id'] = -1
            final_elements.append(el)

    return final_elements

def _generate_group_colors(elements):
    group_colors = {}
    all_groups = set()
    for el in elements:
        if el.get('group_id') is not None and el['group_id'] != -1:
            all_groups.add(el['group_id'])
        if el.get('pair_id') is not None and el['pair_id'] != -1:
            all_groups.add(f"p_{el['pair_id']}")

    hsv_tuples = [(x * 1.0 / len(all_groups), 0.8, 0.9) for x in range(len(all_groups))]
    rgb_tuples = [tuple(int(i * 255) for i in colorsys.hsv_to_rgb(*hsv)) for hsv in hsv_tuples]

    for i, group_id in enumerate(all_groups):
        group_colors[group_id] = rgb_tuples[i]
    return group_colors

# END: Classes and functions from GUI-Perceptual-Grouping


def get_caption_model_processor(model_name, model_name_or_path="Salesforce/blip2-opt-2.7b", device=None):
    if not device:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if model_name == "blip2":
        from transformers import Blip2Processor, Blip2ForConditionalGeneration
        processor = Blip2Processor.from_pretrained("Salesforce/blip2-opt-2.7b")
        if device == 'cpu':
            model = Blip2ForConditionalGeneration.from_pretrained(
            model_name_or_path, device_map=None, torch_dtype=torch.float32
        )
        else:
            model = Blip2ForConditionalGeneration.from_pretrained(
            model_name_or_path, device_map=None, torch_dtype=torch.float16
        ).to(device)
    elif model_name == "florence2":
        from transformers import AutoProcessor, AutoModelForCausalLM
        processor = AutoProcessor.from_pretrained("microsoft/Florence-2-base", trust_remote_code=True)
        if device == 'cpu':
            model = AutoModelForCausalLM.from_pretrained(model_name_or_path, torch_dtype=torch.float32, trust_remote_code=True)
        else:
            model = AutoModelForCausalLM.from_pretrained(model_name_or_path, torch_dtype=torch.float16, trust_remote_code=True).to(device)
    return {'model': model.to(device), 'processor': processor}


def get_yolo_model(model_path):
    from ultralytics import YOLO
    # Load the model.
    model = YOLO(model_path)
    return model


@torch.inference_mode()
def get_parsed_content_icon(filtered_boxes, starting_idx, image_source, caption_model_processor, prompt=None, batch_size=128):
    to_pil = ToPILImage()
    if starting_idx != -1:
        non_ocr_boxes = filtered_boxes[starting_idx:]
    else:
        non_ocr_boxes = filtered_boxes
    croped_pil_image = []
    for _i, coord in enumerate(non_ocr_boxes):
        try:
            xmin, xmax = int(coord[0]*image_source.shape[1]), int(coord[2]*image_source.shape[1])
            ymin, ymax = int(coord[1]*image_source.shape[0]), int(coord[3]*image_source.shape[0])
            cropped_image = image_source[ymin:ymax, xmin:xmax, :]
            cropped_image = cv2.resize(cropped_image, (64, 64))
            croped_pil_image.append(to_pil(cropped_image))
        except Exception:
            continue

    model, processor = caption_model_processor['model'], caption_model_processor['processor']
    if not prompt:
        prompt = "<CAPTION>" if 'florence' in model.config.name_or_path else "The image shows"

    generated_texts = []
    device = model.device
    for i in range(0, len(croped_pil_image), batch_size):
        batch = croped_pil_image[i:i+batch_size]
        inputs = processor(images=batch, text=[prompt]*len(batch), return_tensors="pt", do_resize=False).to(device=device, dtype=torch.float16 if model.device.type == 'cuda' else torch.float32)

        if 'florence' in model.config.name_or_path:
            generated_ids = model.generate(input_ids=inputs["input_ids"],pixel_values=inputs["pixel_values"],max_new_tokens=20,num_beams=1, do_sample=False, early_stopping=False)
        else:
            generated_ids = model.generate(**inputs, max_length=100, num_beams=5, no_repeat_ngram_size=2, early_stopping=True, num_return_sequences=1)

        generated_text = [gen.strip() for gen in processor.batch_decode(generated_ids, skip_special_tokens=True)]
        generated_texts.extend(generated_text)

    return generated_texts


def remove_overlap_new(boxes, iou_threshold, ocr_bbox=None, containment_ratio=0.8):
    assert ocr_bbox is None or isinstance(ocr_bbox, list)

    def box_area(box): return (box[2] - box[0]) * (box[3] - box[1])
    def intersection_area(box1, box2):
        x1, y1, x2, y2 = max(box1[0], box2[0]), max(box1[1], box2[1]), min(box1[2], box2[2]), min(box1[3], box2[3])
        return max(0, x2 - x1) * max(0, y2 - y1)
    def is_inside(box1, box2, ratio=containment_ratio):
        intersection = intersection_area(box1, box2)
        area1 = box_area(box1)
        return intersection / area1 >= ratio if area1 > 0 else False

    final_boxes = []
    contained_texts_indices = set()

    for _i, box_elem in enumerate(boxes):
        box = box_elem['bbox']
        texts_inside = []
        texts_inside_indices = []

        if ocr_bbox:
            for j, ocr_elem in enumerate(ocr_bbox):
                if is_inside(ocr_elem['bbox'], box):
                    texts_inside.append(ocr_elem)
                    texts_inside_indices.append(j)

        # Scenario A: Container found. Mark it, populate its content, and preserve it.
        if len(texts_inside) > 1:
            box_elem['type'] = 'container'
            # Concatenate the content of all child text elements
            combined_content = " ".join([ocr['content'] for ocr in texts_inside])
            box_elem['content'] = combined_content.strip()
            final_boxes.append(box_elem)
            # The text elements inside will be added in the final loop.

        # Scenario B: Button with a single text. Merge text and mark as processed.
        elif len(texts_inside) == 1:
            box_elem['content'] = texts_inside[0]['content']
            box_elem['source'] = 'box_yolo_content_ocr'
            contained_texts_indices.add(texts_inside_indices[0])
            final_boxes.append(box_elem)

        # Scenario C: Simple icon with no text.
        else:
            box_elem['source'] = 'box_yolo_content_yolo'
            final_boxes.append(box_elem)

    # Add all text boxes that were not merged into a button.
    # This now includes text elements that were inside a container.
    if ocr_bbox:
        for i, ocr_elem in enumerate(ocr_bbox):
            if i not in contained_texts_indices:
                final_boxes.append(ocr_elem)

    return final_boxes


def get_som_labeled_img(image_source: str | Image.Image, model=None, box_threshold=0.01, output_coord_in_ratio=False, ocr_bbox=None, text_scale=0.4, text_padding=5, draw_bbox_config=None, caption_model_processor=None, ocr_text=None, use_local_semantics=True, iou_threshold=0.9,prompt=None, scale_img=False, imgsz=None, batch_size=128, settings_manager=None):
    if ocr_text is None:
        ocr_text = []
    if isinstance(image_source, str):
        image_source = Image.open(image_source)
    image_source = image_source.convert("RGB")
    w, h = image_source.size
    box_overlay_ratio = max(w, h) / 3200
    if not imgsz:
        imgsz = (h, w)

    xyxy, _, _, speed_metrics = predict_yolo(model=model, image=image_source, box_threshold=box_threshold, imgsz=imgsz, scale_img=scale_img, iou_threshold=0.1)
    xyxy = xyxy / torch.Tensor([w, h, w, h]).to(xyxy.device)
    image_np = np.asarray(image_source)

    ocr_bbox_list = (torch.tensor(ocr_bbox) / torch.Tensor([w, h, w, h])).tolist() if ocr_bbox else []
    ocr_bbox_elem = [{'type': 'text', 'bbox':box, 'interactivity':False, 'content':txt, 'source': 'box_ocr_content_ocr'} for box, txt in zip(ocr_bbox_list, ocr_text, strict=False) if int_box_area(box, w, h) > 0]
    xyxy_elem = [{'type': 'icon', 'bbox':box, 'interactivity':True, 'content':None} for box in xyxy.tolist() if int_box_area(box, w, h) > 0]

    # Get dynamic containment ratio from settings
    containment_ratio = 0.8
    if settings_manager:
        omniparser_settings = settings_manager.get('omniparser', {})
        containment_ratio = omniparser_settings.get('containment_ratio', 0.8)

    refined_elements = remove_overlap_new(
        boxes=xyxy_elem,
        iou_threshold=iou_threshold,
        ocr_bbox=ocr_bbox_elem,
        containment_ratio=containment_ratio
    )

    import logging
    #logging.info(f"Refined elements before grouping: {refined_elements}")
    try:
        grouped_elements = group_elements(refined_elements)
        #logging.info(f"Grouped elements after grouping: {grouped_elements}")
    except Exception as e:
        logging.error(f"Error during grouping: {e}", exc_info=True)
        grouped_elements = [] # Return empty list on error

    filtered_boxes_elem = sorted(grouped_elements, key=lambda x: x['content'] is None)
    starting_idx = next((i for i, box in enumerate(filtered_boxes_elem) if box['content'] is None), -1)

    if use_local_semantics and starting_idx != -1:
        filtered_boxes_coords = torch.tensor([box['bbox'] for box in filtered_boxes_elem])
        parsed_content_icon = get_parsed_content_icon(filtered_boxes_coords, starting_idx, image_np, caption_model_processor, prompt=prompt, batch_size=batch_size)

        icon_content_idx = 0
        for box in filtered_boxes_elem:
            if box['content'] is None:
                if icon_content_idx < len(parsed_content_icon):
                    box['content'] = parsed_content_icon[icon_content_idx]
                    icon_content_idx += 1

    annotated_frame = image_np.copy()
    label_coordinates = {}

    if settings_manager:
        omniparser_settings = settings_manager.get('omniparser', {})
        font_path = omniparser_settings.get('font_path', 'cv2_default')
        font_size = omniparser_settings.get('font_size', 16)
        text_padding = omniparser_settings.get('text_padding', 2)
        draw_bounding_boxes = omniparser_settings.get("save_post_screenshot", True)
    else:
        draw_bounding_boxes = True
        font_path = 'cv2_default'
        font_size = 16
        text_padding = 2

    if draw_bounding_boxes:
        box_annotator = BoxAnnotator(
            thickness=max(int(2 * box_overlay_ratio), 1),
            font_path=font_path,
            font_size=font_size,
            text_padding=text_padding
        )

        # Prepare detections for supervision
        boxes_for_sv = []
        for element in filtered_boxes_elem:
            box = element['bbox']
            p1 = (int(box[0] * w), int(box[1] * h))
            p2 = (int(box[2] * w), int(box[3] * h))
            boxes_for_sv.append([p1[0], p1[1], p2[0], p2[1]])

        if boxes_for_sv:
            detections = sv.Detections(xyxy=np.array(boxes_for_sv))

            # Use descriptive content for labels, not just numbers
            labels = [f"{elem.get('content', '') or elem.get('type', 'ui')}" for elem in filtered_boxes_elem]

            annotated_frame = box_annotator.annotate(
                scene=annotated_frame,
                detections=detections,
                labels=labels,
                # Let the annotator use its default color palette for variety
                custom_colors=None,
                image_size=(w, h)
            )

    # Populate label_coordinates regardless of drawing
    for i, element in enumerate(filtered_boxes_elem):
        box = element['bbox']
        p1 = (int(box[0] * w), int(box[1] * h))
        p2 = (int(box[2] * w), int(box[3] * h))
        label_coordinates[str(i)] = [p1[0], p1[1], p2[0] - p1[0], p2[1] - p1[1]]

    pil_img = Image.fromarray(annotated_frame)
    buffered = io.BytesIO()
    pil_img.save(buffered, format="PNG")
    encoded_image = base64.b64encode(buffered.getvalue()).decode('ascii')

    if output_coord_in_ratio:
        for k, v in label_coordinates.items():
            label_coordinates[k] = [v[0]/w, v[1]/h, v[2]/w, v[3]/h]

    return encoded_image, annotated_frame, filtered_boxes_elem, speed_metrics


def int_box_area(box, w, h):
    x1, y1, x2, y2 = box
    int_box = [int(x1*w), int(y1*h), int(x2*w), int(y2*h)]
    return (int_box[2] - int_box[0]) * (int_box[3] - int_box[1])

def predict_yolo(model, image, box_threshold, imgsz, scale_img, iou_threshold=0.7):
    if scale_img:
        result = model.predict(source=image, conf=box_threshold, imgsz=imgsz, iou=iou_threshold, verbose=False)
    else:
        result = model.predict(source=image, conf=box_threshold, iou=iou_threshold, verbose=False)

    # Extract performance metrics from the result object
    speed_metrics = result[0].speed  # e.g., {'preprocess': 7.6, 'inference': 35.7, 'postprocess': 2.0}

    return result[0].boxes.xyxy, result[0].boxes.conf, [str(i) for i in range(len(result[0].boxes.xyxy))], speed_metrics

def get_xywh(input):
    x, y, w, h = input[0][0], input[0][1], input[2][0] - input[0][0], input[2][1] - input[0][1]
    x, y, w, h = int(x), int(y), int(w), int(h)
    return x, y, w, h

def get_xyxy(input):
    x, y, xp, yp = input[0][0], input[0][1], input[2][0], input[2][1]
    x, y, xp, yp = int(x), int(y), int(xp), int(yp)
    return x, y, xp, yp

def get_xywh_yolo(input):
    x, y, w, h = input[0], input[1], input[2] - input[0], input[3] - input[1]
    x, y, w, h = int(x), int(y), int(w), int(h)
    return x, y, w, h

def check_ocr_box(image_source: str | Image.Image, display_img = True, output_bb_format='xywh', goal_filtering=None, easyocr_args=None):
    if isinstance(image_source, str):
        image_source = Image.open(image_source)
    if image_source.mode == 'RGBA':
        # Convert RGBA to RGB to avoid alpha channel issues
        image_source = image_source.convert('RGB')
    image_np = np.array(image_source)
    w, h = image_source.size
    # --- START LANGUAGE CHANGE ---
    if easyocr_args is None:
        easyocr_args = {}

    # Get language list from args, default to ['en']
    lang_list = easyocr_args.pop('lang_list', ['en'])
    reader = get_ocr_reader(lang_list)

    start_time = time.time()
    result = reader.readtext(image_np, **easyocr_args)
    end_time = time.time()
    logging.info(f"EasyOCR readtext took {(end_time - start_time) * 1000:.1f}ms")
    # --- END LANGUAGE CHANGE ---
    coord = [item[0] for item in result]
    text = [item[1] for item in result]
    if display_img:
        opencv_img = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)
        bb = []
        for item in coord:
            x, y, a, b = get_xywh(item)
            bb.append((x, y, a, b))
            cv2.rectangle(opencv_img, (x, y), (x+a, y+b), (0, 255, 0), 2)
        #  matplotlib expects RGB
        plt.imshow(cv2.cvtColor(opencv_img, cv2.COLOR_BGR2RGB))
    else:
        if output_bb_format == 'xywh':
            bb = [get_xywh(item) for item in coord]
        elif output_bb_format == 'xyxy':
            bb = [get_xyxy(item) for item in coord]
    return (text, bb), goal_filtering
