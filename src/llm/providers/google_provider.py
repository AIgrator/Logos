# llm/gemini_provider.py

import json
import logging
from google import genai
from google.genai import types as genai_types

from collections.abc import Mapping, Sequence
from src.llm.base_provider import BaseLLMProvider
from src.llm.exceptions import FatalError, RetryableError
from google.api_core import exceptions as google_exceptions
from src.llm.llm_parsing import repair_json_string

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
            "supports_cancellation": True,
            "supports_streaming_tools": True
        }

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        # Instance-level cache for generated tools module to avoid globals
        # and to allow one-time (fast) attempts per process instance.
        self.cache_gemini_tools = None
        self._cache_gemini_tools_checked = False

        if not self.api_key:
            raise ValueError("Gemini API key is not configured in settings.")

        self.client = genai.Client(api_key=self.api_key)

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
            raise FatalError(f"An unexpected error in Gemini provider: {e}") from e

    def cancel_stream(self, generation_id: str = None):
        """
        For Gemini, cancellation is handled by the LLMWorker by stopping iteration.
        This method exists for interface consistency.
        """
        logging.info("Gemini stream cancellation is handled by the worker. No action needed in provider.")
        pass

