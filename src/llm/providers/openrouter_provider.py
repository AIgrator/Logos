# src/llm/openrouter_provider.py

import copy
import json
import logging
import base64
import io
import requests
from PIL import Image
from ..base_provider import BaseLLMProvider
from ..exceptions import FatalError, RetryableError, extract_retry_after, is_transient_error, is_transient_throttle

class OpenRouterProvider(BaseLLMProvider):
    """
    LLM provider for OpenRouter. This version implements robust error handling
    that explicitly captures and logs the server's error response body before
    raising an exception, ensuring precise diagnostics.
    """
    @staticmethod
    def get_provider_id() -> str:
        return "openrouter"

    @staticmethod
    def get_display_name() -> str:
        return "OpenRouter"

    @staticmethod
    def get_default_settings() -> dict:
        return {
            "model": "anthropic/claude-sonnet-4.5",
            "require_json_output": True
        }

    @staticmethod
    def get_ui_schema() -> dict:
        return {
            "api_key": {"type": "password", "label": "API Key"},
            "model": {
                "type": "nested_dropdown",
                "label": "Model",
                "primary_dropdown_label": "Provider",
                "secondary_dropdown_label": "Model",
                "options_source": "models.json"
            },
            "require_json_output": {"type": "checkbox", "label": "Require JSON Output"}
        }

    @staticmethod
    def get_provider_config() -> dict:
        return {
            "api_url": "https://openrouter.ai/api/v1",
            # This is a complex case. Some models support cancellation, others don't.
            # The logic is handled inside the provider based on the selected model.
            "supports_cancellation": True,
            "models_by_provider": {},
            "provider_properties": {
                "Anthropic": {"supports_cancellation": True},
                "Google": {"supports_cancellation": False},
                "OpenAI": {"supports_cancellation": True},
                "X-AI": {"supports_cancellation": True}
            }
        }

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        self.base_url = self.get_provider_config().get("api_url")
        self._tools_cache = None
        self.session = requests.Session()
        self.current_generation_id = None

    def web_search(self, query: str) -> str | None:
        """
        Performs a web search using the configured OpenRouter model by appending
        the ':online' suffix, which automatically enables the web search plugin.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/AIgrator/Logos", "X-Title": "Logos"
        }

        # Use the ':online' suffix as a shortcut to enable web search
        search_model = f"{self.model}:online"
        logging.info(f"Performing web search with OpenRouter model: {search_model}")

        messages = [{"role": "user", "content": query}]
        request_body = {"model": search_model, "messages": messages}

        llm_comms_logger = logging.getLogger('llm_communication')
        llm_comms_logger.debug(f"OpenRouter Request Body (web_search):\n{json.dumps(request_body, indent=2)}")

        try:
            response = self.session.post(f"{self.base_url}/chat/completions", headers=headers, json=request_body, timeout=120)

            llm_comms_logger.debug(f"OpenRouter Response (web_search):\nStatus: {response.status_code}\nBody: {response.text}")

            response.raise_for_status()

            response_data = response.json()
            message = response_data.get("choices", [{}])[0].get("message", {})
            text_content = message.get("content") or ""

            return text_content

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            error_message = f"OpenRouter HTTP error ({status_code}) during web search: {e.response.text}"
            if status_code == 429:
                if is_transient_throttle(error_message):
                    raise RetryableError(f"OpenRouter transient upstream throttling: {error_message}", retry_after=extract_retry_after(response=e.response, message=error_message)) from e
                raise FatalError(f"OpenRouter quota exhausted: {error_message}") from e
            if 400 <= status_code < 500:
                raise FatalError(error_message) from e
            else: # Treat 5xx and other errors as potentially retryable
                raise RetryableError(error_message) from e
        except requests.exceptions.RequestException as e:
            raise RetryableError(f"OpenRouter network error during web search: {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in OpenRouter provider (web_search): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"OpenRouter transient error (web_search): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in OpenRouter provider (web_search): {e}") from e

    def _get_tools_from_cache(self):
        if self._tools_cache is not None:
            return self._tools_cache

        schema_files = [
            'cache/terminal/response_formats.json',
            'cache/vision/response_formats.json',
            'cache/browser/response_formats.json'
        ]
        all_schemas = {}

        for file_path in schema_files:
            try:
                with open(file_path, encoding='utf-8') as f:
                    schemas = json.load(f)
                    all_schemas.update(schemas)
            except FileNotFoundError:
                logging.warning(f"Could not find schema file: '{file_path}'. Skipping.")
                continue
            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse JSON from '{file_path}': {e}", exc_info=True)
                continue
            except Exception as e:
                logging.error(f"Failed to load or parse tools from '{file_path}': {e}", exc_info=True)
                continue

        if not all_schemas:
            logging.error("No tool schemas could be loaded. Please ensure 'cache/terminal/response_formats.json', 'cache/vision/response_formats.json' and/or 'cache/browser/response_formats.json' exist and are valid.")
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

    def _prepare_messages(self, prompt, attachments=None, system_prompt=None, history=None):
        is_multimodal = bool(attachments)
        messages = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if history:
            for item in history:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                if role == 'model':
                    role = 'assistant'
                if role not in ["user", "assistant"]:
                    continue
                content_text = str(item.get("content", ""))
                content = [{"type": "text", "text": content_text}] if is_multimodal else content_text
                messages.append({"role": role, "content": content})

        user_content_parts = [{"type": "text", "text": prompt}]
        if is_multimodal:
            for attachment in attachments:
                if attachment is None:
                    continue
                try:
                    img = attachment if isinstance(attachment, Image.Image) else Image.open(io.BytesIO(attachment) if isinstance(attachment, bytes) else attachment)
                    buffer = io.BytesIO()
                    img.save(buffer, format='PNG')
                    base64_image = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    data_url = f"data:image/png;base64,{base64_image}"
                    user_content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                except Exception as e:
                    logging.error(f"Error processing attachment for OpenRouter: {e}", exc_info=True)

        final_user_content = user_content_parts if is_multimodal else prompt
        new_user_message = {"role": "user", "content": final_user_content}

        if messages and messages[-1].get("role") == "user":
            messages[-1] = new_user_message
        else:
            messages.append(new_user_message)

        return messages

    def _execute_get_response(self, prompt: str, attachments: list = None, system_prompt: str = None, history: list = None, schema_name: str | None = None):
        from ..llm_parsing import repair_json_string
        headers = {
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/AIgrator/Logos", "X-Title": "Logos"
        }
        messages = self._prepare_messages(prompt, attachments, system_prompt, history)
        request_body = {"model": self.model, "messages": messages}

        use_forced_tool = schema_name and self.require_json_output

        if use_forced_tool:
            logging.debug(f"Attempting to force tool call for schema: {schema_name}")
            all_tools = self._get_tools_from_cache()
            target_tool = next((t for t in all_tools if t.get("function", {}).get("name") == schema_name), None)

            if target_tool:
                tool_to_send = copy.deepcopy(target_tool)

                # --- Universal Schema Adaptation ---
                # Instead of sending the full Pydantic model schema as parameters,
                # we tell the LLM that the function's only argument is the 'payload'.
                # This forces the LLM to return a clean `{"payload": ...}` structure
                # in the arguments, which we can reliably reconstruct.
                original_parameters = tool_to_send["function"]["parameters"]
                if "payload" in original_parameters.get("properties", {}):
                    corrected_parameters = {
                        "type": "object",
                        "properties": {
                            "payload": original_parameters["properties"]["payload"]
                        },
                        "required": ["payload"]
                    }
                    tool_to_send["function"]["parameters"] = corrected_parameters

                request_body["tools"] = [tool_to_send]
                request_body["tool_choice"] = {"type": "function", "function": {"name": schema_name}}
            else:
                logging.warning(f"Schema '{schema_name}' requested but not found. Falling back to 'auto'.")
                use_forced_tool = False

        if not use_forced_tool:
            if system_prompt:
                tools = self._get_tools_from_cache()
                if tools:
                    request_body["tools"] = tools
                    request_body["tool_choice"] = "auto"

        # The llm_communication logger is configured to write to a separate file
        llm_comms_logger = logging.getLogger('llm_communication')

        log_body = copy.deepcopy(request_body)
        for message in log_body.get('messages', []):
            if isinstance(message.get('content'), list):
                for part in message['content']:
                    if part.get('type') == 'image_url':
                        part['image_url']['url'] = '[IMAGE DATA]'

        llm_comms_logger.debug(f"OpenRouter Request Body (get_response):\n{json.dumps(log_body, indent=2)}")

        try:
            response = self.session.post(f"{self.base_url}/chat/completions", headers=headers, json=request_body, timeout=120)

            # Log the full response for debugging purposes
            llm_comms_logger.debug(f"OpenRouter Response (get_response):\nStatus: {response.status_code}\nBody: {response.text}")

            response.raise_for_status()

            response_data = response.json()
            message = response_data.get("choices", [{}])[0].get("message", {})
            text_content = message.get("content") or ""
            raw_tool_calls = message.get("tool_calls")

            if raw_tool_calls:
                for call in raw_tool_calls:
                    function = call.get("function", {})
                    name, args_str = function.get("name"), function.get("arguments", "{}")
                    if not name:
                        continue

                    try:
                        args = json.loads(args_str)
                        if 'payload' in args:
                            reconstructed_object = {"type": name, "payload": args['payload']}
                            return {"text": text_content, "tool_calls": reconstructed_object}
                        else:
                            logging.warning(f"Tool call '{name}' received without a 'payload'. Arguments: {args_str}")

                    except json.JSONDecodeError:
                        logging.error(f"Failed to decode tool call arguments for '{name}'. Raw args: '{args_str}'")
                        # Attempt to repair the broken JSON from the arguments
                        repaired_args = repair_json_string(args_str)
                        if repaired_args and 'payload' in repaired_args:
                            logging.info(f"Successfully repaired arguments for tool call '{name}'.")
                            reconstructed_object = {"type": name, "payload": repaired_args['payload']}
                            return {"text": text_content, "tool_calls": reconstructed_object}
                        else:
                            logging.error(f"Could not repair arguments for tool call '{name}'. Skipping.")
                            continue # Move to the next tool call

                return {"text": text_content, "tool_calls": []}
            elif text_content:
                repaired = repair_json_string(text_content)
                return {"text": text_content, "tool_calls": repaired or []}
            else:
                return {"text": "", "tool_calls": []}

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if status_code == 429:
                if is_transient_throttle(e.response.text):
                    raise RetryableError(f"OpenRouter transient upstream throttling ({status_code}): {e.response.text}", retry_after=extract_retry_after(response=e.response, message=e.response.text)) from e
                raise FatalError(f"OpenRouter quota exhausted ({status_code}): {e.response.text}") from e
            if 400 <= status_code < 500:
                raise FatalError(f"OpenRouter client error ({status_code}): {e.response.text}") from e
            elif 500 <= status_code < 600:
                raise RetryableError(f"OpenRouter server error ({status_code}): {e.response.text}") from e
            else:
                raise FatalError(f"OpenRouter unexpected HTTP error ({status_code}): {e.response.text}") from e
        except requests.exceptions.RequestException as e:
            raise RetryableError(f"OpenRouter network error: {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in OpenRouter provider (get_response): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"OpenRouter transient error (get_response): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in OpenRouter provider: {e}") from e

    def stream_response(self, prompt: str, attachments: list = None, system_prompt: str = None, history: list = None, callbacks=None):
        self.current_generation_id = None
        headers = {
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/AIgrator/Logos", "X-Title": "Logos"
        }
        messages = self._prepare_messages(prompt, attachments, system_prompt, history)

        # Append ':online' to enable web search for streaming responses
        streaming_model = f"{self.model}:online"
        logging.info(f"Enabling web search for streaming with model: {streaming_model}")
        request_body = {"model": streaming_model, "messages": messages, "stream": True}

        log_body = copy.deepcopy(request_body)
        for message in log_body.get('messages', []):
            if isinstance(message.get('content'), list):
                for part in message['content']:
                    if part.get('type') == 'image_url':
                        part['image_url']['url'] = '[IMAGE DATA]'

        logging.debug(f"OpenRouter Request Body (stream_response):\n{json.dumps(log_body, indent=2)}")
        try:
            with self.session.post(f"{self.base_url}/chat/completions", headers=headers, json=request_body, timeout=120, stream=True) as response:
                if not response.ok:
                    error_body = response.text
                    logging.error(f"OpenRouter server returned an error ({response.status_code}) for stream_response. Body: {error_body}")
                    # Re-raise the error to be caught by the appropriate handler below
                    response.raise_for_status()

                # Manually iterate through the stream to handle SSE events
                for line in response.iter_lines():
                    if not line:
                        continue

                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith('data: '):
                        json_str = decoded_line[len('data: '):]
                        if json_str.strip() == '[DONE]':
                            break

                        try:
                            chunk = json.loads(json_str)
                            # Check for generation_id, which is a special event
                            if 'id' in chunk and 'choices' not in chunk:
                                self.current_generation_id = chunk.get('id')
                                if self.current_generation_id:
                                    logging.info(f"OpenRouter generation ID received: {self.current_generation_id}")
                                    yield f"generation_id::{self.current_generation_id}"
                                continue

                            content = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            logging.warning(f"Could not decode stream chunk: {json_str}")

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if status_code == 429:
                if is_transient_throttle(str(e)):
                    raise RetryableError(f"OpenRouter transient upstream throttling ({status_code}): {e}", retry_after=extract_retry_after(response=e.response, message=str(e))) from e
                raise FatalError(f"OpenRouter quota exhausted ({status_code}): {e}") from e
            if 400 <= status_code < 500:
                raise FatalError(f"OpenRouter client error ({status_code}): {e}") from e
            elif 500 <= status_code < 600:
                raise RetryableError(f"OpenRouter server error ({status_code}): {e}") from e
            else:
                raise FatalError(f"OpenRouter unexpected HTTP error ({status_code}): {e}") from e
        except requests.exceptions.RequestException as e:
            # Catches connection errors, timeouts, etc.
            raise RetryableError(f"OpenRouter network error: {e}") from e
        except Exception as e:
            logging.error(f"An unexpected error occurred in OpenRouter provider that will be re-raised: {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"OpenRouter transient error: {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in OpenRouter provider: {e}") from e
        finally:
            logging.info(f"Stream finished. Resetting generation ID from {self.current_generation_id}")
            self.current_generation_id = None

    def cancel_stream(self, generation_id: str):
        """
        Sends a POST request to OpenRouter to cancel a specific stream.
        """
        if not generation_id:
            logging.warning("No generation ID provided, cannot cancel stream.")
            return

        # Note: The `api_url` from config is for completions, not the base for cancellation.
        # We use the hardcoded base_url of the provider.
        cancel_url = f"{self.base_url}/chat/generations/{generation_id}/cancel"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/AIgrator/Logos", "X-Title": "Logos"
        }

        logging.info(f"Attempting to cancel stream with ID: {generation_id} at {cancel_url}")

        try:
            response = self.session.post(cancel_url, headers=headers, timeout=15)

            if response.status_code == 200:
                logging.info(f"Successfully requested cancellation for generation ID: {generation_id}")
            else:
                logging.error(f"Failed to cancel stream for generation ID: {generation_id}. Status: {response.status_code}, Response: {response.text}")

        except requests.RequestException as e:
            logging.error(f"An exception occurred while trying to cancel stream {generation_id}: {e}", exc_info=True)
