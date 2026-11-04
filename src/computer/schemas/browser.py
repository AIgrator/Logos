# src/computer/schemas/browser.py
import json
from pydantic import BaseModel, Field
from typing import Any, Literal

from src.computer.schemas.agent_tools import AGENT_TOOL_NAMES

class ToolCall(BaseModel):
    # Union of the agent tools (see schemas/agent_tools.py): the session's grants,
    # not the envelope, decide which of them may run.
    name: Literal[*AGENT_TOOL_NAMES] = Field(..., example="browser_goto")
    arguments: dict[str, Any] | None = Field(default=None, example={"url": "https://example.com"})

class BrowserToolCodePayload(BaseModel):
    commentary: str | None = Field(
        default=None,
        description=(
            "Optional one-sentence note to the user about this batch of "
            "steps, in their language. Shown as narration in the activity "
            "block. Leave null when the steps need no explanation."),
        example="Opening the project page first.")
    actions: list[ToolCall] = Field(
        ...,
        min_length=1,
        max_length=8,
        description=(
            "One to eight independent tool calls, executed in order. Only "
            "include calls whose arguments you already know: a later call "
            "never sees an earlier call's output. finish and "
            "communicate_to_user must be alone."),
        example=[{"name": "browser_goto", "arguments": {"url": "https://example.com"}}])

class BrowserToolCode(BaseModel):
    """A direct tool call for the browser mode."""
    type: Literal['browser_tool_code'] = Field(..., example="browser_tool_code")
    payload: BrowserToolCodePayload

SCHEMA_MAP = {
    'browser_tool_code': BrowserToolCode,
}

def _create_example_from_schema(schema: dict, defs: dict = None) -> any:
    if not isinstance(schema, dict):
        return None
    if defs is None:
        defs = {}
    if 'example' in schema:
        return schema['example']
    if 'anyOf' in schema:
        non_null_schemas = [s for s in schema['anyOf'] if s.get('type') != 'null']
        if non_null_schemas:
            return _create_example_from_schema(non_null_schemas[0], defs)
        else:
            return None
    if '$ref' in schema:
        ref_key = schema['$ref'].split('/')[-1]
        return _create_example_from_schema(defs.get(ref_key, {}), defs)
    schema_type = schema.get('type')
    if schema_type == 'object':
        example = {}
        properties = schema.get('properties', {})
        for key, prop_schema in properties.items():
            example[key] = _create_example_from_schema(prop_schema, defs)
        return example
    elif schema_type == 'array':
        items_schema = schema.get('items', {})
        return [_create_example_from_schema(items_schema, defs)]
    elif schema_type == 'string':
        enum = schema.get('enum')
        if enum:
            return enum[0]
        return "string_example"
    elif schema_type == 'integer':
        return 123
    elif schema_type == 'boolean':
        return True
    elif schema_type == 'null':
        return None
    else:
        return "unknown_type_example"

def _resolve_refs(obj: Any, defs: dict) -> Any:
    if isinstance(obj, dict):
        if '$ref' in obj:
            ref_key = obj['$ref'].split('/')[-1]
            return _resolve_refs(defs.get(ref_key, {}), defs)
        return {k: _resolve_refs(v, defs) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_refs(item, defs) for item in obj]
    else:
        return obj

def generate_response_formats_dict() -> dict:
    formats = {}
    for name, model in SCHEMA_MAP.items():
        schema = model.model_json_schema()
        defs = schema.pop('$defs', {})
        resolved_schema = _resolve_refs(schema, defs)
        payload_schema = resolved_schema.get('properties', {}).get('payload', {})
        example_payload = _create_example_from_schema(payload_schema, defs)
        type_example = resolved_schema.get('properties', {}).get('type', {}).get('example', name)
        example_obj = {"type": type_example, "payload": example_payload}
        formats[name] = {
            "description": model.__doc__ or f"A response format for {name}",
            "instructions": "You MUST respond using the following single JSON format. Do not add any other text or explanation.",
            "schema": resolved_schema,
            "example": json.dumps(example_obj, indent=2)
        }
    return formats

if __name__ == '__main__':
    generated_formats = generate_response_formats_dict()
    print(json.dumps(generated_formats, indent=2))
