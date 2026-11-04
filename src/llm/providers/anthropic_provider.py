import base64
import io
import json
import logging
import warnings
import mimetypes
import pathlib
from typing import Any
from PIL import Image
from ..exceptions import FatalError, RetryableError, extract_retry_after, is_transient_error
from ..exceptions import RETRYABLE_STATUS_CODES as _RETRYABLE_CODES
from ..llm_parsing import repair_json_string
from anthropic import Anthropic, APIError, AuthenticationError, RateLimitError, APIConnectionError, APITimeoutError, InternalServerError
from ..base_provider import BaseLLMProvider
logging.getLogger('anthropic._base_client').setLevel(logging.INFO)
logger = logging.getLogger(__name__)

def image_to_anthropic_format(attachment):
    """Converts an image from a file path or bytes into Anthropic's format."""
    try:
        if isinstance(attachment, str):
            file_uri = pathlib.Path(attachment).as_uri()
            media_type, _ = mimetypes.guess_type(file_uri)
            if not media_type:
                media_type = "application/octet-stream"
            with open(attachment, "rb") as image_file:
                data = base64.b64encode(image_file.read()).decode('utf-8')
        elif isinstance(attachment, bytes):
            img = Image.open(io.BytesIO(attachment))
            buffer = io.BytesIO()
            img_format = img.format or 'PNG'
            img.save(buffer, format=img_format)
            data = base64.b64encode(buffer.getvalue()).decode('utf-8')
            media_type = f"image/{img_format.lower()}"
        else:
            logger.error(f"Unsupported attachment type: {type(attachment)}")
            return None

        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }
    except Exception as e:
        logger.error(f"Error processing image attachment for Anthropic: {e}", exc_info=True)
        return None

class AnthropicProvider(BaseLLMProvider):
    @staticmethod
    def get_provider_id() -> str:
        return "anthropic"

    @staticmethod
    def get_display_name() -> str:
        return "Anthropic"

    @staticmethod
    def get_default_settings() -> dict:
        return {
            "model": "claude-sonnet-4-5",
            "max_tokens": 4096,
            "require_json_output": True
        }

    @staticmethod
    def get_ui_schema() -> dict:
        return {
            "api_key": {"type": "password", "label": "API Key"},
            "model": {"type": "dropdown", "label": "Model", "options_source": "models.json"},
            "max_tokens": {"type": "string", "label": "Max Tokens"},
            "require_json_output": {"type": "checkbox", "label": "Require JSON Output"}
        }

    @staticmethod
    def get_provider_config() -> dict:
        return {
            "api_url": "https://api.anthropic.com",
            "supports_cancellation": True,
            "supports_streaming_tools": True
        }

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        self._tools_cache = None
        self.current_stream = None
        self.is_cancelling = False
        self.client = Anthropic(
            api_key=self.api_key,
            base_url=self.provider_config.get("api_url")
        )
        # Server-side conversation chains (Anthropic has no response chaining:
        # the full message history travels with every call). Keyed by the
        # chain id we hand out as response_id. Bounded; oldest dropped.
        self._cua_chains: dict = {}

    COMPUTER_TOOLSET = {"type": "computer_toolset_20260801"}
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
    LAUNCH_APP_TOOL = {
        "name": "launch_app",
        "description": (
            "Launch an installed application by its exact name. Use it "
            "whenever the task needs an app that is not already on screen "
            "instead of searching the Start menu yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": (
                        "The exact application name from the Available "
                        "applications list."),
                }
            },
            "required": ["app_name"],
        },
    }
    MAX_CUA_CHAINS = 32

    # computer_toolset members -> our executor action types. All 17 covered
    # (the 6 Anthropic-only extras live in the shared executor — OpenAI never
    # emits them, so no per-provider gating is needed).
    _MEMBER_MAP = {
        "screenshot": "screenshot",
        "zoom": "zoom",
        "left_click": ("click", "left"),
        "right_click": ("click", "right"),
        "middle_click": ("click", "middle"),
        "double_click": "double_click",
        "triple_click": "triple_click",
        "left_click_drag": "drag",
        "mouse_move": "move",
        "cursor_position": "cursor_position",
        "left_mouse_down": "left_mouse_down",
        "left_mouse_up": "left_mouse_up",
        "scroll": "scroll",
        "type": "type",
        "key": "keypress",
        "hold_key": "hold_key",
        "wait": "wait",
    }

    def supports_computer_use(self) -> bool:
        return True

    def _launch_app_tool(self, launch_apps) -> dict:
        return {
            **self.LAUNCH_APP_TOOL,
            "input_schema": {
                **self.LAUNCH_APP_TOOL["input_schema"],
                "properties": {
                    **self.LAUNCH_APP_TOOL["input_schema"]["properties"],
                    "app_name": {
                        **self.LAUNCH_APP_TOOL["input_schema"]["properties"]["app_name"],
                        "enum": [str(name) for name in launch_apps],
                    },
                },
            },
        }

    def _effort_kwargs(self) -> dict:
        """Adaptive thinking + effort level, or nothing when the model has no
        effort control. Forced tool choice is incompatible with thinking, so
        callers on that path must not use this."""
        if not self.reasoning_effort:
            return {}
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.reasoning_effort},
        }

    def _thinking_max_tokens(self, max_tokens: int) -> int:
        """Adaptive thinking shares max_tokens with the answer; keep a floor
        so the default 4096 cannot be spent entirely on thinking."""
        if self.reasoning_effort:
            return max(int(max_tokens), 8192)
        return max_tokens

    def computer_use_turn(
        self,
        task: str | None = None,
        outputs: list | None = None,
        previous_response_id: str | None = None,
        instructions: str | None = None,
        launch_apps: list[str] | None = None,
    ) -> dict:
        """
        One computer-use turn via Messages API (non-streaming; the CUA
        worker already runs off the GUI thread — streaming buys nothing).

        Tools sent: the `computer_toolset_20260801` client toolset plus
        the `ask_user` custom tool (same array, per docs). Member calls
        arrive as tool_use blocks with toolset_name "computer"; answers go
        back as tool_result blocks echoing toolset_name. Coordinates are
        screenshot pixels, like OpenAI. A non-empty `launch_apps` list adds
        the `launch_app` custom tool restricted to those names.

        One action per turn (disable_parallel_tool_use): matches the
        worker's single-call output shape, no outbox rework needed.
        """
        from anthropic import (
            AuthenticationError, RateLimitError, InternalServerError,
            APIConnectionError, APITimeoutError, APIError,
        )
        try:
            try:
                max_tokens = int(self.provider_config.get("max_tokens", 4096))
            except (ValueError, TypeError):
                max_tokens = 4096
            max_tokens = self._thinking_max_tokens(max_tokens)
            tools = [dict(self.COMPUTER_TOOLSET), dict(self.ASK_USER_TOOL)]
            if launch_apps:
                tools.append(self._launch_app_tool(launch_apps))
            # Batches allowed (like OpenAI): the worker answers every
            # block id, so multi-action turns stay chain-valid.
            tool_choice = {"type": "auto"}

            if previous_response_id is None:
                if not task:
                    raise ValueError("First computer-use turn requires a task.")
                import uuid
                chain_id = uuid.uuid4().hex[:12]
                messages: list = [{"role": "user", "content": [
                    {"type": "text", "text": task}]}]
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
                    # then the post screenshot (last item only); else OK.
                    # (Never an empty image — the API rejects it.)
                    if out.get("result_image_b64"):
                        content = [{
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": out["result_image_b64"],
                            }}]
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
                            }}]
                    else:
                        content = [{"type": "text", "text": "OK"}]
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": out.get("call_id"),
                        "toolset_name": "computer",
                        "content": content,
                    })
                messages.append({"role": "user", "content": results})

            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                system=system,
                messages=messages,
                timeout=90,
                **self._effort_kwargs(),
            )
            self._record_usage(getattr(response, "usage", None))
            messages.append({"role": "assistant", "content": response.content})

            actions: list = []
            out_call_id = None
            all_ids: list = []
            asks: list = []
            launches: list = []
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
                        import json as _json
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
                elif name == "launch_app" and not tset:
                    launches.append({
                        "call_id": bid,
                        "app_name": block_input.get("app_name", ""),
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
                "launch": launches,
                "done_text": done_text,
            }
        except (AuthenticationError, RateLimitError) as e:
            raise FatalError(f"Anthropic computer-use auth/quota error: {e}") from e
        except (APIConnectionError, APITimeoutError, InternalServerError) as e:
            raise RetryableError(f"Anthropic computer-use transient error: {e}") from e
        except APIError as e:
            _code = getattr(e, "status_code", None)
            if _code in _RETRYABLE_CODES:
                raise RetryableError(
                    f"Anthropic computer-use transient API error ({_code}): {e}",
                    retry_after=extract_retry_after(
                        response=getattr(e, "response", None),
                        message=str(e))) from e
            raise FatalError(f"Anthropic computer-use API error: {e}") from e

    @staticmethod
    def _member_to_action(name: str, mapped, block_input: dict) -> dict:
        """Converts one computer_toolset member call to our action format."""
        if mapped == "screenshot":
            return {"type": "screenshot"}
        if mapped == "zoom":
            region = block_input.get("region") or [0, 0, 100, 100]
            return {"type": "zoom", "region": list(region)[:4]}
        if mapped == "cursor_position":
            return {"type": "cursor_position"}
        if mapped == "left_mouse_down":
            return {"type": "left_mouse_down"}
        if mapped == "left_mouse_up":
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
            direction = str(block_input.get("scroll_direction", "down"))
            try:
                amount = int(block_input.get("scroll_amount", 3) or 3)
            except Exception:
                amount = 3
            step = amount * 120
            sx, sy = 0, 0
            if direction == "up":
                sy = -step
            elif direction == "down":
                sy = step
            elif direction == "left":
                sx = -step
            elif direction == "right":
                sx = step
            return {"type": "scroll", "x": coord[0], "y": coord[1],
                    "scroll_x": sx, "scroll_y": sy}
        if mapped == "type":
            return {"type": "type", "text": block_input.get("text", "")}
        if mapped == "keypress":
            raw = str(block_input.get("text", ""))
            keys = [k for k in raw.split("+") if k] or [raw]
            return {"type": "keypress", "keys": keys}
        if mapped == "wait":
            return {"type": "wait"}
        # click family: ("click", button) tuples or "double_click".
        button = mapped[1] if isinstance(mapped, tuple) else "left"
        coord = block_input.get("coordinate") or [0, 0]
        keys = []
        mods = block_input.get("text")
        if mods:
            keys = [k for k in str(mods).split("+") if k]
        action = {"type": "click" if "double" not in name else "double_click",
                  "x": coord[0], "y": coord[1], "button": button or "left"}
        if keys:
            action["keys"] = keys
        return action

    # Anthropic compiles strict tool schemas into a grammar with a hard size
    # budget: a single response envelope containing more than ~12 tool branches
    # (and any request with too many strict tools) is rejected with a 400. When
    # the union is too large we fall back to the unconstrained schema instead of
    # failing the request.
    ANTHROPIC_STRICT_MAX_BRANCHES = 12

    @classmethod
    def _strict_grammar_fits(cls, strict_schema: dict) -> bool:
        try:
            branches = (
                strict_schema["properties"]["payload"]["properties"]
                ["actions"]["items"]["anyOf"]
            )
            return len(branches) <= cls.ANTHROPIC_STRICT_MAX_BRANCHES
        except Exception:
            return False

    def _get_tools_from_cache(self):
        from src.llm import tool_catalog

        stamp = tool_catalog.catalogue_stamp()
        if self._tools_cache is not None and getattr(self, "_tools_stamp", None) == stamp:
            return self._tools_cache

        all_schemas = tool_catalog.load_schemas()

        if not all_schemas:
            logger.error("No tool schemas could be loaded. Please ensure the files under 'cache/' exist and are valid.")
            return []

        formatted_tools = []
        for name, schema_info in all_schemas.items():
            strict_schema = schema_info.get("strict_schema")
            flat_schema = schema_info.get("strict_schema_flat")
            params = strict_schema or schema_info.get("schema")
            if params:
                strict_ok = False
                from src.computer.schemas.strict import to_anthropic_schema
                if strict_schema and self._strict_grammar_fits(strict_schema):
                    # Anthropic rejects the OpenAI `["T","null"]` optional idiom.
                    params = to_anthropic_schema(strict_schema)
                    strict_ok = True
                elif flat_schema:
                    # The full union is too large for Anthropic's grammar budget
                    # (e.g. browser/terminal: 17/23 branches). Fall back to the
                    # single-branch action schema, which fits and keeps tool use
                    # grammar-constrained instead of degrading to unconstrained.
                    params = to_anthropic_schema(flat_schema)
                    strict_ok = True
                # Anthropic uses 'input_schema' instead of 'parameters'
                tool = {
                    "name": name,
                    "description": schema_info.get("description", ""),
                    "input_schema": params
                }
                if strict_ok:
                    # Grammar-constrained tool use: model must match the schema.
                    tool["strict"] = True
                formatted_tools.append(tool)
        self._tools_stamp = stamp
        self._tools_cache = formatted_tools
        return formatted_tools

    def _prepare_messages(self, prompt: str, attachments: list, history: list = None):
        messages = []
        if history:
            # Avoid duplicating the last message if it's the same as the current prompt
            if history and history[-1].get("content") == prompt:
                history = history[:-1]

            for message in history:
                role = message.get("role")
                if role == "model":
                    role = "assistant"
                if role in ["user", "assistant"]:
                    content = message.get("content", "")
                    # Anthropic's API requires content to be a list of blocks.
                    if isinstance(content, str):
                        content = [{"type": "text", "text": content}]
                    messages.append({"role": role, "content": content})

        user_content = []
        if attachments:
            for attachment in attachments:
                image_part = image_to_anthropic_format(attachment)
                if image_part:
                    user_content.append(image_part)

        user_content.append({"type": "text", "text": prompt})

        messages.append({"role": "user", "content": user_content})
        return messages

    def _execute_get_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        schema_name: str | None = None
    ) -> dict[str, Any]:
        messages = self._prepare_messages(prompt, attachments, history)
        try:
            max_tokens = int(self.provider_config.get("max_tokens", 4096))
        except (ValueError, TypeError):
            max_tokens = 4096

        request_body = {
            "model": self.model,
            "messages": messages,
            "system": system_prompt,
            "max_tokens": max_tokens,
        }

        use_forced_tool = schema_name and self.require_json_output

        if use_forced_tool:
            logger.debug(f"Attempting to force tool call for schema: {schema_name}")
            all_tools = self._get_tools_from_cache()
            target_tool = next((t for t in all_tools if t.get("name") == schema_name), None)

            if target_tool:
                request_body["tools"] = [target_tool]
                request_body["tool_choice"] = {"type": "tool", "name": schema_name}
            else:
                logger.warning(f"Schema '{schema_name}' requested but not found. Proceeding without forced tool.")

        if not use_forced_tool:
            # Adaptive thinking cannot be combined with a forced tool call.
            request_body.update(self._effort_kwargs())
            request_body["max_tokens"] = self._thinking_max_tokens(
                request_body["max_tokens"])

        try:
            response = self.client.messages.create(**request_body)
            self._record_usage(getattr(response, "usage", None))

            text_content = ""

            if response.content:
                for block in response.content:
                    if block.type == 'text':
                        text_content += block.text
                    elif block.type == 'tool_use' and use_forced_tool:
                        final_payload = block.input

                        # Peel off wrapper layers until we reach the real payload.
                        # A wrapper is either a dict like
                        #   {"type": "<tool>", "payload": {...}}  (or {"payload": {...}})
                        # or the payload itself encoded as a JSON string. Models
                        # (especially on large payloads) may double-encode, so we
                        # alternate between unwrapping dicts and decoding strings.
                        for _ in range(10):
                            if isinstance(final_payload, dict) and 'payload' in final_payload and (
                                len(final_payload.keys()) == 1 or final_payload.get('type') == block.name
                            ):
                                logger.info(f"Unwrapping nested payload for tool '{block.name}'.")
                                final_payload = final_payload.get('payload')
                                continue
                            if isinstance(final_payload, str):
                                decoded = None
                                try:
                                    decoded = json.loads(final_payload)
                                except (json.JSONDecodeError, TypeError):
                                    decoded = repair_json_string(final_payload)
                                if decoded is None:
                                    break
                                final_payload = decoded
                                logger.info(f"Decoded JSON string payload for tool '{block.name}'.")
                                continue
                            break

                        reconstructed_object = {
                            "type": block.name,
                            "payload": final_payload
                        }
                        return {"text": text_content, "tool_calls": reconstructed_object}

            # Fallback for non-tool responses or if the tool response was malformed
            return {"text": text_content, "tool_calls": []}

        except AuthenticationError as e:
            raise FatalError(f"Anthropic authentication error: {e}") from e
        except RateLimitError as e:
            raise FatalError(f"Anthropic rate limit/quota exhausted: {e}") from e
        except APIConnectionError as e:
            raise RetryableError(f"Anthropic connection error: {e}") from e
        except APITimeoutError as e:
            raise RetryableError(f"Anthropic timeout error: {e}") from e
        except InternalServerError as e:
            raise RetryableError(f"Anthropic internal server error: {e}") from e
        except APIError as e:  # General API errors; 5xx (incl. 529 overload) are transient, 429 is quota
            _code = getattr(e, 'status_code', None)
            if _code in _RETRYABLE_CODES:
                raise RetryableError(f"Anthropic transient API error ({_code}): {e}", retry_after=extract_retry_after(response=getattr(e, 'response', None), message=str(e))) from e
            raise FatalError(f"Anthropic API error: {e}") from e
        except Exception as e:
            logger.error(f"An unexpected error occurred in Anthropic provider (get_response): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"Anthropic transient error (get_response): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in Anthropic provider: {e}") from e

    def cancel_stream(self, generation_id: str = None):
        """Cancels the current active stream by closing the connection."""
        if self.current_stream:
            logger.info("Attempting to cancel Anthropic stream.")
            try:
                self.is_cancelling = True
                self.current_stream.close()
                logger.info("Successfully closed Anthropic stream.")
            except Exception as e:
                logger.error(f"Error closing Anthropic stream: {e}", exc_info=True)

    def web_search(self, query: str) -> str | None:
        """
        Performs a native web search using Anthropic's server-side
        `web_search` tool and returns the model's synthesized answer.

        Terminal mode calls this as "Plan A" before falling back to the
        app's local search implementation ("Plan B").
        """
        try:
            max_tokens = int(self.provider_config.get("max_tokens", 4096))
        except (ValueError, TypeError):
            max_tokens = 4096

        logger.info("Performing native web search via Anthropic web_search server tool.")
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": query}],
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 3
                    },
                    {
                        "type": "web_fetch_20250910",
                        "name": "web_fetch",
                        "max_uses": 5,
                        "citations": {"enabled": True}
                    }
                ],
                extra_headers={
                    "anthropic-beta": "web-fetch-2025-09-10"
                }
            )

            text_parts = [
                block.text
                for block in (message.content or [])
                if getattr(block, "type", None) == "text" and getattr(block, "text", None)
            ]
            text_content = "\n".join(text_parts).strip()
            return text_content or None

        except AuthenticationError as e:
            raise FatalError(f"Anthropic authentication error during web search: {e}") from e
        except RateLimitError as e:
            raise FatalError(f"Anthropic rate limit/quota exhausted during web search: {e}") from e
        except APIConnectionError as e:
            raise RetryableError(f"Anthropic connection error during web search: {e}") from e
        except APITimeoutError as e:
            raise RetryableError(f"Anthropic timeout error during web search: {e}") from e
        except InternalServerError as e:
            raise RetryableError(f"Anthropic internal server error during web search: {e}") from e
        except APIError as e:  # General API errors; 5xx (incl. 529 overload) are transient, 429 is quota
            _code = getattr(e, 'status_code', None)
            if _code in _RETRYABLE_CODES:
                raise RetryableError(f"Anthropic transient API error ({_code}) during web search: {e}", retry_after=extract_retry_after(response=getattr(e, 'response', None), message=str(e))) from e
            raise FatalError(f"Anthropic API error during web search: {e}") from e
        except Exception as e:
            logger.error(f"An unexpected error occurred in Anthropic provider (web_search): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"Anthropic transient error (web_search): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in Anthropic provider (web_search): {e}") from e

    def stream_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        callbacks=None
    ):
        # Suppress the specific Pydantic UserWarning from the Anthropic library
        warnings.filterwarnings(
            "ignore",
            category=UserWarning,
            message="Pydantic serializer warnings.*"
        )
        from collections import defaultdict
        messages = self._prepare_messages(prompt, attachments, history)
        try:
            max_tokens = int(self.provider_config.get("max_tokens", 4096))
        except (ValueError, TypeError):
            max_tokens = 4096
        max_tokens = self._thinking_max_tokens(max_tokens)
        self.is_cancelling = False
        raw_llm_logger = logging.getLogger('llm_raw_response')

        try:
            with self.client.messages.stream(
                model=self.model,
                messages=messages,
                system=system_prompt,
                max_tokens=max_tokens,
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 3
                    },
                    {
                        "type": "web_fetch_20250910",
                        "name": "web_fetch",
                        "max_uses": 5,
                        "citations": {"enabled": True}
                    }
                ],
                extra_headers={
                    "anthropic-beta": "web-fetch-2025-09-10"
                },
                **self._effort_kwargs(),
            ) as stream:
                self.current_stream = stream
                tool_calls = defaultdict(lambda: {"name": "", "arguments": ""})

                for event in stream:
                    try:
                        if hasattr(event, 'model_dump'):
                            event_dict = event.model_dump()
                        elif hasattr(event, 'dict'):
                            event_dict = event.dict()
                        else:
                            event_dict = {'type': event.type, 'data': str(event)}
                        raw_llm_logger.info(json.dumps(event_dict, default=str, ensure_ascii=False))
                    except Exception as log_e:
                        raw_llm_logger.warning(f"Could not serialize and log raw Anthropic event: {log_e}")
                        try:
                            raw_llm_logger.info(f"Event type: {event.type}, Data: {str(event)}")
                        except Exception:
                            pass

                    if self.is_cancelling:
                        logger.info("Stream gracefully cancelled by user request (Anthropic).")
                        break

                    if event.type == "message_start":
                        usage = getattr(getattr(event, "message", None), "usage", None)
                        self._record_usage(usage, merge=True)

                    elif event.type == "content_block_start":
                        if event.content_block.type in ["tool_use", "server_tool_use"]:
                            tool_calls[event.index]["name"] = event.content_block.name
                            logger.info(f"🔧 Tool use started: {event.content_block.name} at index {event.index}")

                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield {"type": "text", "content": event.delta.text}
                        elif event.delta.type == "input_json_delta":
                            tool_calls[event.index]["arguments"] += event.delta.partial_json

                    elif event.type == "content_block_stop":
                        if event.index in tool_calls:
                            tool_call = tool_calls[event.index]
                            logger.info(f"✅ Tool use finished for: {tool_call['name']}. Assembled arguments: {tool_call['arguments']}")
                            try:
                                parsed_args = json.loads(tool_call['arguments'])

                                # Map Anthropic tool names to internal tool names
                                tool_name = tool_call['name']
                                if tool_name == 'web_fetch':
                                    tool_name = 'web_fetchs'
                                elif tool_name and tool_name.startswith('web_search'):
                                    tool_name = 'web_search'

                                final_tool_call = {
                                    "name": tool_name,
                                    "arguments": parsed_args
                                }
                                yield {"type": "tool_code", "content": final_tool_call}
                            except json.JSONDecodeError:
                                logger.error(f"Failed to parse JSON for tool {tool_call['name']}: {tool_call['arguments']}")
                            del tool_calls[event.index]

                    elif event.type == "message_delta":
                        if hasattr(event.delta, 'stop_reason') and event.delta.stop_reason:
                            logger.info(f"⏹️ Stop reason: {event.delta.stop_reason}")
                        usage = getattr(event, 'usage', None)
                        if usage is not None:
                            self._record_usage(usage, merge=True)
                            logger.info(f"📊 Usage: {usage}")

        except AuthenticationError as e:
            raise FatalError(f"Anthropic authentication error: {e}") from e
        except RateLimitError as e:
            raise FatalError(f"Anthropic rate limit/quota exhausted: {e}") from e
        except APIConnectionError as e:
            raise RetryableError(f"Anthropic connection error: {e}") from e
        except APITimeoutError as e:
            raise RetryableError(f"Anthropic timeout error: {e}") from e
        except InternalServerError as e:
            raise RetryableError(f"Anthropic internal server error: {e}") from e
        except APIError as e:  # General API errors; 5xx (incl. 529 overload) are transient, 429 is quota
            _code = getattr(e, 'status_code', None)
            if _code in _RETRYABLE_CODES:
                raise RetryableError(f"Anthropic transient API error ({_code}): {e}", retry_after=extract_retry_after(response=getattr(e, 'response', None), message=str(e))) from e
            raise FatalError(f"Anthropic API error: {e}") from e
        except Exception as e:
            if self.is_cancelling:
                logger.info("Stream gracefully cancelled by user request (exception suppressed).")
            else:
                logger.error(f"An unexpected error occurred in Anthropic provider that will be re-raised: {e}", exc_info=True)
                if is_transient_error(e):
                    raise RetryableError(f"Anthropic transient error: {e}", retry_after=extract_retry_after(message=str(e))) from e
                raise FatalError(f"An unexpected error in Anthropic provider: {e}") from e
        finally:
            logger.info("Anthropic stream finished or cancelled. Clearing stream reference.")
            self.current_stream = None
            self.is_cancelling = False

    def get_generation_id(self, response_chunk) -> str | None:
        return None
