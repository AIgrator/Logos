# src/computer/schemas/strict.py
"""
Strict / grammar-constrained tool-call schema builder.

Providers that can constrain decoding to a grammar (OpenAI Structured Outputs /
strict tools, Anthropic strict tool use, Google `VALIDATED`/`ANY` function
calling, x.ai where tool calling is implicitly strict, OpenRouter when the
upstream endpoint supports it) only accept a narrow subset of JSON Schema:

  * the root must be an object, never a top-level ``anyOf``;
  * every object must set ``additionalProperties: false``;
  * every property must be listed in ``required`` (optionality is expressed as
    a nullable type, e.g. ``["string", "null"]``);
  * only object / array / string / number / integer / boolean / null, ``enum``
    and ``anyOf`` are reliably supported across all of them.

The legacy envelope generated from the Pydantic models exposes ``arguments`` as
a free-form ``dict[str, Any]``. No provider can constrain that under strict
mode, and OpenAI rejects it outright because ``additionalProperties`` must be
``false``. This module rebuilds the envelope from the tool manifests so that
each tool's arguments are fully typed and the tool name becomes a literal,
letting the provider constrain the name and the arguments together.

The produced schema describes exactly the same *output shape* as before::

    {"type": "<envelope>", "payload": {"action": {"name": ..., "arguments": {...}}}}
"""

from __future__ import annotations

from typing import Any

# Manifest scalar type strings -> JSON Schema types.
_SCALAR_TYPES = {
    "STRING": "string",
    "NUMBER": "number",
    "INTEGER": "integer",
    "BOOLEAN": "boolean",
}


def _nullable(schema: dict) -> dict:
    """Express an optional field as a nullable type (strict requires all keys)."""
    t = schema.get("type")
    if t is None:
        return schema
    if isinstance(t, list):
        if "null" not in t:
            schema["type"] = [*t, "null"]
    else:
        schema["type"] = [t, "null"]
    return schema


def _arg_schema(spec: Any) -> dict:
    """Convert a single manifest argument spec into a strict JSON Schema fragment."""
    if not isinstance(spec, dict):
        return {"type": "string"}

    raw_type = str(spec.get("type", "STRING")).upper()

    if raw_type == "OBJECT":
        schema = _object_schema(spec.get("properties") or {})
    elif raw_type == "ARRAY" or raw_type == "LIST_OF_STRINGS":
        schema = {"type": "array"}
        items = spec.get("items")
        if raw_type == "LIST_OF_STRINGS" or not isinstance(items, dict):
            schema["items"] = {"type": "string"}
        else:
            schema["items"] = _arg_schema(items)
    else:
        schema = {"type": _SCALAR_TYPES.get(raw_type, "string")}

    description = spec.get("description")
    if description:
        schema["description"] = description

    enum = spec.get("enum")
    if enum:
        schema["enum"] = list(enum)

    return schema


def _object_schema(properties: dict) -> dict:
    """Build a closed object schema: all keys required, optionals nullable."""
    props: dict[str, Any] = {}
    for name, spec in (properties or {}).items():
        field = _arg_schema(spec)
        if isinstance(spec, dict) and not spec.get("required", False):
            field = _nullable(field)
        props[name] = field
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


def build_action_union(metadata: dict) -> list[dict]:
    """Discriminated union of every tool: one variant per tool name."""
    variants: list[dict] = []
    for tool_name in sorted(metadata):
        info = metadata.get(tool_name)
        if not isinstance(info, dict):
            # Accept raw `name -> function` manifests (use the __tool_meta__ attr).
            info = getattr(info, "__tool_meta__", None) or {}
        tool = info.get("tool") or {}
        name = tool.get("name") or tool_name
        arguments = tool.get("arguments") or {}

        variant: dict[str, Any] = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": [name]},
                "arguments": _object_schema(arguments),
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        }
        description = tool.get("description")
        if description:
            variant["description"] = description
        variants.append(variant)
    return variants


def build_flat_action_schema(metadata: dict) -> dict:
    """Single-branch action schema: a `name` enum plus a closed argument bag.

    Collapses the per-tool discriminated union into ONE object so the whole
    envelope fits a provider's small grammar budget (e.g. Anthropic strict tool
    use rejects unions of more than ~10 branches with "compiled grammar is too
    large"). Every argument is listed as ``required``: optional keys turn the
    grammar into a combinatorial set of branches, which Anthropic rejects with
    "Schema is too complex". The output shape is unchanged:
    ``{"name": ..., "arguments": {...}}``.
    """
    names: list[str] = []
    props: dict[str, Any] = {}
    for tool_name in sorted(metadata):
        info = metadata.get(tool_name)
        if not isinstance(info, dict):
            info = getattr(info, "__tool_meta__", None) or {}
        tool = info.get("tool") or {}
        name = tool.get("name") or tool_name
        names.append(name)
        for arg_name, spec in (tool.get("arguments") or {}).items():
            if arg_name not in props:
                field = _arg_schema(spec)
                # Optional arguments stay listed in `required` but become
                # nullable, so the model can leave them out as null instead
                # of being forced to invent a value (e.g. read_files.offset).
                if isinstance(spec, dict) and not spec.get("required", False):
                    field = _nullable(field)
                props[arg_name] = field
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": names},
            "arguments": {
                "type": "object",
                "properties": props,
                "required": list(props.keys()),
                "additionalProperties": False,
            },
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    }


# Optional narration inside the payload, next to the action. It lives at the
# envelope level (not per tool) so the tool union/grammar stays unchanged.
COMMENTARY_SCHEMA = {
    "type": ["string", "null"],
    "description": (
        "Optional one-sentence note to the user about this step, in their "
        "language. Shown as narration in the activity block. null when no "
        "explanation is needed."),
}


def build_strict_schema_flat(name: str, metadata: dict, description: str | None = None) -> dict:
    """Same envelope as :func:`build_strict_schema`, but with a single-branch
    action (see :func:`build_flat_action_schema`) instead of the 10+ branch
    union. Used as a strict-grammar fallback for providers whose grammar budget
    cannot hold the full union."""
    envelope: dict[str, Any] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": [name]},
            "payload": {
                "type": "object",
                "properties": {
                    "commentary": COMMENTARY_SCHEMA,
                    "action": build_flat_action_schema(metadata),
                },
                "required": ["commentary", "action"],
                "additionalProperties": False,
            },
        },
        "required": ["type", "payload"],
        "additionalProperties": False,
    }
    if description:
        envelope["description"] = description
    return envelope


def build_strict_schema(name: str, metadata: dict, description: str | None = None) -> dict:
    """Build a strict, single-tool-call envelope schema for one mode."""
    envelope: dict[str, Any] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": [name]},
            "payload": {
                "type": "object",
                "properties": {
                    "commentary": COMMENTARY_SCHEMA,
                    "action": {"anyOf": build_action_union(metadata)},
                },
                "required": ["commentary", "action"],
                "additionalProperties": False,
            },
        },
        "required": ["type", "payload"],
        "additionalProperties": False,
    }
    if description:
        envelope["description"] = description
    return envelope


def to_anthropic_schema(schema: Any) -> Any:
    """Convert a strict JSON Schema to Anthropic's strict-tool dialect.

    Anthropic rejects the ``["T", "null"]`` type-array idiom (and enum values
    that do not literally match the declared type). Its native way to express an
    optional field is to omit it from ``required`` while keeping its plain type.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        stype = schema.get("type")
        if isinstance(stype, list):
            non_null = [t for t in stype if t != "null"]
            stype = non_null[0] if non_null else None
        # Anthropic compiles the schema into a grammar; descriptions inflate it
        # without affecting the accepted language, so they are dropped here.
        if "enum" in schema:
            out["enum"] = schema["enum"]
        if stype:
            out["type"] = stype
        if "properties" in schema:
            props = schema["properties"]
            out["properties"] = {k: to_anthropic_schema(v) for k, v in props.items()}
            required = []
            for k, v in props.items():
                t = v.get("type") if isinstance(v, dict) else None
                optional = isinstance(t, list) and "null" in t
                if not optional:
                    required.append(k)
            if required:
                out["required"] = required
        elif "required" in schema:
            out["required"] = list(schema["required"])
        if "items" in schema:
            out["items"] = to_anthropic_schema(schema["items"])
        if "anyOf" in schema:
            out["anyOf"] = [to_anthropic_schema(s) for s in schema["anyOf"]]
        if schema.get("additionalProperties") is False:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [to_anthropic_schema(s) for s in schema]
    return schema


def to_google_schema(schema: Any) -> Any:
    """Convert a strict JSON Schema to Gemini's OpenAPI-subset dialect.

    Gemini's ``response_schema`` follows OpenAPI Schema, which differs from the
    OpenAI-compatible subset we emit:
      * ``type`` must be a single string (nullable is a separate flag);
      * optional fields use ``nullable: true`` instead of ``["T", "null"]``;
      * ``additionalProperties`` is not accepted.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        stype = schema.get("type")
        nullable = False
        if isinstance(stype, list):
            nullable = "null" in stype
            non_null = [t for t in stype if t != "null"]
            stype = non_null[0] if non_null else "string"
        for key in ("description", "enum", "title"):
            if key in schema:
                out[key] = schema[key]
        if stype:
            out["type"] = stype
        if nullable:
            out["nullable"] = True
        if "properties" in schema:
            out["properties"] = {k: to_google_schema(v) for k, v in schema["properties"].items()}
        if "required" in schema:
            out["required"] = list(schema["required"])
        if "items" in schema:
            out["items"] = to_google_schema(schema["items"])
        if "anyOf" in schema:
            out["anyOf"] = [to_google_schema(s) for s in schema["anyOf"]]
        return out
    if isinstance(schema, list):
        return [to_google_schema(s) for s in schema]
    return schema


def validate_strict_schema(schema: Any) -> list[str]:
    """Return a list of strict-subset violations (empty list == valid)."""
    problems: list[str] = []

    def walk(node: Any, path: str, root: bool) -> None:
        if isinstance(node, dict):
            if root and "anyOf" in node:
                problems.append(f"{path}: root schema must not be anyOf")
            t = node.get("type")
            is_object = t == "object" or (
                isinstance(t, list) and "object" in t
            ) or "properties" in node
            if is_object:
                if node.get("additionalProperties") is not False:
                    problems.append(f"{path}: object missing additionalProperties:false")
                props = node.get("properties") or {}
                required = node.get("required")
                if not isinstance(required, list) or set(required) != set(props.keys()):
                    problems.append(f"{path}: required must list every property")
            for key in ("properties",):
                for child_name, child in (node.get(key) or {}).items():
                    walk(child, f"{path}.{child_name}", False)
            if "items" in node:
                walk(node["items"], f"{path}[]", False)
            for child in node.get("anyOf") or []:
                walk(child, f"{path}.anyOf", False)
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]", False)

    walk(schema, "$", True)
    return problems


if __name__ == "__main__":
    import importlib
    import json

    for mode in ("terminal", "browser"):
        meta = importlib.import_module(f"src.computer.modes.{mode}").get_tools_metadata()
        type_name = f"{mode}_tool_code"
        schema = build_strict_schema(type_name, meta, description=f"Direct tool call for {mode} mode.")
        problems = validate_strict_schema(schema)
        print(f"{mode}: tools={len(meta)} json_bytes={len(json.dumps(schema))} problems={problems}")
