import json
import logging
import base64
import mimetypes
import httpx
import pathlib
import io
from PIL import Image

from openai import OpenAI, APIError, AuthenticationError, RateLimitError, APIConnectionError, APITimeoutError
from src.llm.base_provider import BaseLLMProvider
from src.llm.exceptions import FatalError, RetryableError, extract_retry_after, is_transient_error
from src.llm.exceptions import RETRYABLE_STATUS_CODES as _RETRYABLE_CODES

logger = logging.getLogger(__name__)

def image_to_base64_url(attachment):
    """Converts an image from a file path or bytes into a data URL."""
    try:
        if isinstance(attachment, str):
            # It's a file path
            file_uri = pathlib.Path(attachment).as_uri()
            mime_type, _ = mimetypes.guess_type(file_uri)
            if not mime_type:
                mime_type = "application/octet-stream"
            with open(attachment, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                return f"data:{mime_type};base64,{encoded_string}"
        elif isinstance(attachment, bytes):
            # It's raw image data
            img = Image.open(io.BytesIO(attachment))
            buffer = io.BytesIO()
            img_format = img.format or 'PNG'  # Default to PNG
            img.save(buffer, format=img_format)
            encoded_string = base64.b64encode(buffer.getvalue()).decode('utf-8')
            mime_type = f"image/{img_format.lower()}"
            return f"data:{mime_type};base64,{encoded_string}"
        else:
            logger.error(f"Unsupported attachment type: {type(attachment)}")
            return None
    except Exception as e:
        logger.error(f"Error processing image attachment: {e}", exc_info=True)
        return None

class XAIProvider(BaseLLMProvider):
    @staticmethod
    def get_provider_id() -> str:
        return "x-ai"

    @staticmethod
    def get_display_name() -> str:
        return "x=AI"

    @staticmethod
    def get_default_settings() -> dict:
        return {
            "model": "grok-4-fast-reasoning",
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
            "api_url": "https://api.x.ai/v1",
            "supports_cancellation": True,
            "supports_streaming_tools": True
        }

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        self.api_url = self.provider_config.get("api_url")
        self._tools_cache = None
        self.current_stream = None
        self.is_cancelling = False

        http_client = httpx.Client(follow_redirects=True)

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.api_url,
            http_client=http_client
        )

    def web_search(self, query: str) -> str | None:
        """
        Performs a web search using XAI's Live Search functionality by sending a direct HTTP request.
        This bypasses the openai client which does not support the 'search_parameters' argument.
        Note: XAI Live Search will be deprecated by December 15, 2025.
        """
        try:
            logging.info(f"Performing XAI Live Search for: '{query}'")

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            payload = {
                'model': self.model,
                'messages': [{'role': 'user', 'content': query}],
                'search_parameters': {
                    'mode': 'on',
                    'return_citations': True,
                    'data_sources': ['web']
                }
            }

            with httpx.Client() as client:
                response = client.post(f"{self.api_url}/chat/completions", headers=headers, json=payload, timeout=60)
                response.raise_for_status()

            response_data = response.json()

            message = response_data.get('choices', [{}])[0].get('message', {})
            text_content = message.get('content', "")

            citations = []
            if 'citations' in message and message['citations']:
                for citation in message['citations']:
                    if 'url' in citation:
                        citations.append(citation['url'])

            if citations:
                return f"{text_content}\n\nCitations:\n" + "\n".join(citations)
            else:
                return text_content

        except httpx.HTTPStatusError as e:
            # More specific error handling for HTTP errors
            error_details = e.response.text
            logging.error(f"XAI API error during web search: {e.response.status_code} - {error_details}")
            if e.response.status_code in _RETRYABLE_CODES:
                raise RetryableError(f"XAI transient error during web search ({e.response.status_code}): {error_details}", retry_after=extract_retry_after(response=e.response, message=error_details)) from e
            raise FatalError(f"XAI API error during web search: {e.response.status_code} - {error_details}") from e
        except httpx.RequestError as e:
            # For network-level errors
            logging.error(f"XAI connection error during web search: {e}")
            raise RetryableError(f"XAI connection error during web search: {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in XAI provider (web_search): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"XAI transient error (web_search): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in XAI provider (web_search): {e}") from e

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
                formatted_tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": schema_info.get("description", ""),
                        "parameters": params
                    }
                })
        self._tools_cache = formatted_tools
        return formatted_tools

    def _prepare_messages(self, prompt: str, attachments: list, system_prompt: str = None, history: list = None):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if history:
            for message in history:
                role = message.get("role")
                if role == "model":
                    role = "assistant"
                if role in ["user", "assistant"]:
                    messages.append({"role": role, "content": message.get("content", "")})

        user_content_parts = [{"type": "text", "text": prompt}]
        if attachments:
            for attachment in attachments:
                base64_url = image_to_base64_url(attachment)
                if base64_url:
                    user_content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": base64_url}
                    })

        messages.append({"role": "user", "content": user_content_parts})
        return messages

    def _execute_get_response(self, prompt: str, attachments: list = None, system_prompt: str = None, history: list = None, schema_name: str | None = None):
        messages = self._prepare_messages(prompt, attachments, system_prompt, history)

        request_body = {
            "model": self.model,
            "messages": messages,
        }

        # Always provide all tools if JSON output is required, then specify tool_choice
        if self.require_json_output:
            all_tools = self._get_tools_from_cache()
            if all_tools:
                request_body["tools"] = all_tools
                if schema_name:
                    # Force a specific tool
                    request_body["tool_choice"] = {"type": "function", "function": {"name": schema_name}}
                else:
                    # Let the model choose
                    request_body["tool_choice"] = "auto"
            else:
                logger.warning("`require_json_output` is True, but no tools were found in cache.")

        try:
            response = self.client.chat.completions.create(**request_body)
            message = response.choices[0].message
            text_content = message.content or ""
            raw_tool_calls = message.tool_calls

            if raw_tool_calls:
                for call in raw_tool_calls:
                    function = call.function
                    name, args_str = function.name, function.arguments
                    if not name:
                        continue
                    try:
                        args = json.loads(args_str)
                        if 'payload' in args:
                            reconstructed_object = {"type": name, "payload": args['payload']}
                            return {"text": text_content, "tool_calls": reconstructed_object}
                        else:
                            logger.warning(f"Tool call '{name}' received without a 'payload'. Arguments: {args_str}")
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode tool call arguments for '{name}': {args_str}")
                return {"text": text_content, "tool_calls": []}
            else:
                # Fallback for non-tool responses
                return {"text": text_content, "tool_calls": []}

        except AuthenticationError as e:
            raise FatalError(f"XAI authentication error: {e}") from e
        except RateLimitError as e:
            raise RetryableError(f"XAI rate limit error (retryable): {e}", retry_after=extract_retry_after(response=getattr(e, 'response', None), message=str(e))) from e
        except APIConnectionError as e:
            raise RetryableError(f"XAI connection error: {e}") from e
        except APITimeoutError as e:
            raise RetryableError(f"XAI timeout error: {e}") from e
        except APIError as e:  # General API errors (e.g., 400, 404); 5xx/429 are transient
            _code = getattr(e, 'status_code', None)
            if _code in _RETRYABLE_CODES:
                raise RetryableError(f"XAI transient API error ({_code}): {e}", retry_after=extract_retry_after(response=getattr(e, 'response', None), message=str(e))) from e
            raise FatalError(f"XAI API error: {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in XAI provider (get_response): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"XAI transient error (get_response): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in XAI provider: {e}") from e

    def cancel_stream(self, generation_id: str = None):
        if self.current_stream:
            logger.info("Attempting to cancel XAI stream.")
            try:
                self.is_cancelling = True
                self.current_stream.close()
                logger.info("Successfully closed XAI stream.")
            except Exception as e:
                logger.error(f"Error closing XAI stream: {e}", exc_info=True)
        else:
            logger.warning("No active XAI stream to cancel.")

    def stream_response(self, prompt: str, attachments: list, system_prompt: str = None, history: list = None, callbacks=None):
        # Use xai_sdk's stateful Responses API for streaming interactive tool calls.
        # The xai_sdk exposes a `Client().chat.create(...)` object with `append()` and `stream()`.
        self.is_cancelling = False
        raw_llm_logger = logging.getLogger('llm_raw_response')
        try:
            # Import xai_sdk objects lazily to avoid hard dependency at module import time
            try:
                from xai_sdk import Client as XAIClient
                from xai_sdk.chat import user as xai_user, assistant as xai_assistant, system as xai_system
                from xai_sdk.tools import web_search as xai_web_search
            except Exception as e:
                logger.error(f"xai_sdk is required for streaming with XAI provider: {e}")
                raise FatalError("xai_sdk is required for streaming with XAI provider. Install 'xai_sdk' package.") from e

            # xai_sdk.Client does not accept 'base_url' named argument in current SDK.
            # Use api_key only and let xai_sdk use its defaults or configured environment variables.
            xai_client = XAIClient(api_key=self.api_key)

            # Build the chat and provide web_search (default for browsing) as an agentic tool.
            chat = xai_client.chat.create(model=self.model, tools=[xai_web_search()])

            # Append a system message if provided
            if system_prompt:
                chat.append(xai_system(system_prompt))

            # Append history messages
            if history:
                for message in history:
                    role = message.get("role")
                    content = message.get("content", "")
                    if role == "model":
                        role = "assistant"
                    if role == "user":
                        chat.append(xai_user(content))
                    elif role == "assistant":
                        chat.append(xai_assistant(content))

            # Add current user prompt (and attachments as data URLs if present)
            user_text = prompt
            if attachments:
                # Build combined user content including images inline using data URLs
                # If more advanced attachment types are needed, consider chat.append with richer content objects.
                for attachment in attachments:
                    base64_url = image_to_base64_url(attachment)
                    if base64_url:
                        user_text += f"\n[image: {base64_url}]"
            chat.append(xai_user(user_text))

            # Start streaming using the XAI SDK: yields (response, chunk) pairs
            self.current_stream = chat
            _logged_finish_event = False

            for resp, chunk in chat.stream():
                # Determine if the chunk has meaningful content or if it's a finish event
                has_content = bool(getattr(chunk, "content", None))
                has_tool_calls = bool(getattr(chunk, "tool_calls", None))
                is_finish_event = 'finish_reason' in str(resp)

                # Log only if there's content/tools, or if it's the final event (and we haven't logged it yet)
                if has_content or has_tool_calls or (is_finish_event and not _logged_finish_event):
                    try:
                        log_entry = {
                            'response': str(resp),
                            'chunk': str(chunk)
                        }
                        raw_llm_logger.info(json.dumps(log_entry, default=str))

                        if is_finish_event:
                            _logged_finish_event = True
                    except Exception as log_e:
                        raw_llm_logger.warning(f"Could not serialize and log raw XAI event: {log_e}")

                if self.is_cancelling:
                    logger.info("Stream gracefully cancelled by user request (XAI).")
                    try:
                        # Try to close the chat session if supported
                        if hasattr(chat, "close") and callable(chat.close):
                            chat.close()
                    except Exception:
                        pass
                    break

                # Real-time tool calls (agentic tool events)
                if getattr(chunk, "tool_calls", None):
                    for tc in chunk.tool_calls:
                        try:
                            tc_name = getattr(tc.function, "name", None)

                            # Map XAI tool names to internal tool names
                            if tc_name == 'browse_page':
                                tc_name = 'web_fetchs'
                            elif tc_name and tc_name.startswith('web_search'):
                                tc_name = 'web_search'

                            tc_args = getattr(tc.function, "arguments", None)
                            if isinstance(tc_args, str):
                                try:
                                    tc_args_parsed = json.loads(tc_args)
                                except Exception:
                                    tc_args_parsed = {}
                            else:
                                tc_args_parsed = tc_args or {}
                            tool_call_data = {"name": tc_name, "arguments": tc_args_parsed}
                            yield {"type": "tool_code", "content": tool_call_data}
                        except Exception:
                            logger.exception("Failed to process streaming tool call from xAI chunk")

                # Content deltas (streamed text)
                if getattr(chunk, "content", None):
                    content_piece = chunk.content
                    if content_piece:
                        yield {"type": "text", "content": content_piece}

        except AuthenticationError as e:
            raise FatalError(f"XAI authentication error: {e}") from e
        except RateLimitError as e:
            raise RetryableError(f"XAI rate limit error (retryable): {e}", retry_after=extract_retry_after(response=getattr(e, 'response', None), message=str(e))) from e
        except APIConnectionError as e:
            raise RetryableError(f"XAI connection error: {e}") from e
        except APITimeoutError as e:
            raise RetryableError(f"XAI timeout error: {e}") from e
        except APIError as e:
            _code = getattr(e, 'status_code', None)
            if _code in _RETRYABLE_CODES:
                raise RetryableError(f"XAI transient API error ({_code}): {e}", retry_after=extract_retry_after(response=getattr(e, 'response', None), message=str(e))) from e
            raise FatalError(f"XAI API error: {e}") from e
        except Exception as e:
            if self.is_cancelling:
                logger.info("Stream gracefully cancelled by user request (exception suppressed).")
            else:
                logger.error(f"An unexpected error occurred in XAI provider (stream_response): {e}", exc_info=True)
                if is_transient_error(e):
                    raise RetryableError(f"XAI transient error: {e}", retry_after=extract_retry_after(message=str(e))) from e
                raise FatalError(f"An unexpected error in XAI provider: {e}") from e
        finally:
            logger.info("XAI stream finished or cancelled. Clearing stream reference.")
            self.current_stream = None
            self.is_cancelling = False

