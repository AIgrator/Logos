import base64
import io
import json
import logging
import mimetypes
import pathlib
import requests
from typing import Any
from PIL import Image
from ..exceptions import FatalError, RetryableError, extract_retry_after, is_transient_error
from ..base_provider import BaseLLMProvider

logger = logging.getLogger(__name__)


def image_to_huggingface_format(attachment):
    """Converts an image from a file path or bytes into Hugging Face OpenAI-compatible Vision format."""
    try:
        if isinstance(attachment, str):
            file_uri = pathlib.Path(attachment).as_uri()
            media_type, _ = mimetypes.guess_type(file_uri)
            if not media_type:
                media_type = "image/png"
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
            "type": "image_url",
            "image_url": {
                "url": f"data:{media_type};base64,{data}"
            }
        }
    except Exception as e:
        logger.error(f"Error processing image attachment for HuggingFace: {e}", exc_info=True)
        return None


class HuggingFaceProvider(BaseLLMProvider):
    @staticmethod
    def get_provider_id() -> str:
        return "huggingface"

    @staticmethod
    def get_display_name() -> str:
        return "Hugging Face"

    @staticmethod
    def get_default_settings() -> dict:
        return {
            "model": "Qwen/Qwen3.8-27B:novita",
            "max_tokens": 4096,
            "require_json_output": True
        }

    @staticmethod
    def get_ui_schema() -> dict:
        return {
            "api_key": {"type": "password", "label": "API Key (Token)"},
            "model": {"type": "dropdown", "label": "Model", "options_source": "models.json"},
            "max_tokens": {"type": "string", "label": "Max Tokens"},
            "require_json_output": {"type": "checkbox", "label": "Require JSON Output"}
        }

    @staticmethod
    def get_provider_config() -> dict:
        return {
            "api_url": "https://router.huggingface.co/v1",
            "supports_cancellation": True,
            "supports_streaming_tools": False
        }

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        self._tools_cache = None
        self.current_response = None
        self.is_cancelling = False
        self.session = requests.Session()

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def _resolve_model_name(self) -> str:
        """
        Returns the model string as configured.
        If no provider suffix (e.g. ':novita', ':together') is present, leaves it clean
        unless a specific default rule applies.
        """
        model_name = self.model.strip() if self.model else "Qwen/Qwen3.8-27B:novita"
        return model_name

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
            logger.error("No tool schemas could be loaded.")
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
            if history and history[-1].get("content") == prompt:
                history = history[:-1]

            for message in history:
                role = message.get("role")
                if role == "model":
                    role = "assistant"
                if role in ["user", "assistant", "system"]:
                    content = message.get("content", "")
                    messages.append({"role": role, "content": content})

        user_content = []
        if attachments:
            for attachment in attachments:
                image_part = image_to_huggingface_format(attachment)
                if image_part:
                    user_content.append(image_part)

        if attachments:
            user_content.append({"type": "text", "text": prompt})
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": prompt})

        return messages

    def _execute_get_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        schema_name: str | None = None
    ) -> dict[str, Any]:
        messages = self._prepare_messages(prompt, attachments, system_prompt, history)
        try:
            max_tokens = int(self.provider_config.get("max_tokens", 4096))
        except (ValueError, TypeError):
            max_tokens = 4096

        resolved_model = self._resolve_model_name()

        request_body = {
            "model": resolved_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False
        }

        use_forced_tool = schema_name and self.require_json_output

        if use_forced_tool:
            logger.debug(f"Attempting to force tool call for schema: {schema_name}")
            all_tools = self._get_tools_from_cache()
            target_tool = next((t for t in all_tools if t.get("function", {}).get("name") == schema_name), None)

            if target_tool:
                request_body["tools"] = [target_tool]
                request_body["tool_choice"] = {"type": "function", "function": {"name": schema_name}}
            else:
                logger.warning(f"Schema '{schema_name}' requested but not found. Proceeding without forced tool.")

        api_url = self.provider_config.get("api_url", "https://router.huggingface.co/v1")
        endpoint = f"{api_url.rstrip('/')}/chat/completions"

        try:
            response = self.session.post(
                endpoint,
                headers=self._get_headers(),
                json=request_body,
                timeout=60
            )
            response.raise_for_status()
            data = response.json()

            text_content = ""
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                text_content = message.get("content") or ""

                tool_calls = message.get("tool_calls", [])
                if tool_calls and use_forced_tool:
                    tool_call = tool_calls[0]
                    func_info = tool_call.get("function", {})
                    fn_name = func_info.get("name")
                    fn_args_str = func_info.get("arguments", "{}")

                    try:
                        final_payload = json.loads(fn_args_str)
                    except json.JSONDecodeError:
                        final_payload = {}

                    while isinstance(final_payload, dict) and 'payload' in final_payload:
                        if len(final_payload.keys()) == 1 or ('type' in final_payload and final_payload.get('type') == fn_name):
                            logger.info(f"Unwrapping nested payload for tool '{fn_name}'.")
                            final_payload = final_payload.get('payload')
                        else:
                            break

                    reconstructed_object = {
                        "type": fn_name,
                        "payload": final_payload
                    }
                    return {"text": text_content, "tool_calls": reconstructed_object}

            return {"text": text_content, "tool_calls": []}

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            error_text = e.response.text
            if status_code == 429:
                raise FatalError(f"HuggingFace quota exhausted ({status_code}): {error_text}") from e
            if 400 <= status_code < 500:
                raise FatalError(f"HuggingFace client error ({status_code}): {error_text}") from e
            elif 500 <= status_code < 600:
                raise RetryableError(f"HuggingFace server error ({status_code}): {error_text}") from e
            else:
                raise FatalError(f"HuggingFace unexpected HTTP error ({status_code}): {error_text}") from e
        except requests.exceptions.RequestException as e:
            raise RetryableError(f"HuggingFace network error: {e}") from e
        except Exception as e:
            logger.error(f"An unexpected error occurred in HuggingFace provider (get_response): {e}", exc_info=True)
            if is_transient_error(e):
                raise RetryableError(f"HuggingFace transient error (get_response): {e}", retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"An unexpected error in HuggingFace provider: {e}") from e

    def cancel_stream(self, generation_id: str = None):
        """Cancels the current active stream by closing the HTTP response connection."""
        if self.current_response:
            logger.info("Attempting to cancel HuggingFace stream.")
            try:
                self.is_cancelling = True
                self.current_response.close()
                logger.info("Successfully closed HuggingFace response stream.")
            except Exception as e:
                logger.error(f"Error closing HuggingFace stream: {e}", exc_info=True)
        else:
            logger.warning("No active HuggingFace stream to cancel.")

    def stream_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        callbacks=None
    ):
        messages = self._prepare_messages(prompt, attachments, system_prompt, history)
        try:
            max_tokens = int(self.provider_config.get("max_tokens", 4096))
        except (ValueError, TypeError):
            max_tokens = 4096

        self.is_cancelling = False
        raw_llm_logger = logging.getLogger('llm_raw_response')

        resolved_model = self._resolve_model_name()

        request_body = {
            "model": resolved_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True
        }

        api_url = self.provider_config.get("api_url", "https://router.huggingface.co/v1")
        endpoint = f"{api_url.rstrip('/')}/chat/completions"

        try:
            with self.session.post(
                endpoint,
                headers=self._get_headers(),
                json=request_body,
                timeout=120,
                stream=True
            ) as response:
                self.current_response = response
                response.raise_for_status()

                for line in response.iter_lines():
                    if self.is_cancelling:
                        logger.info("Stream gracefully cancelled by user request (HuggingFace).")
                        break

                    if not line:
                        continue

                    decoded_line = line.decode('utf-8')
                    raw_llm_logger.info(decoded_line)

                    if decoded_line.startswith('data: '):
                        json_str = decoded_line[len('data: '):].strip()
                        if json_str == '[DONE]':
                            break

                        try:
                            chunk = json.loads(json_str)
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content")
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            logger.warning(f"Could not decode HF stream chunk: {json_str}")

        except requests.exceptions.HTTPError as e:
            if self.is_cancelling:
                logger.info("Stream cancelled during HTTP response processing.")
            else:
                status_code = e.response.status_code
                error_text = e.response.text
                if status_code == 429:
                    raise FatalError(f"HuggingFace quota exhausted ({status_code}): {error_text}") from e
                if 400 <= status_code < 500:
                    raise FatalError(f"HuggingFace client error ({status_code}): {error_text}") from e
                elif 500 <= status_code < 600:
                    raise RetryableError(f"HuggingFace server error ({status_code}): {error_text}") from e
                else:
                    raise FatalError(f"HuggingFace unexpected HTTP error ({status_code}): {error_text}") from e
        except requests.exceptions.RequestException as e:
            if self.is_cancelling:
                logger.info("Stream cancelled during network request.")
            else:
                raise RetryableError(f"HuggingFace network error: {e}") from e
        except Exception as e:
            if self.is_cancelling:
                logger.info("Stream gracefully cancelled by user request (exception suppressed).")
            else:
                logger.error(f"An unexpected error occurred in HuggingFace provider: {e}", exc_info=True)
                if is_transient_error(e):
                    raise RetryableError(f"HuggingFace transient error: {e}", retry_after=extract_retry_after(message=str(e))) from e
                raise FatalError(f"An unexpected error in HuggingFace provider: {e}") from e
        finally:
            logger.info("HuggingFace stream finished or cancelled. Clearing stream reference.")
            self.current_response = None
            self.is_cancelling = False

    def get_generation_id(self, response_chunk) -> str | None:
        return None
