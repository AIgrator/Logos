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

    def _get_tools_from_cache(self):
        if self._tools_cache is not None:
            return self._tools_cache

        schema_files = [
            'cache/terminal/response_formats.json',
            'cache/vision/response_formats.json'
        ]
        all_schemas = {}

        for file_path in schema_files:
            try:
                with open(file_path, encoding='utf-8') as f:
                    schemas = json.load(f)
                    all_schemas.update(schemas)
            except FileNotFoundError:
                logger.warning(f"Could not find schema file: '{file_path}'. Skipping.")
                continue
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from '{file_path}': {e}", exc_info=True)
                continue
            except Exception as e:
                logger.error(f"Failed to load or parse tools from '{file_path}': {e}", exc_info=True)
                continue

        if not all_schemas:
            logger.error("No tool schemas could be loaded. Please ensure 'cache/terminal/response_formats.json' and/or 'cache/vision/response_formats.json' exist and are valid.")
            return []

        formatted_tools = []
        for name, schema_info in all_schemas.items():
            params = schema_info.get("schema")
            if params:
                # Anthropic uses 'input_schema' instead of 'parameters'
                formatted_tools.append({
                    "name": name,
                    "description": schema_info.get("description", ""),
                    "input_schema": params
                })
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

        try:
            response = self.client.messages.create(**request_body)

            text_content = ""

            if response.content:
                for block in response.content:
                    if block.type == 'text':
                        text_content += block.text
                    elif block.type == 'tool_use' and use_forced_tool:
                        final_payload = block.input

                        # Continuously unwrap the payload as long as it's nested
                        while isinstance(final_payload, dict) and 'payload' in final_payload:
                            # If the dict has other keys besides 'payload', it might be the final object.
                            # The main indicator of a wrapper is having 'type' equal to the tool name
                            # or having 'payload' as the *only* key.
                            if len(final_payload.keys()) == 1 or ('type' in final_payload and final_payload.get('type') == block.name):
                                logger.info(f"Unwrapping nested payload for tool '{block.name}'.")
                                final_payload = final_payload.get('payload')
                            else:
                                # This seems to be the actual payload, stop unwrapping.
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
        else:
            logger.warning("No active Anthropic stream to cancel.")

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
                }
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

                    if event.type == "content_block_start":
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
                        if hasattr(event, 'usage'):
                            logger.info(f"📊 Usage: {event.usage}")

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
