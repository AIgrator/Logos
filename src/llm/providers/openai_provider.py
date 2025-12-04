import copy
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
from src.llm.exceptions import FatalError, RetryableError
from src.llm.llm_parsing import repair_json_string

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

class OpenAIProvider(BaseLLMProvider):
    @staticmethod
    def get_provider_id() -> str:
        return "openai"

    @staticmethod
    def get_display_name() -> str:
        return "OpenAI"

    @staticmethod
    def get_default_settings() -> dict:
        return {
            "model": "gpt-5-mini",
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
            "api_url": "https://api.openai.com/v1",
            "supports_cancellation": True,
            "supports_streaming_tools": True
        }

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        api_url = self.provider_config.get("api_url")
        self._tools_cache = None
        self.current_stream = None
        self.is_cancelling = False

        http_client = httpx.Client(follow_redirects=True)

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=api_url,
            http_client=http_client
        )

    def web_search(self, query: str) -> str | None:
        """
        Performs a web search using a supported native OpenAI model.
        This method checks if the configured model is a specialized search model.
        If so, it performs the search; otherwise, it returns None to allow fallback.
        """
        # Per OpenAI documentation for the Chat Completions API, a specialized model
        # must be used for web search. We hardcode the project-mandated 'gpt-5-search-api' here.
        search_model = "gpt-5-search-api"

        try:
            logging.info(f"Performing native web search with dedicated model: {search_model}")
            response = self.client.chat.completions.create(
                model=search_model,
                messages=[{"role": "user", "content": query}],
            )
            message = response.choices[0].message
            text_content = message.content or ""
            return text_content

        except AuthenticationError as e:
            raise FatalError(f"OpenAI authentication error during web search: {e}") from e
        except RateLimitError as e:
            raise FatalError(f"OpenAI rate limit error during web search: {e}") from e
        except APIConnectionError as e:
            raise RetryableError(f"OpenAI connection error during web search: {e}") from e
        except APITimeoutError as e:
            raise RetryableError(f"OpenAI timeout error during web search: {e}") from e
        except APIError as e:
            raise FatalError(f"OpenAI API error during web search: {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in OpenAI provider (web_search): {e}", exc_info=True)
            raise FatalError(f"An unexpected error in OpenAI provider (web_search): {e}") from e

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
                # Create tool definitions compatible with Responses API (top-level name)
                formatted_tools.append({
                    "type": "function",
                    "name": name,
                    "description": schema_info.get("description", ""),
                    "parameters": params
                })
        self._tools_cache = formatted_tools
        return formatted_tools

    def _prepare_messages(self, prompt: str, attachments: list, system_prompt: str = None, history: list = None):
        """
        Prepare input items and instructions for the Responses API.
        Returns a tuple: (input_items, instructions)
        """
        input_items = []
        instructions = system_prompt if system_prompt else None

        # Map history into the Responses API input format
        if history:
            for message in history:
                role = message.get("role")
                content = message.get("content", "")
                if role == "model":
                    role = "assistant"
                if role in ["user", "assistant"]:
                    content_type = "output_text" if role == "assistant" else "input_text"
                    input_items.append({
                        "role": role,
                        "content": [{"type": content_type, "text": content}]
                    })

        # Current user prompt + optional attachments
        user_content_parts = [{"type": "input_text", "text": prompt}]
        if attachments:
            for attachment in attachments:
                base64_url = image_to_base64_url(attachment)
                if base64_url:
                    # Responses API expects images provided as 'image_url' (data URL is OK)
                    user_content_parts.append({
                        "type": "input_image",
                        "image_url": base64_url
                    })

        input_items.append({"role": "user", "content": user_content_parts})
        return input_items, instructions

    def _prepare_stream_input(self, prompt: str, attachments: list, system_prompt: str = None, history: list = None):
        """Prepares the input for the new streaming-capable Responses API."""
        input_items = []
        instructions = system_prompt if system_prompt else None

        if history:
            for message in history:
                role = message.get("role")
                content = message.get("content", "")
                if role == "model":
                    role = "assistant"

                content_type = "output_text" if role == "assistant" else "input_text"

                if role in ["user", "assistant"]:
                    input_items.append({
                        "role": role,
                        "content": [{"type": content_type, "text": content}]
                    })

        # Add the current user prompt and attachments with corrected types
        user_content_parts = [{"type": "input_text", "text": prompt}]
        if attachments:
            for attachment in attachments:
                base64_url = image_to_base64_url(attachment)
                if base64_url:
                    # Responses API expects images provided as 'image_url' (data URL is OK)
                    user_content_parts.append({
                        "type": "input_image",
                        "image_url": base64_url
                    })

        input_items.append({"role": "user", "content": user_content_parts})

        return {"input": input_items, "instructions": instructions, "tools": [{"type": "web_search_preview"}]}

    def _execute_get_response(self, prompt: str, attachments: list = None, system_prompt: str = None, history: list = None, schema_name: str | None = None):
        # Prepare Respones API input items and instructions
        input_items, instructions = self._prepare_messages(prompt, attachments, system_prompt, history)

        request_body = {
            "model": self.model,
            "input": input_items,
            "instructions": instructions or "",
        }

        use_forced_tool = schema_name and self.require_json_output

        if use_forced_tool:
            logger.debug(f"Attempting to force tool call for schema: {schema_name}")
            all_tools = self._get_tools_from_cache()
            target_tool = next((t for t in all_tools if t.get("name") == schema_name), None)

            if target_tool:
                # Normalize and restrict parameters if needed. Target tool is now top-level.
                tool_to_send = copy.deepcopy(target_tool)
                params = tool_to_send.get("parameters", {}) or {}
                if "properties" in params and "payload" in params.get("properties", {}):
                    tool_to_send["parameters"] = {
                        "type": "object",
                        "properties": {"payload": params["properties"]["payload"]},
                        "required": ["payload"]
                    }

                # Validate name exists
                if not tool_to_send.get("name"):
                    logger.error(f"Invalid tool format: missing 'name' for tool: {tool_to_send}")
                    raise FatalError("Invalid tool format: 'name' is required for tools")

                request_body["tools"] = [tool_to_send]
                request_body["tool_choice"] = {"type": "function", "name": schema_name}
            else:
                logger.warning(f"Schema '{schema_name}' requested but not found. Proceeding without forced tool.")

        try:
            # Use the Responses API
            response = self.client.responses.create(**request_body)
            final_text = getattr(response, "output_text", "") or ""

            # Parse possible function/tool calls in the output
            tool_call_result = None
            if response.output:
                for item in response.output:
                    item_type = getattr(item, "type", None)
                    # function_call is the typical Responses API type
                    if item_type == 'function_call' and getattr(item, 'name', None) == schema_name:
                        # Arguments may be a JSON string or dict in new SDK.
                        args_field = getattr(item, 'arguments', None)
                        try:
                            if isinstance(args_field, str):
                                parsed = json.loads(args_field)
                            else:
                                parsed = args_field or {}
                            payload = parsed.get('payload', parsed)
                            tool_call_result = {"type": schema_name, "payload": payload}
                            final_text = ""  # prefer structured output
                            break
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse function_call arguments for '{schema_name}', attempting to repair: {args_field}")
                            parsed = repair_json_string(args_field)
                            if parsed:
                                payload = parsed.get('payload', parsed)
                                tool_call_result = {"type": schema_name, "payload": payload}
                                final_text = ""
                            else:
                                logger.error(f"Failed to repair and parse arguments for '{schema_name}'")
                            break
                    elif item_type == 'tool_call':
                        # Some SDK versions expose 'tool_call' with .tool and .arguments
                        tool_name = getattr(item, 'tool', None)
                        args_field = getattr(item, 'arguments', None)
                        try:
                            if isinstance(args_field, str):
                                parsed = json.loads(args_field)
                            else:
                                parsed = args_field or {}
                            payload = parsed.get('payload', parsed)
                            tool_call_result = {"type": tool_name, "payload": payload}
                            final_text = ""
                            break
                        except Exception:
                            logger.exception(f"Failed to parse tool_call arguments for '{tool_name}': {args_field}")

            if tool_call_result:
                return {"text": final_text, "tool_calls": tool_call_result}
            else:
                return {"text": final_text, "tool_calls": []}

        except AuthenticationError as e:
            raise FatalError(f"OpenAI authentication error: {e}") from e
        except RateLimitError as e:
            raise FatalError(f"OpenAI rate limit error: {e}") from e
        except APIConnectionError as e:
            raise RetryableError(f"OpenAI connection error: {e}") from e
        except APITimeoutError as e:
            raise RetryableError(f"OpenAI timeout error: {e}") from e
        except APIError as e: # General API errors (e.g., 400, 404, etc.)
            raise FatalError(f"OpenAI API error: {e}") from e
        except Exception as e:
            logger.error(f"An unexpected error occurred in OpenAI provider (get_response): {e}", exc_info=True)
            raise FatalError(f"An unexpected error in OpenAI provider: {e}") from e

    def cancel_stream(self, generation_id: str = None):
        """Cancels the current active stream by closing the connection."""
        if self.current_stream:
            logger.info("Attempting to cancel OpenAI stream.")
            try:
                self.is_cancelling = True
                self.current_stream.close()
                logger.info("Successfully closed OpenAI stream.")
            except Exception as e:
                logger.error(f"Error closing OpenAI stream: {e}", exc_info=True)
        else:
            logger.warning("No active OpenAI stream to cancel.")

    def stream_response(self, prompt: str, attachments: list, system_prompt: str = None, history: list = None, callbacks=None):
        payload = self._prepare_stream_input(prompt, attachments, system_prompt, history)
        self.is_cancelling = False
        raw_llm_logger = logging.getLogger('llm_raw_response')

        try:
            stream = self.client.responses.create(
                model=self.model,
                input=payload["input"],
                instructions=payload["instructions"],
                tools=payload["tools"],
                stream=True,
            )
            self.current_stream = stream
            for event in stream:
                # The raw event object has a rich structure. For logging, convert it to a dict.
                try:
                    # The to_dict() method is a reliable way to serialize the event object
                    raw_llm_logger.info(json.dumps(event.to_dict()))
                except Exception as log_e:
                    raw_llm_logger.warning(f"Could not serialize and log raw OpenAI event: {log_e}")

                if self.is_cancelling:
                    logger.info("Stream gracefully cancelled by user request (OpenAI).")
                    break

                event_type = getattr(event, 'type', '')

                if event_type == 'response.output_text.delta':
                    content = getattr(event, 'delta', '')
                    if content:
                        yield {"type": "text", "content": content}

                elif event_type == 'response.output_web_search_call':
                    # This event signals the start of a web search.
                    # We yield a tool_code event to notify the UI.
                    tool_call_data = {
                        "name": "web_search",
                        "arguments": {} # Arguments are not provided in this event
                    }
                    yield {"type": "tool_code", "content": tool_call_data}

                elif event_type == 'response.output_function_call':
                    # This event signals a function call, similar to Gemini's.
                    function_name = getattr(event, 'name', '')
                    args_str = getattr(event, 'arguments', '{}')
                    try:
                        args_dict = json.loads(args_str)
                    except json.JSONDecodeError:
                        logger.warning(f"Could not parse function call arguments from OpenAI: {args_str}")
                        args_dict = {}

                    tool_call_data = {
                        "name": function_name,
                        "arguments": args_dict
                    }
                    yield {"type": "tool_code", "content": tool_call_data}

        except AuthenticationError as e:
            raise FatalError(f"OpenAI authentication error: {e}") from e
        except RateLimitError as e:
            raise FatalError(f"OpenAI rate limit error: {e}") from e
        except APIConnectionError as e:
            raise RetryableError(f"OpenAI connection error: {e}") from e
        except APITimeoutError as e:
            raise RetryableError(f"OpenAI timeout error: {e}") from e
        except APIError as e: # General API errors (e.g., 400, 404, etc.)
            raise FatalError(f"OpenAI API error: {e}") from e
        except Exception as e:
            if self.is_cancelling:
                logger.info("Stream gracefully cancelled by user request (exception suppressed).")
            else:
                logger.error(f"An unexpected error occurred in OpenAI provider that will be re-raised: {e}", exc_info=True)
                raise FatalError(f"An unexpected error in OpenAI provider: {e}") from e
        finally:
            logger.info("OpenAI stream finished or cancelled. Clearing stream reference.")
            self.current_stream = None
            self.is_cancelling = False
