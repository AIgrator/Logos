# src/llm_parsing.py
"""
Centralized utilities for validating and parsing LLM responses using Pydantic.
"""
import logging
import re
from json_repair import repair_json
from pydantic import ValidationError
from typing import Any
from difflib import SequenceMatcher

# Import the Pydantic models and the schema map
from src.computer.schemas.vision import SCHEMA_MAP as VISUAL_SCHEMA_MAP, Evaluation
from src.computer.schemas.terminal import SCHEMA_MAP as TERMINAL_SCHEMA_MAP, ToolCall as TerminalToolCall
from src.computer.schemas.browser import SCHEMA_MAP as BROWSER_SCHEMA_MAP

# Combine both schema maps into one for the validator to use
SCHEMA_MAP = {**VISUAL_SCHEMA_MAP, **TERMINAL_SCHEMA_MAP, **BROWSER_SCHEMA_MAP}

# --- Constants for Fuzzy Matching ---
SIMILARITY_THRESHOLD = 0.8
# Dynamically get valid enum values from the Pydantic schema itself
VALID_STATUSES = list(Evaluation.model_fields['payload'].annotation.model_fields['evaluation'].annotation.model_fields['status'].annotation.__args__)
VALID_NEXT_MOVE_TYPES = list(Evaluation.model_fields['payload'].annotation.model_fields['next_move'].annotation.model_fields['type'].annotation.__args__)

# --- Constants for XML-like response parsing ---

# Hardcoded list of vision tool names.
# TODO: Refactor to a central tool registry to avoid hardcoding and allow for schema-aware parsing.
VISION_TOOL_NAMES = [
    'click', 'double_click', 'drag_and_drop', 'write_text', 'press_key', 'hotkey',
    'key_down', 'key_up', 'scroll', 'scroll_page', 'launch_app', 'show_warning',
    'wait_element', 'get_clipboard_content'
]

# Dynamically get valid tool names from the Pydantic schema for terminal mode
TERMINAL_TOOL_NAMES = list(TerminalToolCall.model_fields['name'].annotation.__args__)

# Create a unified set of all known tool names from both modes
VALID_TOOL_NAMES = set(TERMINAL_TOOL_NAMES + VISION_TOOL_NAMES)

TOOL_NAME_SYNONYMS = ['name', 'action', 'tool_name', 'function_name', 'tool', 'command']
ARGUMENTS_SYNONYMS = ['arguments', 'args', 'parameters', 'params', 'payload', 'inputs']


def decode_unicode_escapes(obj):
    """
    Recursively traverses a dictionary or list and decodes Unicode escape
    sequences in all string values.
    """
    if isinstance(obj, dict):
        return {k: decode_unicode_escapes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decode_unicode_escapes(elem) for elem in obj]
    elif isinstance(obj, str):
        return obj.encode('raw_unicode_escape').decode('unicode_escape')
    else:
        return obj


def repair_json_string(raw_text: str) -> any:
    """
    Repairs syntax of a JSON string and parses it into a Python object.
    If standard repair fails, it attempts to parse the string as a malformed XML-like tool call.
    Returns the object on success, or None on failure.
    """
    if not raw_text:
        return None
    try:
        # First, try the robust json_repair library
        return repair_json(raw_text, return_objects=True)
    except Exception as e:
        logging.warning(f"Standard JSON repair failed: {e}. Attempting XML-like fallback parser.")
        # If json_repair fails, try our custom XML-like parser as a fallback
        try:
            repaired_obj = _repair_xml_like_tool_call(raw_text)
            if repaired_obj:
                logging.info("Successfully repaired response using XML-like fallback parser.")
                return repaired_obj
        except Exception as xml_e:
            logging.error(f"XML-like fallback parser also failed: {xml_e}", exc_info=True)

        logging.warning(f"All repair attempts failed for raw text: {raw_text}")
        return None

def _repair_xml_like_tool_call(raw_text: str) -> dict | None:
    """
    Parses a string that might contain an XML-like or malformed structure for a tool call.
    It prioritizes finding a valid tool name from the schema first, then extracts its arguments.
    """
    logging.info("Attempting to repair response using content-first XML-like parser.")

    cleaned_text = re.sub(r'\s+', ' ', raw_text).strip().replace('`', '')

    found_tool_name = None

    # --- Step 1: Find a valid tool name in the text ---
    # Sort by length descending to match longer names first (e.g., 'read_many_files' before 'read_file')
    for tool in sorted(VALID_TOOL_NAMES, key=len, reverse=True):
        # Use word boundaries to avoid partial matches
        if re.search(fr'\b{re.escape(tool)}\b', cleaned_text, re.IGNORECASE):
            found_tool_name = tool
            logging.info(f"Found potential tool name in response: '{found_tool_name}'")
            break

    if not found_tool_name:
        logging.warning("Content-first parser did not find any valid tool name in the response.")
        return None

    # --- Step 2: Extract Arguments with improved regex for unclosed tags ---
    arguments = {}

    # This regex finds a parameter tag and captures its name and value.
    # The value is captured non-greedily (.*?) and stops at the beginning of the next tag (?=<) or end of string ($).
    # This handles cases where the closing </parameter> tag is missing.
    args_pattern_param = r'<parameter\s+name="?([^\"]+)"?>(.*?)(?=<|$)'

    arg_pairs = re.findall(args_pattern_param, cleaned_text)
    for key, value in arg_pairs:
        # Avoid misinterpreting the tool name itself as an argument
        if key.lower() not in TOOL_NAME_SYNONYMS:
            arguments[key.strip()] = value.strip()

    # --- Step 3: Reconstruct the object into the standard format our system expects ---
    logging.info(f"Content-first parser identified tool: '{found_tool_name}' with arguments: {arguments}")
    return {
        "action": {
            "name": found_tool_name,
            "arguments": arguments if arguments else {}
        }
    }

def _get_obj_at_loc(data: Any, loc: tuple) -> Any | None:
    """Navigates a nested structure to get the object at a given location tuple."""
    for key in loc:
        if isinstance(data, dict) and key in data:
            data = data[key]
        elif isinstance(data, list) and isinstance(key, int) and 0 <= key < len(data):
            data = data[key]
        else:
            return None
    return data

def validate_object(data_object: any, schema_name: str) -> tuple[dict | None, str | None]:
    """
    Validates a Python object against a predefined Pydantic schema.
    Returns a tuple containing the validated dictionary and None on success,
    or None and a detailed error string on failure.
    """
    if not data_object:
        error_msg = f"Validation failed for '{schema_name}': received empty data object."
        logging.warning(error_msg)
        return None, error_msg

    pydantic_model = SCHEMA_MAP.get(schema_name)
    if not pydantic_model:
        error_msg = f"Pydantic model for schema '{schema_name}' not found."
        logging.error(error_msg)
        return None, error_msg

    if isinstance(data_object, dict) and schema_name in ('terminal_tool_code', 'browser_tool_code'):
        # Universal repair logic for various malformed tool calls in agent modes
        if 'name' in data_object and 'arguments' in data_object and 'action' not in data_object:
            logging.warning("Detected a naked tool call for terminal_tool_code. Wrapping it.")
            data_object = {'action': data_object}
        else:
            tool_name = data_object.get('action') or data_object.get('tool_name') or data_object.get('tool_code')
            args = data_object.get('arguments') or data_object.get('command_arguments') or data_object.get('parameters')

            if tool_name and isinstance(tool_name, str) and args is not None and isinstance(args, dict):
                logging.warning(f"Detected a malformed/flattened tool call ('{tool_name}'). Reconstructing it.")
                reconstructed_call = {'name': tool_name, 'arguments': args}
                data_object = {'action': reconstructed_call}

    if isinstance(data_object, list) and schema_name == 'strategic_plan':
        object_to_validate = {"type": "strategic_plan", "payload": data_object}
    elif isinstance(data_object, dict):
        # Case 1: Already well-formed.
        if "type" in data_object and "payload" in data_object:
            object_to_validate = data_object
        # Case 2: The LLM provided a payload but forgot the top-level 'type'.
        elif "payload" in data_object:
            logging.warning("Detected a response with 'payload' but missing 'type'. Adding 'type' automatically.")
            object_to_validate = data_object.copy()  # Avoid modifying the original dict
            object_to_validate['type'] = schema_name
        # Case 3: Fully unstructured response, wrap the entire object (original fallback).
        else:
            object_to_validate = {"type": schema_name, "payload": data_object}
    else:
        error_msg = f"Cannot validate data of type {type(data_object).__name__} for schema '{schema_name}'."
        logging.error(error_msg)
        return None, error_msg

    try:
        validated_model = pydantic_model.model_validate(object_to_validate)
        validated_dict = validated_model.model_dump()
        logging.info(f"Object successfully validated against Pydantic schema '{schema_name}'.")
        return validated_dict, None
    except ValidationError as e:
        error_str = f"Pydantic validation failed for schema '{schema_name}':\n{e}"
        logging.error(error_str)
        return None, error_str
    except Exception as e:
        error_msg = f"An unexpected error occurred during Pydantic validation for '{schema_name}': {e}"
        logging.error(error_msg)
        return None, error_msg

# --- Universal Fallback Parser (Hierarchical) ---

def universal_fallback_parser(raw_object: dict) -> tuple[Any | None, str | None]:
    """
    Primary fallback function that attempts to repair a malformed object by trying different strategies in order of priority.
    """
    logging.info("Activating universal fallback parser...")

    # Priority 1: Try to repair as a full strategic plan.
    repaired_as_plan, schema_name = _try_repair_as_strategic_plan(raw_object)
    if repaired_as_plan:
        return repaired_as_plan, schema_name

    # Priority 2: Try to repair as a standard evaluation with typos.
    repaired_as_eval, schema_name = _try_repair_as_standard_evaluation(raw_object)
    if repaired_as_eval:
        return repaired_as_eval, schema_name

    # Priority 3: Try to repair a 'flattened' evaluation.
    repaired_as_flat_eval, schema_name = _try_repair_as_flattened_evaluation(raw_object)
    if repaired_as_flat_eval:
        return repaired_as_flat_eval, schema_name

    logging.warning("Universal fallback parser failed. Could not repair the object with any known strategy.")
    return None, None

# --- Repair Strategy 1: Find a Strategic Plan ---

def _find_task_list_recursively(data: Any, max_depth=5, current_depth=0) -> list[dict] | None:
    """Recursively searches for a list of objects that look like Tasks."""
    if current_depth > max_depth:
        return None
    if isinstance(data, list) and data:
        first_item = data[0]
        if isinstance(first_item, dict) and all(k in first_item for k in ["task_description", "actions", "expected_outcome"]):
            return data
        for item in data:
            found = _find_task_list_recursively(item, max_depth, current_depth + 1)
            if found:
                return found
    elif isinstance(data, dict):
        for value in data.values():
            found = _find_task_list_recursively(value, max_depth, current_depth + 1)
            if found:
                return found
    return None

def _try_repair_as_strategic_plan(raw_response: dict) -> tuple[Any | None, str | None]:
    """Attempts to find a task list and wrap it in a 'strategic_failure' Evaluation object."""
    logging.info("Repair attempt (Priority 1): Searching for a strategic plan...")
    task_list = _find_task_list_recursively(raw_response)
    if not task_list:
        logging.info("Repair attempt (Priority 1) failed: No task list found.")
        return None, None

    logging.info(f"Repair attempt (Priority 1) successful: Found a plan with {len(task_list)} tasks. Wrapping it.")
    evaluation_wrapper = {
        "type": "evaluation",
        "payload": {
            "evaluation": {"status": "failure", "reason": "Inferred from fallback: LLM provided a new strategic plan directly."},
            "next_move": {"type": "strategic_failure", "new_plan": task_list}
        }
    }
    validated_obj, _ = validate_object(evaluation_wrapper, 'evaluation')
    return (validated_obj, 'evaluation') if validated_obj else (None, None)

# --- Repair Strategy 2: Fix a Standard Evaluation ---

def _find_best_match(value: str, candidates: list[str]) -> str | None:
    """Finds the best fuzzy match for a string from a list of candidates."""
    if not value or not isinstance(value, str):
        return None
    matcher = SequenceMatcher(a=value.lower())
    best_match, highest_ratio = None, 0
    for candidate in candidates:
        matcher.set_seq2(candidate.lower())
        ratio = matcher.ratio()
        if ratio > highest_ratio:
            highest_ratio, best_match = ratio, candidate
    if highest_ratio >= SIMILARITY_THRESHOLD:
        if value != best_match:
            logging.info(f"Corrected value '{value}' to '{best_match}' with similarity {highest_ratio:.2f}")
        return best_match
    return None

def _find_object_by_keys(data: Any, required_keys: list[str], max_depth=5, current_depth=0) -> dict | None:
    """Recursively searches for a dictionary that contains all of the required keys."""
    if current_depth > max_depth:
        return None
    if isinstance(data, dict):
        if all(key in data for key in required_keys):
            return data
        for value in data.values():
            if (found := _find_object_by_keys(value, required_keys, max_depth, current_depth + 1)):
                return found
    elif isinstance(data, list):
        for item in data:
            if (found := _find_object_by_keys(item, required_keys, max_depth, current_depth + 1)):
                return found
    return None

def _try_repair_as_standard_evaluation(raw_object: dict) -> tuple[Any | None, str | None]:
    """Attempts to find and repair a standard Evaluation object with potential typos."""
    logging.info("Repair attempt (Priority 2): Searching for standard evaluation components...")
    evaluation_part = _find_object_by_keys(raw_object, ['status', 'reason'])
    next_move_part = _find_object_by_keys(raw_object, ['type'])

    if not (evaluation_part and next_move_part):
        logging.info("Repair attempt (Priority 2) failed: Could not find both 'evaluation' and 'next_move' components.")
        return None, None

    logging.info("Found potential 'evaluation' and 'next_move' components. Attempting to correct enums.")
    if 'status' in evaluation_part:
        if (corrected_status := _find_best_match(evaluation_part['status'], VALID_STATUSES)):
            evaluation_part['status'] = corrected_status
    if 'type' in next_move_part:
        if (corrected_type := _find_best_match(next_move_part['type'], VALID_NEXT_MOVE_TYPES)):
            next_move_part['type'] = corrected_type

    reconstructed_payload = {
        "type": "evaluation",
        "payload": {"evaluation": evaluation_part, "next_move": next_move_part}
    }

    validated_obj, _ = validate_object(reconstructed_payload, 'evaluation')
    if validated_obj:
        logging.info("Repair attempt (Priority 2) successful: The object has been corrected and validated.")
        return validated_obj, 'evaluation'
    else:
        logging.warning("Repair attempt (Priority 2) failed: Could not validate the reconstructed object.")
        return None, None

# --- Repair Strategy 3: Fix a Flattened Evaluation ---

def _try_repair_as_flattened_evaluation(raw_object: dict) -> tuple[Any | None, str | None]:
    """
    Attempts to repair an evaluation object that is 'flattened' (e.g., 'status' and 'next_move' are at the top level).
    """
    logging.info("Repair attempt (Priority 3): Searching for a flattened evaluation...")

    # Check for the minimum required keys at the top level
    if 'status' not in raw_object or 'next_move' not in raw_object or not isinstance(raw_object.get('next_move'), dict):
        logging.info("Repair attempt (Priority 3) failed: Missing 'status' or 'next_move' at the top level.")
        return None, None

    logging.info("Found flattened evaluation components. Attempting to reconstruct and correct.")

    # 1. Reconstruct the 'evaluation' part, adding a default reason if missing.
    status = raw_object['status']
    reason = raw_object.get('reason')

    # If 'reason' is not at the top level, try to find it as 'feedback'
    # inside a nested 'evaluation' object.
    if not reason and isinstance(raw_object.get('evaluation'), dict):
        reason = raw_object['evaluation'].get('feedback')

    if not reason:
        # This log is for the developer, to be seen in the terminal.
        logging.info("LLM provided a flattened response without a reason. Generating a default reason for internal logic.")
        if status == 'success':
            reason = "The action was completed successfully."
        else:
            reason = "A reason for the failure was not provided by the LLM."

    evaluation_part = {
        'status': status,
        'reason': reason
    }

    # 2. Get the 'next_move' part.
    next_move_part = raw_object['next_move']

    # 3. Correct enum values using fuzzy matching, similar to the standard repair.
    if (corrected_status := _find_best_match(evaluation_part['status'], VALID_STATUSES)):
        evaluation_part['status'] = corrected_status

    if 'type' in next_move_part:
        if (corrected_type := _find_best_match(next_move_part['type'], VALID_NEXT_MOVE_TYPES)):
            next_move_part['type'] = corrected_type

    # 4. Build the final object in the correct nested structure.
    reconstructed_payload = {
        "type": "evaluation",
        "payload": {"evaluation": evaluation_part, "next_move": next_move_part}
    }

    # 5. Validate the reconstructed object.
    validated_obj, _ = validate_object(reconstructed_payload, 'evaluation')
    if validated_obj:
        logging.info("Repair attempt (Priority 3) successful: The flattened object has been corrected and validated.")
        return validated_obj, 'evaluation'
    else:
        logging.warning("Repair attempt (Priority 3) failed: Could not validate the reconstructed flattened object.")
        return None, None
