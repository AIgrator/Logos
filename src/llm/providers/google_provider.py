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
            # Computer Use requires a Gemini 3.x+ model (2.x has no
            # computer_use tool); 3.8-flash is the documented recommendation.
            "model": "gemini-3.8-flash",
            "require_json_output": True,
            # Native Google Search grounding for chat. OFF by default:
            # grounding is NOT available on the Free tier (429 on every
            # request) and is billed per search on paid tiers.
            "native_web_search": False
        }

    @staticmethod
    def get_ui_schema() -> dict:
        return {
            "free_tier_note": {
                "type": "note",
                "label": (
                    "&#9432;&nbsp; Free-tier API key works for: <b>Chat, Terminal, Browser</b>.<br>"
                    "&#9888;&nbsp; Requires a <b>PAID</b> key: <b>Computer Use (Vision mode)</b> and <b>Native Google Search</b>."
                ),
            },
            "api_key": {"type": "password", "label": "API Key"},
            "model": {"type": "dropdown", "label": "Model", "options_source": "models.json"},
            "require_json_output": {"type": "checkbox", "label": "Require JSON Output"},
            "native_web_search": {
                "type": "checkbox",
                "label": "Native Google Search (web grounding) — needs a PAID key",
                "tooltip": (
                    "When ON, chat uses Gemini's native Google Search grounding "
                    "and can cite web results. WARNING: grounding is billed and is "
                    "NOT available on the Free tier — a free API key will fail with "
                    "429 RESOURCE_EXHAUSTED on every message. When OFF (default), "
                    "chat works on any key and web search is done by the app's own "
                    "web_search / web_fetchs tools instead."
                ),
            },
        }

    @staticmethod
    def get_provider_config() -> dict:
        return {
            "api_url": "https://api.google.com",
            "supports_cancellation": True,
            "supports_streaming_tools": True,
        }

    # ------------------------------------------------------------------
    # Native Computer Use (Interactions API, Gemini 3.x).
    #
    # Official protocol (ai.google.dev/gemini-api/docs/computer-use):
    #   * client.interactions.create(model, input, tools, system_instruction,
    #     previous_interaction_id) — NOT models.generate_content.
    #   * tools: {"type": "computer_use", "environment": "desktop"} (+ our
    #     ask_user custom function tool, same pattern as the docs' HITL
    #     yield_to_user sample).
    #   * coordinates are NORMALIZED 0-999 (x/1000*width); we denormalize to
    #     sent-image pixels using the screenshot size of this turn, so the
    #     handler's _shot_scale keeps working unchanged.
    #   * answers: one function_result per function_call (result carries the
    #     new screenshot image on the last call; safety_acknowledgement rides
    #     inside the result text JSON, per docs).
    #   * safety: safety_decision lives INSIDE the call arguments
    #     ({decision: require_confirmation|blocked, explanation}) — mapped to
    #     pending_safety_checks so the shared approve/replay flow just works.
    #   * done: no function_call steps, only model_output text.
    # ------------------------------------------------------------------

    ASK_USER_TOOL = {
        "type": "function",
        "name": "ask_user",
        "description": (
            "Show the human a modal dialog with your question and wait "
            "for their answer. Use it before any destructive, "
            "irreversible, or transmitting action, and whenever the task "
            "cannot proceed safely without clarification. The human's "
            "answer returns as this call's result; then continue."
        ),
        "parameters": {
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

    def supports_computer_use(self) -> bool:
        return True

    def wants_first_screenshot(self) -> bool:
        # Unlike OpenAI/Anthropic (blind first turn, model asks for a
        # screenshot via its own tool), the documented Google loop always
        # sends an initial screenshot with the task.
        return True

    @staticmethod
    def _field(obj, name, default=None):
        """dict-or-object attribute access (SDK pydantic or plain dicts)."""
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @staticmethod
    def _sent_image_size(screenshot_b64: str | None) -> tuple | None:
        """Width/height of the PNG the model saw (for 0-999 denormalizing)."""
        if not screenshot_b64:
            return None
        try:
            import base64
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64)))
            return (img.size[0], img.size[1])
        except Exception:
            return None

    def _denorm(self, x, y, size: tuple | None) -> tuple:
        """Normalized 0-999 -> sent-image pixels (ints, clamped)."""
        if size is None:
            raise ValueError(
                "Model emitted coordinates before any screenshot was sent; "
                "send a screenshot first (take_screenshot).")
        w, h = size
        px = min(max(int(round(float(x) / 1000 * w)), 0), w - 1)
        py = min(max(int(round(float(y) / 1000 * h)), 0), h - 1)
        return (px, py)

    def _predefined_to_actions(self, name: str, args: dict,
                               size: tuple | None,
                               record_chain: str | None,
                               lookup_chain: str | None) -> list:
        """One Google desktop predefined function -> our action dicts."""
        import time as _time
        actions: list = []
        intent = args.get("intent")

        def _emit(action: dict) -> dict:
            if intent:
                action["_intent"] = str(intent)[:300]
            actions.append(action)
            return action

        if name in ("click", "double_click", "triple_click"):
            x, y = self._denorm(args["x"], args["y"], size)
            kind = {"click": "click", "double_click": "double_click",
                    "triple_click": "triple_click"}[name]
            _emit({"type": kind, "x": x, "y": y, "button": "left"})
        elif name == "middle_click":
            x, y = self._denorm(args["x"], args["y"], size)
            _emit({"type": "click", "x": x, "y": y, "button": "middle"})
        elif name == "right_click":
            x, y = self._denorm(args["x"], args["y"], size)
            _emit({"type": "click", "x": x, "y": y, "button": "right"})
        elif name == "move":
            x, y = self._denorm(args["x"], args["y"], size)
            _emit({"type": "move", "x": x, "y": y})
        elif name in ("mouse_down", "mouse_up"):
            # Our down/up take no coordinates: move there first.
            x, y = self._denorm(args["x"], args["y"], size)
            _emit({"type": "move", "x": x, "y": y})
            _emit({"type": "left_mouse_down" if name == "mouse_down"
                   else "left_mouse_up"})
        elif name == "drag_and_drop":
            x0, y0 = self._denorm(args["start_x"], args["start_y"], size)
            x1, y1 = self._denorm(args["end_x"], args["end_y"], size)
            _emit({"type": "drag",
                   "path": [{"x": x0, "y": y0}, {"x": x1, "y": y1}]})
        elif name == "scroll":
            x, y = self._denorm(args.get("x", 500), args.get("y", 500), size)
            try:
                magnitude = int(args.get("magnitude_in_pixels", 300) or 300)
            except Exception:
                magnitude = 300
            direction = str(args.get("direction", "down")).lower()
            sx, sy = 0, 0
            if direction == "up":
                sy = -magnitude
            elif direction == "down":
                sy = magnitude
            elif direction == "left":
                sx = -magnitude
            elif direction == "right":
                sx = magnitude
            else:
                raise ValueError(f"Unknown scroll direction: {direction!r}")
            _emit({"type": "scroll", "x": x, "y": y,
                   "scroll_x": sx, "scroll_y": sy})
        elif name == "type":
            text = args.get("text", "")
            if text is None:
                raise ValueError("type without text")
            if "x" in args and "y" in args:
                # Focus-then-type (docs' browser sample pattern).
                x, y = self._denorm(args["x"], args["y"], size)
                _emit({"type": "click", "x": x, "y": y, "button": "left"})
            _emit({"type": "type", "text": str(text)})
            if args.get("press_enter"):
                _emit({"type": "keypress", "keys": ["enter"]})
        elif name == "press_key":
            key = args.get("key")
            if not key:
                raise ValueError("press_key without key")
            _emit({"type": "keypress", "keys": [str(key)]})
        elif name == "hotkey":
            keys = args.get("keys") or []
            if not keys:
                raise ValueError("hotkey without keys")
            _emit({"type": "keypress", "keys": [str(k) for k in keys]})
        elif name == "key_down":
            # Held-key tracking per chain: key_up converts the wall-clock
            # hold into our hold_key(duration). key_down itself emits a
            # harmless wait so the turn stays alive.
            key = str(args.get("key", ""))
            if not key:
                raise ValueError("key_down without key")
            self._prune_held_keys()
            self._held_keys[(record_chain, key.lower())] = _time.time()
            _emit({"type": "wait", "seconds": 0.5})
        elif name == "key_up":
            key = str(args.get("key", ""))
            if not key:
                raise ValueError("key_up without key")
            started = self._held_keys.pop((lookup_chain, key.lower()), None)
            if started is None:
                _emit({"type": "keypress", "keys": [key]})
            else:
                duration = min(max(_time.time() - started, 0.2), 30.0)
                _emit({"type": "hold_key", "keys": [key],
                       "duration": round(duration, 1)})
        elif name == "wait":
            try:
                seconds = float(args.get("seconds", 1.0))
            except Exception:
                seconds = 1.0
            _emit({"type": "wait", "seconds": seconds})
        elif name == "take_screenshot":
            _emit({"type": "screenshot"})
        else:
            raise ValueError(f"Unknown Google computer function '{name}'.")
        return actions

    def _prune_held_keys(self) -> None:
        """Bound the key_down registry (stale entries die after 10 min)."""
        import time as _time
        try:
            now = _time.time()
            old = [k for k, t in self._held_keys.items() if now - t > 600]
            for k in old:
                del self._held_keys[k]
        except Exception:
            pass

    def computer_use_turn(
        self,
        task: str | None = None,
        outputs: list | None = None,
        previous_response_id: str | None = None,
        instructions: str | None = None,
        screenshot_b64: str | None = None,
    ) -> dict:
        """
        One Computer Use turn via the Interactions API.

        First turn: `task` (+ `screenshot_b64`, injected by the worker) ->
        one function_call (or done text). Follow-up turns:
        `previous_response_id` (= prior interaction id) + `outputs` answered
        from the previous turn. Each computer output becomes one
        function_result carrying the fresh screenshot on the last call;
        ask_user answers ride as plain-text function results.

        Returns the shared contract: {"actions", "call_id", "call_ids",
        "response_id", "pending_safety_checks", "ask", "done_text"}.
        """
        from google.genai.errors import APIError as _GenAIAPIError
        try:
            tools = [
                {"type": "computer_use",
                 "environment": "desktop",
                 "enable_prompt_injection_detection": True},
                dict(self.ASK_USER_TOOL),
            ]

            sent_size = self._sent_image_size(screenshot_b64)
            if previous_response_id is None:
                if not task:
                    raise ValueError("First computer-use turn requires a task.")
                initial: list = [{"type": "text", "text": task}]
                if screenshot_b64:
                    initial.append({"type": "image", "data": screenshot_b64,
                                    "mime_type": "image/png"})
                else:
                    logging.warning(
                        "Google computer-use first turn without screenshot; "
                        "the model will likely call take_screenshot first.")
                create_kwargs: dict = {
                    "model": self.model,
                    "input": initial,
                    "tools": tools,
                    "timeout": 90,
                }
                if instructions:
                    create_kwargs["system_instruction"] = instructions
                interaction = self.client.interactions.create(**create_kwargs)
            else:
                if not outputs:
                    raise ValueError("Follow-up turn requires outputs.")
                ack_by_call = {
                    o.get("call_id") for o in outputs
                    if o.get("kind") == "computer"
                    and o.get("acknowledged_safety_checks")
                }
                # The model saw this screenshot last: prefer the fresh post
                # screenshot, else a per-call result image, else the opening
                # shot (coordinates always denormalize against what it saw).
                for out in outputs:
                    if out.get("kind") != "computer":
                        continue
                    img = (out.get("screenshot_b64")
                           or out.get("result_image_b64"))
                    if img:
                        sent_size = self._sent_image_size(img) or sent_size
                follow: list = []
                for out in outputs:
                    cid = out.get("call_id")
                    if out.get("kind") != "computer":
                        follow.append({
                            "type": "function_result",
                            "call_id": cid,
                            "name": "ask_user",
                            "result": out.get("output_text", ""),
                        })
                        continue
                    result_text = {"status": out.get("result_status") or "ok"}
                    if out.get("result_text"):
                        result_text["output"] = out["result_text"]
                    if cid in ack_by_call:
                        result_text["safety_acknowledgement"] = True
                    parts: list = [{"type": "text",
                                    "text": json.dumps(result_text)}]
                    # Per-call own image (zoom/screenshot actions) plus the
                    # fresh post screenshot when both ride one output.
                    for key in ("result_image_b64", "screenshot_b64"):
                        if out.get(key):
                            parts.append({"type": "image",
                                          "data": out[key],
                                          "mime_type": "image/png"})
                    follow.append({
                        "type": "function_result",
                        "call_id": cid,
                        "result": parts,
                    })
                create_kwargs = {
                    "model": self.model,
                    "previous_interaction_id": previous_response_id,
                    "input": follow,
                    "tools": tools,
                    "timeout": 90,
                }
                if instructions:
                    create_kwargs["system_instruction"] = instructions
                interaction = self.client.interactions.create(**create_kwargs)

            self._record_usage(self._field(interaction, "usage"))
            chain_id = (self._field(interaction, "id")
                        or previous_response_id)
            steps = self._field(interaction, "steps") or []
            errors = self._field(interaction, "errors") or []
            if not steps and errors:
                raise FatalError(
                    f"Google interaction failed: {errors!r}"[:500])

            actions: list = []
            call_ids: list = []
            out_call_id = None
            safety: list = []
            asks: list = []
            done_parts: list = []
            for step in steps:
                stype = self._field(step, "type", "")
                if stype == "model_output":
                    for block in self._field(step, "content") or []:
                        if self._field(block, "type") == "text":
                            done_parts.append(
                                self._field(block, "text", "") or "")
                    continue
                if stype != "function_call":
                    continue  # thought / unknown steps never crash a turn
                fname = self._field(step, "name", "")
                fargs = self._field(step, "arguments") or {}
                if isinstance(fargs, str):
                    try:
                        fargs = json.loads(fargs or "{}")
                    except Exception:
                        fargs = {}
                fid = self._field(step, "id")
                if fname == "ask_user":
                    asks.append({
                        "call_id": fid,
                        "question": fargs.get("question",
                                             fargs.get("reason", "")),
                    })
                    continue
                call_ids.append(fid)
                out_call_id = fid
                # Safety decision rides INSIDE the call arguments.
                decision = (fargs.get("safety_decision") or {})
                code = decision.get("decision")
                if code in ("require_confirmation", "blocked"):
                    safety.append({
                        "id": fid,
                        "code": code,
                        "message": decision.get("explanation", ""),
                    })
                actions.extend(self._predefined_to_actions(
                    fname, fargs, sent_size,
                    # Held-key registry: key_down records under THIS turn's
                    # interaction id; key_up looks up under the incoming
                    # previous_response_id (== the id of the turn where the
                    # key went down). First turns use the fresh id both ways.
                    chain_id, previous_response_id or chain_id))

            done_text = "".join(done_parts).strip() or None
            if done_text is None:
                fallback = self._field(interaction, "output_text")
                if isinstance(fallback, str) and fallback.strip():
                    done_text = fallback.strip()
            return {
                "actions": actions,
                "call_id": out_call_id,
                "call_ids": call_ids,
                "response_id": chain_id,
                "pending_safety_checks": safety,
                "ask": asks,
                "done_text": done_text,
            }
        except _GenAIAPIError as e:
            code = getattr(e, "code", None)
            if code == 429 or (isinstance(code, int) and 500 <= code < 600):
                raise RetryableError(
                    f"Google computer-use transient error ({code}): {e}",
                    retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(
                f"Google computer-use API error ({code}): {e}") from e
        except (FatalError, RetryableError):
            raise
        except Exception as e:
            logging.error(
                "Unexpected error in Google computer-use turn: %s",
                e, exc_info=True)
            if is_transient_error(e):
                raise RetryableError(
                    f"Google computer-use transient error: {e}",
                    retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(
                f"Unexpected error in Google computer-use turn: {e}") from e

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)

        if not self.api_key:
            raise ValueError("Gemini API key is not configured in settings.")

        self.client = genai.Client(api_key=self.api_key)
        # key_down registry for computer use: (chain_id, key) -> timestamp.
        # key_up converts the wall-clock hold into hold_key(duration).
        self._held_keys: dict = {}

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

    def _load_strict_schema(self, schema_name: str):
        """Load the strict (constrained-decoding) schema for a response format."""
        if not schema_name:
            return None
        for file_path in (
            'cache/terminal/response_formats.json',
            'cache/vision/response_formats.json',
            'cache/browser/response_formats.json',
        ):
            try:
                with open(file_path, encoding='utf-8') as f:
                    schemas = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            except Exception as e:
                logging.error(f"Failed to load '{file_path}' for strict schema: {e}")
                continue
            info = schemas.get(schema_name)
            if info and info.get("strict_schema"):
                return info["strict_schema"]
        return None

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
            strict_schema = self._load_strict_schema(schema_name)
            if strict_schema:
                from src.computer.schemas.strict import to_google_schema
                generation_config_kwargs['response_schema'] = to_google_schema(strict_schema)
                logging.info("Gemini Provider: Enforcing strict response_schema.")
            contents = self._prepare_assistant_contents(prompt, attachments, history)
        else:
            logging.info("Gemini Provider: Using legacy JSON-in-text mode.")
            if system_prompt:
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
            self._record_usage(getattr(response, "usage_metadata", None))

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

        # --- Tools for Chat ---
        # Native Google Search grounding is opt-in (settings: native_web_search).
        # It is gated: NOT available on the Free tier, so a free key gets
        # 429 RESOURCE_EXHAUSTED on every request that carries the tool, and on
        # paid tiers it is billed per search. When the toggle is OFF (default)
        # no built-in tools are attached and chat web search goes through the
        # app's own web_search / web_fetchs function tools instead.
        if self.provider_config.get("native_web_search", False):
            generation_config_kwargs['tools'] = [
                genai_types.Tool(google_search=genai_types.GoogleSearch()),
                {"url_context": {}},
            ]

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

                self._record_usage(
                    getattr(chunk, "usage_metadata", None), merge=True)

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

