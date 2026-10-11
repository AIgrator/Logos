# src/computer/schemas/vision.py
import json
from pydantic import BaseModel, Field
from typing import Any, Literal

# A generic representation for a tool call or a plan step
class ToolCall(BaseModel):
    name: str = Field(..., example="click")
    arguments: dict[str, Any] | None = Field(default=None, example={"element_description": "The 'Login' button"})

# --- Task (New) ---
# A Task is a self-contained "contract" for a logical step.
class Task(BaseModel):
    task_description: str = Field(..., description="A human-readable goal for this task, e.g., 'Create a new folder named MyProject'.", example="Create a new folder named 'MyProject' on the Desktop.")
    actions: list[ToolCall] = Field(..., description="Atomic actions. KEY RULE: at most ONE state-changing action per task (a UI action like 'click'/'launch_app' or a shell/file action like 'execute_shell') — the outcome is verified after every task. Auxiliary actions ('write_text', 'hotkey') may join the main one.")
    expected_outcome: str = Field(..., description="A clear, verifiable description of what the screen should look like after the task is successfully completed.", example="A new folder named 'MyProject' should be visible on the Desktop.")
    expected_output: str | None = Field(default=None, description="Optional textual success criterion for shell/file tasks (e.g. a substring of stdout or 'exit code 0'). UI tasks leave this empty and are judged visually via expected_outcome; shell tasks are judged by command output and skip screenshots.", example="STDOUT contains 'MyProject' and EXIT CODE: 0")

# --- Strategic Plan ---
class StrategicPlan(BaseModel):
    """A multi-step plan to achieve the user's goal."""
    type: Literal['strategic_plan'] = Field(..., example="strategic_plan")
    payload: list[Task]

# --- Evaluation ---
class EvaluationDetails(BaseModel):
    status: Literal['success', 'failure', 'unexpected_wait'] = Field(..., example="success")  #, 'simple_failure', 'strategic_failure'
    reason: str = Field(..., example="The folder was created successfully.")

class NextMove(BaseModel):
    type: Literal['action', 'action_correction', 'task_replacement', 'insertion_correction', 'strategic_failure'] = Field(..., example="action")
    action: ToolCall | None = Field(default=None, example={"name": "finish", "arguments": {"message": "Task complete."}})
    new_task: Task | None = Field(default=None, description="A new task to be inserted or to replace the current one.")
    new_plan: list[Task] | None = Field(default=None, description="A complete new strategic plan, provided pre-emptively by the LLM.")

class EvaluationPayload(BaseModel):
    evaluation: EvaluationDetails
    next_move: NextMove

class Evaluation(BaseModel):
    """Evaluation of the last action and the next move."""
    type: Literal['evaluation'] = Field(..., example="evaluation")
    payload: EvaluationPayload

# --- Wait Check Result ---
class WaitCheckResultPayload(BaseModel):
    condition_met: bool = Field(..., example=True)
    reason: str = Field(..., example="The progress bar has disappeared, indicating the download is complete.")

class WaitCheckResult(BaseModel):
    """Result of a wait condition check."""
    type: Literal['wait_check_result'] = Field(..., example="wait_check_result")
    payload: WaitCheckResultPayload

# --- Wait Origin Result ---
class WaitOriginResultPayload(BaseModel):
    origin_action_index: int = Field(..., example=-2)
    reason: str = Field(..., example="Action #-2, 'click on Download button', is the clear origin of the wait.")

class WaitOriginResult(BaseModel):
    """The result of finding the origin action for a wait."""
    type: Literal['wait_origin_result'] = Field(..., example="wait_origin_result")
    payload: WaitOriginResultPayload

# --- Error Explanation ---
class ErrorExplanationPayload(BaseModel):
    error_explanation: str = Field(..., example="I was unable to find the specified button. It might be disabled or not visible on the screen.")

class ErrorExplanation(BaseModel):
    """An explanation of an error for the user."""
    type: Literal['error_explanation'] = Field(..., example="error_explanation")
    payload: ErrorExplanationPayload

# A dictionary to easily map schema names to their Pydantic models
SCHEMA_MAP = {
    'strategic_plan': StrategicPlan,
    'evaluation': Evaluation,
    'wait_check_result': WaitCheckResult,
    'wait_origin_result': WaitOriginResult,
    'error_explanation': ErrorExplanation,
}

# --- Generation Logic ---

def _create_example_from_schema(schema: dict, defs: dict = None) -> any:
    """Creates a mock example object from a JSON schema, prioritizing 'example' keys."""
    if not isinstance(schema, dict):
        return None

    if defs is None:
        defs = {}

    # Priority 1: Use the 'example' key if it exists in the schema
    if 'example' in schema:
        return schema['example']

    # Handle Optional fields (represented as anyOf in JSON Schema)
    if 'anyOf' in schema:
        # Prefer non-null types for the example. If null is an option,
        # we can use it as a fallback, but a concrete example is better.
        non_null_schemas = [s for s in schema['anyOf'] if s.get('type') != 'null']
        if non_null_schemas:
            # Try to generate an example from the first non-null schema
            return _create_example_from_schema(non_null_schemas[0], defs)
        else:
            # If only 'null' is an option, or list is empty, return None -> null
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
        # If type can be ['array', 'null'], prefer generating an empty list or a list with one item.
        # The 'anyOf' logic above should handle the Optional case, but this is a safeguard.
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
        # Fallback for complex types not explicitly handled
        return "unknown_type_example"

def _resolve_refs(obj: Any, defs: dict) -> Any:
    """Recursively traverses a schema object and replaces all $ref pointers."""
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
    """
    Generates a dictionary with response format instructions for the LLM,
    based on the Pydantic models defined in this file.
    """
    formats = {}
    for name, model in SCHEMA_MAP.items():
        # Generate schema with example values
        schema = model.model_json_schema()

        defs = schema.pop('$defs', {})

        # Resolve references within the main schema body
        resolved_schema = _resolve_refs(schema, defs)

        # Create a dynamic example from the resolved schema, now using 'example' fields
        payload_schema = resolved_schema.get('properties', {}).get('payload', {})
        example_payload = _create_example_from_schema(payload_schema, defs)

        # The top-level type field also needs an example
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
    # This block allows you to run `python src/computer/schemas/vision.py` to see the generated output
    generated_formats = generate_response_formats_dict()
    print(json.dumps(generated_formats, indent=2))
