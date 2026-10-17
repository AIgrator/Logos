# src/llm_parsing.py
"""
Centralized utilities for validating and parsing LLM responses using Pydantic.
"""
import json
import logging
import re
from json_repair import repair_json
from pydantic import ValidationError
from typing import Any

# Import the Pydantic models and the schema map
from src.computer.schemas.vision import SCHEMA_MAP as VISUAL_SCHEMA_MAP
from src.computer.schemas.terminal import SCHEMA_MAP as TERMINAL_SCHEMA_MAP, ToolCall as TerminalToolCall
from src.computer.schemas.browser import SCHEMA_MAP as BROWSER_SCHEMA_MAP, ToolCall as BrowserToolCall

# Combine both schema maps into one for the validator to use
SCHEMA_MAP = {**VISUAL_SCHEMA_MAP, **TERMINAL_SCHEMA_MAP, **BROWSER_SCHEMA_MAP}

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

# Dynamically get valid tool names from the Pydantic schema for browser mode
BROWSER_TOOL_NAMES = list(BrowserToolCall.model_fields['name'].annotation.__args__)

# Create a unified set of all known tool names from all modes
VALID_TOOL_NAMES = set(TERMINAL_TOOL_NAMES + VISION_TOOL_NAMES + BROWSER_TOOL_NAMES)

TOOL_NAME_SYNONYMS = ['name', 'action', 'tool_name', 'function_name', 'tool', 'command']
ARGUMENTS_SYNONYMS = ['arguments', 'args', 'parameters', 'params', 'payload', 'inputs']


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

def _decode_json_lenient(text: str) -> Any | None:
    """Strict-parse JSON, falling back to the lenient json_repair on malformed
    input (duplicated/trailing braces, literal newlines, unquoted keys, ...)."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return repair_json_string(text)


def _normalize_payload_strings(data_object: Any) -> Any:
    """Central repair net shared by every provider.

    Models sometimes hand back the envelope's 'payload' as a JSON *string*
    instead of a real object, and that string is occasionally malformed (e.g.
    a duplicated trailing brace). Decode + repair it here so we validate the
    real object instead of blaming the model, and keep unwrapping while the
    decoded value is still a nested envelope. Only replaces the payload when
    the decoded value is structured (dict/list), so genuine string payloads of
    other schemas are left untouched.
    """
    if not isinstance(data_object, dict):
        return data_object

    result = dict(data_object)
    payload = result.get('payload')
    envelope_type = result.get('type')
    for _ in range(10):
        if isinstance(payload, str):
            decoded = _decode_json_lenient(payload)
            if isinstance(decoded, (dict, list)):
                logging.warning("Decoded string 'payload' into structured data before validation.")
                payload = decoded
                continue
            break
        if isinstance(payload, dict) and 'payload' in payload and (
            len(payload.keys()) == 1 or payload.get('type') == envelope_type
        ):
            logging.warning("Unwrapped nested 'payload' envelope before validation.")
            payload = payload.get('payload')
            continue
        break
    result['payload'] = payload
    return result

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

    # Central repair net (all providers): decode a string 'payload' and unwrap
    # any nested envelope before the schema-specific normalisation below.
    data_object = _normalize_payload_strings(data_object)

    if isinstance(data_object, dict) and schema_name in ('terminal_tool_code', 'browser_tool_code'):
        # Universal repair logic for various malformed tool calls in agent modes
        # Naked tool call with the tool name in 'type' instead of the envelope,
        # e.g. {"type": "finish", "arguments": {...}} — rebuild the envelope.
        mode_tools = BROWSER_TOOL_NAMES if schema_name == 'browser_tool_code' else TERMINAL_TOOL_NAMES
        type_value = data_object.get('type')
        if isinstance(type_value, str) and type_value in mode_tools and type_value != schema_name:
            args = data_object.get('arguments')
            if isinstance(args, dict):
                logging.warning(f"Detected naked tool call with type='{type_value}'. Reconstructing envelope.")
                data_object = {'type': schema_name,
                               'payload': {'action': {'name': type_value, 'arguments': args}}}
            elif isinstance(data_object.get('payload'), dict):
                logging.warning(f"Detected wrong envelope type '{type_value}'. Fixing to '{schema_name}'.")
                data_object = dict(data_object)
                data_object['type'] = schema_name
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

            # Hoist: models sometimes bury "name" one level too deep, inside
            # "arguments" ({"action": {"arguments": {"name": ..., ...}}}) —
            # lift it up to the action level instead of failing validation.
            holders = [data_object]
            payload = data_object.get('payload')
            if isinstance(payload, dict):
                holders.append(payload)
            for holder in holders:
                action_obj = holder.get('action')
                if (isinstance(action_obj, dict) and 'name' not in action_obj
                        and isinstance(action_obj.get('arguments'), dict)
                        and isinstance(action_obj['arguments'].get('name'), str)):
                    nested_name = action_obj['arguments']['name']
                    logging.warning(f"Detected 'name' nested inside arguments ('{nested_name}'). Hoisting it.")
                    fixed_action = dict(action_obj)
                    fixed_action['name'] = nested_name
                    holder['action'] = fixed_action

    if isinstance(data_object, dict):
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

# --- Safe, schema-agnostic repair (pre-correction) ---

def _extract_first_json_object(text: str) -> str | None:
    """Return the first brace-balanced ``{...}`` slice of *text*, ignoring any
    prose or markdown fences around it. Content-preserving: it only *slices*
    the string, it never rewrites it."""
    if not isinstance(text, str):
        return None
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _recursively_decode_json_strings(data_object: Any, depth: int = 0) -> Any:
    """Decode string values that are themselves JSON objects/arrays, recursively.

    Content-preserving: ``'{"a": 1}'`` becomes ``{"a": 1}``; every other string
    value is returned untouched. Reconstructs from models that stringify a
    nested part of the envelope (e.g. ``arguments``, or a doubly-encoded
    ``payload``)."""
    if depth > 20:
        return data_object
    if isinstance(data_object, dict):
        return {key: _recursively_decode_json_strings(value, depth + 1) for key, value in data_object.items()}
    if isinstance(data_object, list):
        return [_recursively_decode_json_strings(item, depth + 1) for item in data_object]
    if isinstance(data_object, str):
        if data_object.lstrip()[:1] in ('{', '['):
            decoded = _decode_json_lenient(data_object)
            if isinstance(decoded, (dict, list)):
                return _recursively_decode_json_strings(decoded, depth + 1)
        return data_object
    return data_object


def _safe_repair_candidates(data_object: Any) -> list[Any]:
    """Build the small, ordered list of meaning-preserving repairs to try."""
    candidates: list[Any] = []

    def add(candidate: Any) -> None:
        if isinstance(candidate, (dict, list)) and candidate != data_object:
            if not any(candidate == existing for existing in candidates):
                candidates.append(candidate)

    # 1. Decode any nested JSON-string values anywhere in the tree.
    add(_recursively_decode_json_strings(data_object))

    # 2. If the whole response arrived as text, slice the JSON object out of it.
    if isinstance(data_object, str):
        extracted = _extract_first_json_object(data_object)
        if extracted:
            add(repair_json_string(extracted))

    return candidates


def safe_repair_object(data_object: Any, schema_name: str) -> tuple[dict | None, str | None]:
    """Content-preserving, validate-on-accept repair for *any* schema.

    Callers invoke this ONLY after ``validate_object`` has already rejected the
    original response, so a valid response is never touched. Every candidate is
    derived from the *same* data by decoding string-encoded JSON or slicing a
    JSON object out of prose - no field is ever invented, renamed or dropped -
    and a candidate is accepted only if it passes the exact same Pydantic
    schema. Repair can therefore rescue malformed replies but can never alter
    the meaning of a good one.
    """
    last_error = f"No safe repair candidate for schema '{schema_name}'."
    for candidate in _safe_repair_candidates(data_object):
        validated, error = validate_object(candidate, schema_name)
        if validated is not None:
            logging.warning(f"safe_repair_object recovered a malformed '{schema_name}' response without altering its content.")
            return validated, None
        last_error = error or last_error
    return None, last_error
