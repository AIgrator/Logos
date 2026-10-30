# Guide: Adding a New LLM Provider

A provider is one self-contained module in `src/llm/providers/`. The application
discovers it automatically, shows it in Settings, and imports its SDK only when
that provider is actually selected. Adding a provider does not require editing
the controller, the settings UI, or the worker.

The base contract lives in [`src/llm/base_provider.py`](../src/llm/base_provider.py).
The lazy discovery mechanism is described in
[LLM Provider Loading Flow](./llm_provider_loading_flow.md).

---

## 1. Create the module

Place the file at:

```
src/llm/providers/<provider_id>_provider.py
```

Rules that the loader relies on:

- The filename **must** end with `_provider.py`; other modules in the folder are
  ignored.
- The filename (without `_provider`) is the provider id used during discovery.
  Keep it equal to `get_provider_id()`.
- The module must define exactly one concrete subclass of `BaseLLMProvider`.
- `get_provider_id()` and `get_display_name()` must return string literals.
  Discovery parses the source without importing it, but its AST scan only sees
  module-level functions; class-based methods are not extracted, so the
  Settings dropdown currently uses the filename-derived id and name
  (`openai_provider.py` → id `openai`, caption `Openai`). The static methods
  are still required by the provider contract and are used whenever the class
  is loaded.

The provider id is a stable key used in `settings.json` (`llm_providers.<id>`),
in `models.json` (`"<provider_id>::<model_id>"` capability keys), and in the
settings dropdowns. Do not rename it after release.

---

## 2. Implement the class contract

```python
from ..base_provider import BaseLLMProvider
from ..exceptions import FatalError, RetryableError


class ExampleProvider(BaseLLMProvider):
    @staticmethod
    def get_provider_id() -> str:
        return "example"

    @staticmethod
    def get_display_name() -> str:
        return "Example AI"

    @staticmethod
    def get_default_settings() -> dict:
        return {
            "model": "example-large",
            "require_json_output": True,
        }

    @staticmethod
    def get_ui_schema() -> dict:
        return {
            "api_key": {"type": "password", "label": "API Key"},
            "model": {
                "type": "dropdown",
                "label": "Model",
                "options_source": "models.json",
            },
            "require_json_output": {
                "type": "checkbox",
                "label": "Require JSON Output",
            },
        }

    @staticmethod
    def get_provider_config() -> dict:
        return {
            "api_url": "https://api.example.com/v1",
            "supports_cancellation": False,
            "supports_streaming_tools": False,
        }

    def _execute_get_response(self, prompt, attachments=None,
                              system_prompt=None, history=None,
                              schema_name=None) -> dict:
        ...

    def stream_response(self, prompt, attachments=None, system_prompt=None,
                        history=None, callbacks=None):
        ...
```

### Required static methods

| Method | Purpose |
| --- | --- |
| `get_provider_id()` | Stable id and settings key. Must return a literal string. |
| `get_display_name()` | Name shown in Settings and the model switcher. Must return a literal string. |
| `get_default_settings()` | Initial values written to `settings.json` the first time the provider is used. Must include `model`; `require_json_output` is strongly recommended. |
| `get_ui_schema()` | Field list rendered by `LLMProviderWidget`. Key order is the UI order. |

### Required instance methods

| Method | Purpose |
| --- | --- |
| `_execute_get_response(...)` | Non-streaming request used by agents. Called through the base wrapper `get_response()`, which also enforces `require_json_output`. |
| `stream_response(...)` | Streaming request used by Chat. Yields text chunks or typed dict chunks. |

`_execute_get_response` receives `schema_name` when the caller expects a
structured object (terminal/browser tool envelopes, vision plans on legacy
paths). Return:

```python
{"text": "...", "tool_calls": {"type": "<schema_name>", "payload": {...}},
 "reasoning": "provider-produced thought summary"}
```

`reasoning` is optional. When a provider exposes thought summaries for
non-streaming calls (OpenAI reasoning summaries, Gemini thought parts, xAI
`reasoning_content`, OpenRouter `reasoning`/`reasoning_details`, HuggingFace
`reasoning_content`), put that text here: the worker passes it to the agent,
which renders it as the collapsed `Reasoning` block and journals a `reasoning`
event. Leave it empty (or omit the key) when the API exposes no summary.
Anthropic forced tool calls do not return thinking blocks, so that path has no
summary to offer.

Return `{"text": "...", "tool_calls": {}}` when the model produced no call.
With `require_json_output=True`, `BaseLLMProvider.get_response()` replaces an
empty or non-dict `tool_calls` with a structured `error_explanation`, so the
agent loop can self-correct instead of crashing. Plain-text callers (the
session-summary request) pass `require_structured=False` to skip that
replacement and keep the response as text.

Advertise the tool set only when `schema_name` is set. A request without a
schema (chat, session summaries) must be answered with text; attaching agent
tools there lets the model reply with a `finish` tool call instead of text.

For structured output, providers normally obtain the JSON with the vendor's
native mechanism (forced tool call, response schema, or MIME type), then run
[`repair_json_string`](../src/llm/llm_parsing.py) as a safety net and return the
result as `tool_calls`. Do not parse tool envelopes in the worker.

`stream_response` has two modes, selected by
`get_provider_config()["supports_streaming_tools"]`:

- **`False`** — yield plain text strings. Chat works, native chat-mode search
  tools are unavailable for this provider.
- **`True`** — yield typed chunks:
  - `{"type": "text", "content": "..."}`
  - `{"type": "tool_code", "content": {...}}` where the content validates as
    `ChatFunctionCall` (chat search/fetch tools).

Streaming may also yield the control string
`"generation_id::<id>"`; the worker captures it so the Cancel button can ask
the provider to stop server-side generation.

### Optional instance methods

| Method | Purpose |
| --- | --- |
| `supports_computer_use()` | Return `True` only if the provider implements native Computer Use. |
| `wants_first_screenshot()` | First CUA turn should carry an opening screenshot (Google). |
| `computer_use_turn(...)` | One native computer-use protocol turn; see the docstring in `base_provider.py` for the exact return shape. |
| `web_search(query)` | Native web search. Return `None` (the base default) when unsupported; the agent then falls back to the local search stack. |
| `cancel_stream(generation_id)` | Server-side cancellation. Required only when `supports_cancellation` is `True`. |
| `_record_usage(raw)` | Store normalized token counts for the usage signal. Call it after each API response; `normalize_usage()` in [`src/llm/usage.py`](../src/llm/usage.py) handles provider field differences. |

Computer Use is currently wired only for `openai`, `anthropic`, and `google`
(`CUA_PROVIDERS` in [`src/llm/mode_capabilities.py`](../src/llm/mode_capabilities.py)).
A provider can implement `computer_use_turn()` and still be excluded from
Vision until it is added there and listed in the `vision` capability list in
`models.json`.

---

## 3. Provider configuration (`get_provider_config`)

This static dictionary is merged into the runtime config when the provider is
instantiated and when the settings page is built. Recognized keys:

| Key | Meaning |
| --- | --- |
| `api_url` | Base endpoint used by SDK/HTTP clients. |
| `supports_cancellation` | Allows the worker to call `cancel_stream()` on Cancel. |
| `supports_streaming_tools` | Enables typed `tool_code` streaming chunks for chat search tools. |
| `show_in_ui` | `False` hides the provider from the Settings dropdown (default `True`). |
| `provider_properties` | Per-sub-provider overrides (used by OpenRouter for cancellation). |

The final instance config is: static provider config → the lightweight copy in
`SettingsManager.PROVIDER_CONFIG` → the user's saved settings for that provider
→ `name` and the selected `model`. The active provider is rejected before
instantiation if its `api_key` setting is empty, so a keyless local provider
must either expose an API-key field that the user fills or the check must be
changed.

---

## 4. UI schema

`get_ui_schema()` is consumed by
[`src/ui/widgets/llm_provider_widget.py`](../src/ui/widgets/llm_provider_widget.py).
Supported `type` values:

| Type | Widget |
| --- | --- |
| `password` | Masked line edit. |
| `string` | Plain line edit. |
| `dropdown` | Editable combo box populated from `models.json` for this provider. Users can also type a model id manually. |
| `nested_dropdown` | Two editable combo boxes (group → model), used by OpenRouter. Requires `primary_dropdown_label` and `secondary_dropdown_label`. |
| `checkbox` | Checkbox with the schema `label` as its text. |
| `note` | Read-only wrapped label for warnings/explanations. |

Each field may define `label` and `tooltip`. The `options_source` key is
documentation only; model lists always come from `models.json` (see
[Dynamic LLM Model Management](./system_architecture.md#dynamic-llm-model-management)).

To make a new provider's models selectable in the normal dropdown, the remote
`models.json` must contain an entry for its provider id. Until then the combo
box is still usable via manual input.

---

## 5. Error handling (mandatory)

Never let a vendor SDK exception escape `_execute_get_response` or
`stream_response`. Classify it with the exceptions from
[`src/llm/exceptions.py`](../src/llm/exceptions.py):

- **`FatalError`** — retrying cannot help: invalid key, exhausted quota,
  malformed request (`4xx`). `LLMWorker` stops immediately and shows a
  user-facing error card.
- **`RetryableError`** — temporary: `5xx`, `529`, network failures, timeouts.
  `LLMWorker` retries with exponential backoff up to `max_retries`.

Additional rules implemented by the shared classifiers:

- Pass `retry_after` when the provider supplies it:
  `RetryableError(message, retry_after=extract_retry_after(response=..., message=...))`.
- `429` is treated as **quota exhausted** (`FatalError`) unless the message is a
  transient upstream throttle; use `is_transient_throttle()` to distinguish it,
  as the OpenRouter provider does.
- HTTP `5xx` and timeouts are retryable; do not retry authentication or
  validation failures.
- Wrap unexpected exceptions as `FatalError` so they are not retried forever.

The worker performs structured-output validation and one LLM self-correction
attempt after a schema mismatch. A provider does not need to implement that
logic; it only needs to return the model's output faithfully.

---

## 6. Logging: write nothing

Provider files contain **no logger lines**. `BaseLLMProvider.__init__` installs
an automatic wrapper around both request paths and `computer_use_turn()`, and
journals start / duration / exception / stream statistics into the single
`app.log` timeline with the task tag and turn number. The worker logs prompts,
responses, repairs, and retries.

If you need temporary diagnostics, use `logger = logging.getLogger(__name__)` and
never open log files or write to `app.log` yourself.

---

## 7. Usage accounting

After a successful call, invoke `self._record_usage(raw_usage_dict)`. The helper
normalizes input/output/reasoning/cache/total token counts, keeps the raw
values, and merges streaming usage when `merge=True`. The worker emits the
normalized result to the UI and session journal; no prices are calculated.

If a provider reports usage in several places (stream start and stream end),
call `_record_usage(..., merge=True)` for the later reports.

---

## 8. Check the integration

There is no automated test suite. After adding a provider:

1. Start the app. The provider appears in Settings → AI Provider without any
   registration step.
2. Its page renders exactly the fields from `get_ui_schema()`.
3. Enter a key and model, select it as the active provider, and send a Chat
   message (streaming path).
4. Run a Terminal task that needs a structured action (non-streaming path with
   `schema_name`); confirm the action is parsed and executed.
5. If implemented, verify native web search, Computer Use in Vision, and Cancel
   (server-side cancellation) separately.
6. Trigger an invalid key and a network failure to confirm `FatalError` vs
   `RetryableError` behavior.

No other files need to be changed. The provider is discovered on the next
application start; its module is imported only when it becomes the active
provider or when its settings page is opened.
