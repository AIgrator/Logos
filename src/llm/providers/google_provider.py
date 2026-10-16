# llm/gemini_provider.py

import json
import logging
from google import genai
from google.genai import types as genai_types

from collections.abc import Mapping, Sequence
from ..base_provider import BaseLLMProvider
from ..exceptions import FatalError, RetryableError, extract_retry_after, is_transient_error
from google.api_core import exceptions as google_exceptions
from ..llm_parsing import repair_json_string

class GoogleProvider(BaseLLMProvider):
    """
    LLM provider for Google's Gemini models.
    """
    @staticmethod
    def get_provider_id() -> str:
        return "google"

    @staticmethod
    def get_display_name() -> str:
        return "Google"

    @staticmethod
    def get_default_settings() -> dict:
        return {
            "model": "gemini-2.5-flash",
            "require_json_output": True
        }

    @staticmethod
    def get_ui_schema() -> dict:
        return {
            "api_key": {"type": "password", "label": "API Key"},
            "model": {"type": "dropdown", "label": "Model", "options_source": "models.json"},
            "require_json_output": {"type": "checkbox", "label": "Require JSON Output"}
        }

    @staticmethod
    def get_provider_config() -> dict:
        return {
            "api_url": "https://api.google.com",
            "supports_cancellation": True,
            "supports_streaming_tools": True,
        }

    def supports_computer_use(self) -> bool:
        return True

    COMPUTER_TOOLSET = {
        "type": "computer_use",
        "environment": "desktop",
    }

    ASK_USER_TOOL = {
        "name": "ask_user",
        "description": (
            "Show the human a modal dialog with your question and wait "
            "for their answer. Use it before any destructive, "
            "irreversible, or transmitting action, and whenever the task "
            "cannot proceed safely without clarification. The human's "
            "answer returns as this call's result; then continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The exact question to show the human.",
                }
            },
            "required": ["question"],
        },
    }

    MAX_CUA_CHAINS = 32

    # computer_toolset members -> our executor action types.
    # Google desktop computer_use supports:
    #   click, double_click, triple_click, middle_click, right_click,
    #   mouse_down, mouse_up, move, scroll, type, key_down, key_up,
    #   hotkey, wait, screenshot
    _MEMBER_MAP = {
        "click": ("click", "left"),
        "double_click": ("double_click", "left"),
        "middle_click": ("click", "middle"),
        "right_click": ("click", "right"),
        "mouse_down": "mouse_down",
        "mouse_up": "mouse_up",
        "move": "move",
        "scroll": "scroll",
        "type": "type",
        "key_down": "key_down",
        "key_up": "key_up",
        "hotkey": "hotkey",
        "wait": "wait",
        "screenshot": "screenshot",
    }

    # Members we deliberately don't implement in v1.
    _UNSUPPORTED_MEMBERS = {
        "cursor_position", "key_down", "key_up",
    }

    @staticmethod
    def _member_to_action(name: str, mapped, block_input: dict) -> dict:
        """Converts one computer_use tool call into our action dict."""
        if mapped == "screenshot":
            return {"type": "screenshot"}
        if mapped == "mouse_down":
            return {"type": "left_mouse_down"}
        if mapped == "mouse_up":
            return {"type": "left_mouse_up"}
        if mapped == "hold_key":
            raw = str(block_input.get("text", ""))
            keys = [k for k in raw.split("+") if k] or [raw]
            try:
                duration = float(block_input.get("duration", 1.0))
            except Exception:
                duration = 1.0
            return {"type": "hold_key", "keys": keys, "duration": duration}
        if mapped == "triple_click":
            coord = block_input.get("coordinate") or [0, 0]
            return {"type": "triple_click", "x": coord[0], "y": coord[1]}
        if mapped == "drag":
            start = block_input.get("start_coordinate") or [0, 0]
            end = block_input.get("coordinate") or [0, 0]
            return {"type": "drag",
                    "path": [{"x": start[0], "y": start[1]},
                             {"x": end[0], "y": end[1]}]}
        if mapped == "move":
            coord = block_input.get("coordinate") or [0, 0]
            return {"type": "move", "x": coord[0], "y": coord[1]}
        if mapped == "scroll":
            coord = block_input.get("coordinate") or [0, 0]
            # Pixel deltas -> wheel clicks (WHEEL_DELTA=120).
            v = int(round(float(block_input.get("scroll_y", 0)) / 120))
            h = int(round(float(block_input.get("scroll_x", 0)) / 120))
            return {"type": "scroll", "x": coord[0], "y": coord[1],
                    "scroll_x": h, "scroll_y": v}
        if mapped == "type":
            return {"type": "type", "text": block_input.get("text", "")}
        if mapped == "wait":
            try:
                seconds = float(block_input.get("seconds", 1.0))
            except Exception:
                seconds = 1.0
            return {"type": "wait", "seconds": seconds}
        if mapped == "click":
            coord = block_input.get("coordinate") or [0, 0]
            button = block_input.get("button", "left")
            keys = block_input.get("keys") or []
            return {"type": "click", "x": coord[0], "y": coord[1],
                    "button": button, "keys": keys}
        raise ValueError(f"Unsupported computer action mapping: {name}")

    def computer_use_turn(
        self,
        task: str | None = None,
        call_id: str | None = None,
        screenshot_b64: str | None = None,
        previous_response_id: str | None = None,
        acknowledged_safety_checks: list | None = None,
        instructions: str | None = None,
    ) -> dict:
        """
        One computer-use turn via the Gemini API.

        Tools sent: the `computer_use` tool (desktop environment) plus
        the `ask_user` custom tool.  Model calls arrive as
        `function_call` blocks; answers go back as `tool_result`
        blocks echoing `toolset_name`.  Coordinates are screenshot
        pixels, like OpenAI.

        One action per turn (disable_parallel_tool_use): matches the
        worker's single-call output shape, no outbox rework needed.
        """
        try:
            try:
                max_tokens = int(self.provider_config.get("max_tokens", 4096))
            except (ValueError, TypeError):
                max_tokens = 4096

            tools = [
                types.Tool(
                    computer_use=types.ComputerUse(
                        environment=types.Environment.ENVIRONMENT_DESKTOP,
                    )
                ),
                types.Tool(
                    function_declaration=self.ASK_USER_TOOL,
                ),
            ]

            if previous_response_id is None:
                if not task:
                    raise ValueError("First computer-use turn requires a task.")
                import uuid
                chain_id = uuid.uuid4().hex[:12]
                messages: list = [
                    types.Content(
                        role="user",
                        content=[types.Part(text=task)]
                    )]
                system = instructions or None
                self._cua_chains[chain_id] = {
                    "messages": messages, "system": system}
                if len(self._cua_chains) > self.MAX_CUA_CHAINS:
                    oldest = next(iter(self._cua_chains))
                    del self._cua_chains[oldest]
            else:
                chain_id = previous_response_id
                slot = self._cua_chains.get(chain_id)
                if slot is None:
                    raise ValueError(
                        "Unknown computer-use chain; restart the task.")
                if not outputs:
                    raise ValueError("Follow-up turn requires outputs.")
                messages = slot["messages"]
                system = slot["system"]
                results: list = []
                for out in outputs:
                    if out.get("kind") != "computer":
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": out.get("call_id"),
                            "content": out.get("output_text", ""),
                        })
                        continue
                    # Per-call result wins (cursor text, zoom/member image);
                    # otherwise the post-batch screenshot (last item only);
                    # never an empty image — the API rejects it.
                if out.get("result_image_b64"):
                    content = [{
                        "type": "image",
                        "image_url": {
                            "url": f"data:image/png;base64,{out['result_image_b64']}"
                        }
                    }]
                elif out.get("result_text"):
                    content = [{"type": "text",
                                "text": out["result_text"]}]
                elif out.get("screenshot_b64"):
                    content = [{
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": out["screenshot_b64"],
                        }}
                    ]
                    # end of screenshot block
                else:
                    content = [{"type": "text", "text": "OK"}]
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": out.get("call_id"),
                        "toolset_name": "computer",
                        "content": content,
                    })
                messages.append({"role": "user", "content": results})

            response = self.client.models.generate_content(
                model=self.model,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                system=system,
                messages=messages,
                timeout=90,
            )
            messages.append({"role": "assistant", "content": response.content})

            actions: list = []
            out_call_id = None
            all_ids: list = []
            asks: list = []
            done_parts: list = []
            for block in (response.content or []):
                btype = getattr(block, "type", "")
                if btype == "text":
                    done_parts.append(getattr(block, "text", "") or "")
                    continue
                if btype not in ("tool_use", "server_tool_use"):
                    continue
                name = getattr(block, "name", "")
                tset = getattr(block, "toolset_name", None)
                bid = getattr(block, "id", None)
                if bid and tset == "computer":
                    all_ids.append(bid)
                block_input = getattr(block, "input", None) or {}
                if isinstance(block_input, str):
                    try:
                        block_input = _json.loads(block_input or "{}")
                    except Exception:
                        block_input = {}
                if tset == "computer":
                    mapped = self._MEMBER_MAP.get(name)
                    if mapped is None:
                        raise ValueError(
                            f"Unknown computer member '{name}'.")
                    action = self._member_to_action(name, mapped, block_input)
                    actions.append(action)
                    out_call_id = bid
                elif name == "ask_user" and not tset:
                    asks.append({
                        "call_id": bid,
                        "question": block_input.get("question", ""),
                    })
                # Unknown third-party blocks are ignored (never crash a turn).

            done_text = "".join(done_parts).strip() or None
            return {
                "actions": actions,
                "call_id": out_call_id,
                "call_ids": all_ids,
                "response_id": chain_id,
                "pending_safety_checks": [],
                "ask": asks,
                "done_text": done_text,
            }
        except (AuthenticationError, RetryableError) as e:
            raise FatalError(f"Anthropic client error (400): {error_text}") from e

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        # Instance-level cache for generated tools module to avoid globals
        # and to allow one-time (fast) attempts per process instance.
        self.cache_gemini_tools = None
        self._cache_gemini_tools_checked = False

        if not self.api_key:
            raise ValueError("Gemini API key is not configured in settings.")

        self.client = genai.Client(api_key=self.api_key)
        # Server-side conversation chains (Google has no response chaining:
        # the full message history travels with every call). Keyed by the
        # chain id we hand out as response_id. Bounded; oldest dropped.
        self._cua_chains: dict = {}

    def _ensure_generated_tools_module(self, mode: str):
        """
        Ensure `cache.gemini_tools` exists and exposes `_get_tools_schema(mode)`.
        This provider does NOT attempt to generate the module itself. Generation
        must be performed once at application startup (see `main.py`).
        Returns the module on success, or None if missing/invalid.
        """
        # Use instance fields instead of module-level globals
        # Fast path: already loaded
        if self.cache_gemini_tools is not None:
            return self.cache_gemini_tools

        # If we've already tried and failed, skip repeated attempts (fast)
        if self._cache_gemini_tools_checked:
            return None

        # Try to import the generated module directly. Do not attempt generation here.
        try:
            import importlib
            module_path = f'cache.{mode}.gemini_tools'
            logging.info(f"Attempting to import tools from '{module_path}'...")
            mod = importlib.import_module(module_path)
        except Exception as e:
            logging.error(f"Required generated module `{module_path}` is missing. Please run the generator once at startup (ToolRegistry.generate_all()). Error: %s", e)
            self._cache_gemini_tools_checked = True
            self.cache_gemini_tools = None
            return None

        # Validate the module exposes the expected factory and that it returns
        # a non-empty collection of tool/proto objects.
        if not hasattr(mod, '_get_tools_schema'):
            logging.error('cache.gemini_tools found but does not expose `_get_tools_schema()`; ensure generator produced the correct artifact.')
            self._cache_gemini_tools_checked = True
            self.cache_gemini_tools = None
            return None

        try:
            tools_schema = mod._get_tools_schema(mode)
            # Must be a non-empty sequence
            from collections.abc import Sequence
            if not isinstance(tools_schema, Sequence) or len(tools_schema) == 0:
                logging.warning(f'cache.gemini_tools._get_tools_schema(mode="{mode}") returned empty or non-sequence value; this may be expected if the mode has no tools.')
                # This is not a fatal error; some modes may have no tools.
                # We will cache the module, but there will be no tools to use.
            else:
                # Basic structural validation: ensure the first entry looks like a Tool proto
                first = tools_schema[0]
                if not hasattr(first, 'function_declarations'):
                    logging.error('Generated tool objects do not appear to be genai_types.Tool (missing `function_declarations`).')
                    self._cache_gemini_tools_checked = True
                    self.cache_gemini_tools = None
                    return None

        except Exception as e:
            logging.error('Calling cache.gemini_tools._get_tools_schema(mode="%s") failed: %s', mode, e, exc_info=True)
            self._cache_gemini_tools_checked = True
            self.cache_gemini_tools = None
            return None

        # Success
        self.cache_gemini_tools = mod
        self._cache_gemini_tools_checked = True
        return mod

    def _recursive_to_dict(self, obj):
        """
        Recursively converts protobuf Structs and other mappings to standard dicts.
        """
        if isinstance(obj, Mapping):
            return {key: self._recursive_to_dict(value) for key, value in obj.items()}
        if isinstance(obj, Sequence) and not isinstance(obj, str | bytes):
            return [self._recursive_to_dict(item) for item in obj]
        return obj

    def web_search(self, query: str) -> str:
        """
        Executes a Google search using the native Gemini tool.
        This method creates the search tool internally and returns the final text response.
        """
        if not self.api_key:
            raise FatalError("Gemini API key is not configured. Cannot use Google Search.")

        try:
            tool = genai_types.Tool(google_search=genai_types.GoogleSearch())
            config = genai_types.GenerateContentConfig(tools=[tool])

            response = self.client.models.generate_content(
                model=self.model,
                contents=[query],
                config=config,
            )

            if response.prompt_feedback and response.prompt_feedback.block_reason:
                reason = response.prompt_feedback.block_reason.name
                raise FatalError(f"Response was blocked by safety filters: {reason}")

            return response.text

        except google_exceptions.InvalidArgument as e:
            raise FatalError(f"Google API Invalid Argument (Google Search): {e}") from e
        except google_exceptions.PermissionDenied as e:
            raise FatalError(f"Google API Permission Denied (Google Search): {e}") from e
        except google_exceptions.ResourceExhausted as e:
            raise FatalError(f"Google API Resource Exhausted (Quota for Google Search): {e}") from e
        except google_exceptions.InternalServerError as e:
            raise RetryableError(f"Google API Internal Server Error (Google Search): {e}") from e
        except google_exceptions.ServiceUnavailable as e:
            raise RetryableError(f"Google API Service Unavailable (Google Search): {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in Gemini provider (execute_google_search): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"Gemini provider transient error (execute_google_search): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in Gemini provider (execute_google_search): {e}") from e

    def _prepare_contents(self, prompt, attachments=None, history=None):
        """Prepares contents for a chat or assistant request, ensuring proper structure."""
        contents = []
        if history:
            for item in history:
                # Ensure item is a dictionary and extract role and content
                if isinstance(item, Mapping):
                    role = 'model' if item.get('role') == 'assistant' else 'user'
                    content_text = item.get('content', '')
                else:
                    # Fallback for unexpected types, log a warning
                    logging.warning(f"Unexpected type in history, converting to string: {type(item)}")
                    role = 'user' # Default role
                    content_text = str(item)

                # Ensure content is always a string before appending
                if not isinstance(content_text, str):
                    content_text = str(content_text)

                contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=content_text)]))

        current_parts = []
        if prompt:
            current_parts.append(genai_types.Part(text=prompt))

        if attachments:
            for attachment in attachments:
                if attachment is None:
                    continue
                try:
                    from PIL import Image
                    import io
                    if isinstance(attachment, str):
                        img = Image.open(attachment)
                    elif isinstance(attachment, bytes):
                        img = Image.open(io.BytesIO(attachment))
                    else:
                        img = attachment
                    buffer = io.BytesIO()
                    img_format = img.format or 'PNG'
                    img.save(buffer, format=img_format)
                    mime_type = f"image/{img_format.lower()}"
                    blob = genai_types.Blob(mime_type=mime_type, data=buffer.getvalue())
                    current_parts.append(genai_types.Part(inline_data=blob))
                except Exception as e:
                    logging.error(f"Error processing attachment for Gemini: {e}")

        if current_parts:
            contents.append(genai_types.Content(role='user', parts=current_parts))

        return contents

    def _prepare_chat_contents(self, prompt, attachments=None, history=None):
        return self._prepare_contents(prompt, attachments, history)

    def _prepare_assistant_contents(self, prompt, attachments=None, history=None):
        return self._prepare_contents(prompt, attachments, history)

    def _execute_get_response(self, prompt: str, attachments: list = None, system_prompt: str = None, history: list = None, schema_name: str = None):
        """
        Sends a non-streaming request to the Gemini API.
        Supports two modes based on `supports_tool_calling`:
        1. True: Uses modern, reliable structured output with response_schema.
        2. False: Uses the legacy method of parsing JSON from a text response.
        """
        use_tool_calling = self.require_json_output and schema_name is not None

        generation_config_kwargs = {
            'max_output_tokens': 8192,
            'safety_settings': [
                {"category": genai.types.HarmCategory.HARM_CATEGORY_HARASSMENT, "threshold": genai.types.HarmBlockThreshold.BLOCK_NONE},
                {"category": genai.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, "threshold": genai.types.HarmBlockThreshold.BLOCK_NONE},
                {"category": genai.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, "threshold": genai.types.HarmBlockThreshold.BLOCK_NONE},
                {"category": genai.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, "threshold": genai.types.HarmBlockThreshold.BLOCK_NONE},
            ]
        }

        if system_prompt:
            generation_config_kwargs['system_instruction'] = system_prompt

        if use_tool_calling:
            logging.info("Gemini Provider: Using modern structured output with response_mime_type.")
            generation_config_kwargs['response_mime_type'] = "application/json"
            contents = self._prepare_assistant_contents(prompt, attachments, history)
        else:
            logging.info("Gemini Provider: Using legacy JSON-in-text mode.")
            if system_prompt:
                mod = self._ensure_generated_tools_module(mode='vision')
                if mod and hasattr(mod, '_get_tools_schema'):
                    try:
                        tools = mod._get_tools_schema(mode='vision')
                        if tools:
                            generation_config_kwargs['tools'] = tools
                    except Exception as e:
                        logging.error('Error calling cache.gemini_tools._get_tools_schema(): %s', e, exc_info=True)
                        raise
                else:
                    logging.error('Generated cache.gemini_tools is missing or invalid; cannot use tools in legacy mode.')
                contents = self._prepare_assistant_contents(prompt, attachments, history)
            else:
                logging.warning("get_response called without a system_prompt. Using chat mode.")
                contents = self._prepare_chat_contents(prompt, attachments, history)

        if not contents:
            logging.warning("No content to send to Gemini.")
            return {"text": "", "tool_calls": []}

        try:
            generation_config = genai.types.GenerateContentConfig(**generation_config_kwargs)

            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=generation_config,
            )

            if response.prompt_feedback and response.prompt_feedback.block_reason:
                reason = response.prompt_feedback.block_reason.name
                raise FatalError(f"Response was blocked by safety filters: {reason}")

            full_text = "".join(part.text for part in response.parts if part.text)

            # --- Response Handling ---
            if use_tool_calling:
                if full_text:
                    try:
                        parsed_object = json.loads(full_text)
                    except json.JSONDecodeError as e:
                        logging.warning(f"Gemini returned invalid JSON despite mime_type enforcement: {e}")
                        logging.debug(f"--- Broken Gemini JSON Start ---\n{full_text}\n--- Broken Gemini JSON End ---")
                        parsed_object = repair_json_string(full_text)
                        if not parsed_object:
                            raise FatalError(f"Failed to parse and repair JSON from Gemini. Original error: {e}") from e
                    return {"text": full_text, "tool_calls": parsed_object}
                else:
                    logging.warning("Structured output was requested, but LLM returned an empty text response.")
                    return {"text": "", "tool_calls": []}
            else:
                # Legacy path response handling
                if full_text:
                    repaired_object = repair_json_string(full_text)
                    return {"text": full_text, "tool_calls": repaired_object or []}
                else:
                    logging.warning("Gemini legacy response was empty.")
                    return {"text": "", "tool_calls": []}

        except google_exceptions.InvalidArgument as e:
            raise FatalError(f"Google API Invalid Argument: {e}") from e
        except google_exceptions.PermissionDenied as e:
            raise FatalError(f"Google API Permission Denied: {e}") from e
        except google_exceptions.ResourceExhausted as e:
            raise FatalError(f"Google API Resource Exhausted (Quota): {e}") from e
        except google_exceptions.InternalServerError as e:
            raise RetryableError(f"Google API Internal Server Error: {e}") from e
        except google_exceptions.ServiceUnavailable as e:
            raise RetryableError(f"Google API Service Unavailable: {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in Gemini provider (get_response): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"Gemini provider transient error (get_response): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in Gemini provider (get_response): {e}") from e

    def stream_response(self, prompt: str, attachments: list = None, system_prompt: str = None, history: list = None, schema_name: str = None, callbacks=None):
        """
        Sends a request to the Gemini API and yields response chunks.
        This method supports streaming chat responses, including interleaved text and tool calls.
        """
        if system_prompt:
            logging.warning("stream_response was called with a system_prompt. For planning, use get_response.")
            # Fallback to non-streaming for safety
            response_dict = self.get_response(prompt, attachments, system_prompt, history, schema_name=schema_name)
            yield {"type": "text", "content": response_dict.get("text", "")}
            return

        safety_settings = [
            {"category": genai.types.HarmCategory.HARM_CATEGORY_HARASSMENT, "threshold": genai.types.HarmBlockThreshold.BLOCK_NONE},
            {"category": genai.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, "threshold": genai.types.HarmBlockThreshold.BLOCK_NONE},
            {"category": genai.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, "threshold": genai.types.HarmBlockThreshold.BLOCK_NONE},
            {"category": genai.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, "threshold": genai.types.HarmBlockThreshold.BLOCK_NONE},
        ]

        generation_config_kwargs = {
            'max_output_tokens': 8192,
            'safety_settings': safety_settings
        }

        # --- Tool Loading for Chat ---
        # Start with the native Google Search and URL Context tools enabled by default for chat.
        tools = [
            genai_types.Tool(google_search=genai_types.GoogleSearch()),
            {"url_context": {}},
        ]
        logging.info("Enabled native Google Search and URL Context for chat.")

        # Configure the generation with the combined tools
        if tools:
            generation_config_kwargs['tools'] = tools

        generation_config = genai.types.GenerateContentConfig(**generation_config_kwargs)
        contents = self._prepare_chat_contents(prompt, attachments, history)

        if not contents:
            logging.warning("No content to send to Gemini.")
            return

        try:
            model_name = self.model
            if not model_name.startswith("models/"):
                model_name = f"models/{model_name}"

            response_stream = self.client.models.generate_content_stream(
                model=model_name,
                contents=contents,
                config=generation_config
            )

            from google.protobuf.json_format import MessageToJson
            raw_llm_logger = logging.getLogger('llm_raw_response')

            for chunk in response_stream:
                # Log the raw chunk before processing its parts
                try:
                    # The actual protobuf message is in the _result attribute.
                    # MessageToJson converts it directly to a JSON string.
                    if hasattr(chunk, '_result'):
                        json_str = MessageToJson(chunk._result)
                        # The output is already a JSON string, so we just log it.
                        # For pretty-printing, we can load and dump again, but let's keep it simple.
                        raw_llm_logger.info(json_str)
                    else:
                        # Fallback for unexpected chunk types
                        raw_llm_logger.info(str(chunk))
                except Exception as log_e:
                    raw_llm_logger.warning(f"Could not serialize and log raw chunk: {log_e}")

                if chunk.prompt_feedback and chunk.prompt_feedback.block_reason:
                    reason = chunk.prompt_feedback.block_reason.name
                    raise FatalError(f"Response was blocked by safety filters: {reason}")

                if chunk.parts:
                    for part in chunk.parts:
                        if part.function_call:
                            fc = part.function_call
                            logging.debug(f"Gemini Function Call Name: {fc.name}")
                            logging.debug(f"Gemini Function Call Args: {fc.args}")
                            tool_call_data = {
                                "name": fc.name,
                                "arguments": self._recursive_to_dict(fc.args)
                            }
                            yield {"type": "tool_code", "content": tool_call_data}
                        elif part.text:
                            yield {"type": "text", "content": part.text}

        except google_exceptions.InvalidArgument as e:
            raise FatalError(f"Google API Invalid Argument: {e}") from e
        except google_exceptions.PermissionDenied as e:
            raise FatalError(f"Google API Permission Denied: {e}") from e
        except google_exceptions.ResourceExhausted as e:
            raise FatalError(f"Google API Resource Exhausted (Quota): {e}") from e
        except google_exceptions.InternalServerError as e:
            raise RetryableError(f"Google API Internal Server Error: {e}") from e
        except google_exceptions.ServiceUnavailable as e:
            raise RetryableError(f"Google API Service Unavailable: {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in Gemini provider that will be re-raised: {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"Gemini provider transient error: {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in Gemini provider: {e}") from e

    def cancel_stream(self, generation_id: str = None):
        """
        For Gemini, cancellation is handled by the LLMWorker by stopping iteration.
        This method exists for interface consistency.
        """
        logging.info("Gemini stream cancellation is handled by the worker. No action needed in provider.")
        pass

