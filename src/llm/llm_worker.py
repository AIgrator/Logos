from PySide6.QtCore import QObject, Signal, Slot
import logging
import json
import copy
from datetime import datetime

from .llm_parsing import repair_json_string, validate_object, universal_fallback_parser
from .exceptions import FatalError, RetryableError
from .error_formatter import format_llm_error
from time import perf_counter
from llm import import_real_provider_classes

class LLMWorker(QObject):
    """
    A persistent worker that runs LLM calls in a separate thread.
    It waits for requests via signals and emits signals with results.
    It now centrally handles parsing and validation of structured responses.
    """

    response_chunk = Signal(str)
    response_finished = Signal(str)
    planning_response_received = Signal(str, object, str)  # reasoning, tool_calls (object), schema_name
    tool_call_received = Signal(dict)
    plain_text_response_received = Signal(str)  # For when a plan was expected but not received
    retry_attempt = Signal(int, int, str, float)  # attempt_num, max_attempts, error, wait_seconds
    retry_countdown = Signal(float)  # remaining seconds before the next automatic retry
    error_occurred = Signal(str)

    def __init__(self, settings_manager, settings):
        super().__init__()
        self.settings_manager = settings_manager
        self.settings = settings
        self.is_cancelled = False
        self.current_generation_id = None

        # Provider is created lazily in the worker thread to avoid blocking the main GUI thread.
        self.provider = None
        # True once a provider instance is ready; distinguishes "still
        # initializing" from "not configured" for user-facing messages.
        self.provider_ready = False

    @Slot(dict)
    def update_settings_and_provider(self, new_settings):
        """Updates the settings and recreates the LLM provider."""
        self.settings = new_settings
        self.provider = self._create_llm_provider(new_settings)
        self.provider_ready = self.provider is not None
        logging.info("LLMWorker settings and provider have been updated.")

    def _create_llm_provider(self, settings):
        """Factory function to create an LLM provider instance based on current settings."""
        active_provider_name = settings.get("active_provider")
        if not active_provider_name:
            logging.warning("No active LLM provider selected in settings.")
            return None

        providers_dict = settings.get("llm_providers", {})
        user_provider_config = providers_dict.get(active_provider_name)

        if not user_provider_config:
            logging.error(f"Configuration for active provider '{active_provider_name}' not found.")
            return None

        # Graceful check for API key before proceeding
        if not user_provider_config.get("api_key"):
            logging.warning(f"API key for active provider '{active_provider_name}' is missing. Cannot create provider.")
            return None

        static_provider_config = self.settings_manager.PROVIDER_CONFIG.get(active_provider_name, {})

        full_config = static_provider_config.copy()
        full_config.update(user_provider_config)
        full_config['name'] = active_provider_name

        from llm import get_provider_class
        provider_class = get_provider_class(active_provider_name)
        if provider_class:
            logging.info(f"Instantiating provider '{active_provider_name}' with model '{full_config.get('model')}'.")
            inst_start = perf_counter()
            instance = provider_class(full_config)
            inst_duration = perf_counter() - inst_start
            logging.info(f"Provider '{active_provider_name}' __init__ completed in {inst_duration:.3f}s")
            return instance

        logging.error(f"Provider class for '{active_provider_name}' not found in PROVIDERS mapping.")
        return None

    @Slot()
    def prepare_provider(self):
        """
        Create the provider inside the worker thread. This is connected to the QThread.started signal.
        It first triggers the eager import of all real provider classes before creating
        the specific instance required.
        """
        if self.provider:
            return
        start = perf_counter()
        try:
            # Phase 2 of provider loading: Import all real provider classes.
            # This is done here in the background thread to avoid blocking the UI
            # and to ensure all modules are available before instantiation.
            import_real_provider_classes()

            self.provider = self._create_llm_provider(self.settings)
            self.provider_ready = self.provider is not None
            duration = perf_counter() - start
            logging.info(f"LLMWorker.prepare_provider completed in {duration:.3f}s")
        except Exception as e:
            logging.error(f"Error preparing provider in worker thread: {e}", exc_info=True)

    def cancel_current_stream(self):
        """
        If there is an active stream and the provider supports cancellation,
        this method will trigger the cancellation. It handles providers that
        require a generation_id (like OpenRouter) and those that don't (like OpenAI).
        """
        if not self.provider:
            logging.warning("Cannot cancel stream: LLM provider is not available.")
            return

        provider_id = self.provider.get_provider_id()

        # First, check if the provider's static config allows cancellation at all.
        if not self.provider.get_provider_config().get("supports_cancellation", False):
            logging.info(f"Cancellation not supported by the '{provider_id}' provider's configuration.")
            return

        # --- Provider-Specific Pre-conditions ---
        if provider_id == "openrouter":
            # OpenRouter needs a generation ID to proceed.
            if not self.current_generation_id:
                logging.warning("Cannot cancel OpenRouter stream: generation ID not yet received.")
                return

            # OpenRouter's support also depends on the sub-model.
            model_name = self.provider.model
            if '/' in model_name:
                sub_provider_id = model_name.split('/')[0]
                provider_properties = self.provider.get_provider_config().get("provider_properties", {})
                sub_provider_display_name = next(
                    (name for name in provider_properties.keys() if name.lower() == sub_provider_id), None
                )
                if sub_provider_display_name:
                    sub_provider_props = provider_properties.get(sub_provider_display_name, {})
                    if not sub_provider_props.get("supports_cancellation", False):
                        logging.info(f"Cancellation not supported for the current OpenRouter model: {model_name}")
                        return
                else:
                    logging.warning(f"Could not determine cancellation support for OpenRouter sub-provider: {sub_provider_id}")

        # --- Execute Cancellation ---
        try:
            if hasattr(self.provider, 'cancel_stream') and callable(self.provider.cancel_stream):
                logging.info(f"Requesting cancellation from '{provider_id}' for generation ID: {self.current_generation_id}")
                self.provider.cancel_stream(self.current_generation_id)
            else:
                logging.warning(f"Provider '{provider_id}' is marked as supporting cancellation, but has no cancel_stream method.")
        except Exception as e:
            logging.error(f"Error calling cancel_stream for provider '{provider_id}': {e}", exc_info=True)

    @Slot(str, str)
    def log_tool_response(self, tool_name, response_text):
        """Logs the response from a tool execution, like web_search."""
        llm_logger = logging.getLogger('llm_communication')
        raw_llm_logger = logging.getLogger('llm_raw_response')

        # Log for llm_communication.log
        comm_log_payload = {
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "direction": "TOOL_RESPONSE",
            "tool_name": tool_name,
            "content": response_text
        }
        llm_logger.info(json.dumps(comm_log_payload, ensure_ascii=False, indent=2))

        # Log for llm_raw_response.log
        raw_log_payload = {
            "type": "tool_response",
            "tool_name": tool_name,
            "response": response_text
        }
        raw_llm_logger.info(json.dumps(raw_log_payload, indent=2))

    @Slot(str, list, str, list, str)
    def execute_request(self, prompt, attachments, system_prompt, history, schema_name=None):
        """
        Executes the LLM request. If a schema_name is provided, it attempts
        to parse and validate the response according to that schema.
        """
        if not self.provider:
            error_message = "LLM provider is not configured. Please check your settings."
            logging.error(error_message)
            self.error_occurred.emit(error_message)
            return

        self.is_cancelled = False
        llm_logger = logging.getLogger('llm_communication')
        raw_llm_logger = logging.getLogger('llm_raw_response')

        # --- Sanitize and Log the outgoing request ---
        log_payload = {
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "direction": "REQUEST",
            "mode": 'Structured' if schema_name else 'Streaming',
            "provider": self.provider.__class__.__name__,
            "model": self.provider.model,
            "attachments_count": len(attachments),
            "history_count": len(history),
            "expected_schema": schema_name
        }

        # Redact image data from prompts for cleaner logging
        clean_system_prompt = copy.deepcopy(system_prompt)
        if isinstance(clean_system_prompt, list):
            for part in clean_system_prompt:
                if isinstance(part, dict) and part.get('type') == 'image_url':
                    part['image_url']['url'] = '[IMAGE DATA]'

        clean_prompt = copy.deepcopy(prompt)
        if isinstance(clean_prompt, list):
            for part in clean_prompt:
                if isinstance(part, dict) and part.get('type') == 'image_url':
                    part['image_url']['url'] = '[IMAGE DATA]'

        log_payload["system_prompt"] = clean_system_prompt
        log_payload["prompt"] = clean_prompt

        llm_logger.info(json.dumps(log_payload, ensure_ascii=False, indent=2))

        max_retries = self.settings.get("max_retries", 3)
        retry_delay_seconds = self.settings.get("retry_delay_seconds", 2)

        for attempt in range(max_retries):
            try:
                # --- Structured Response for Assistant (Planning, Evaluation, etc.) ---
                if schema_name:
                    response_dict = self.provider.get_response(
                        prompt=prompt,
                        attachments=attachments,
                        system_prompt=system_prompt,
                        history=copy.deepcopy(history),
                        schema_name=schema_name
                    )

                    raw_llm_logger.info(json.dumps(response_dict, indent=2))
                    if self.is_cancelled:
                        return

                    tool_calls = response_dict.get("tool_calls", {})
                    raw_text_for_fallback = response_dict.get("text", "")
                    raw_response_str = json.dumps(tool_calls) if tool_calls else raw_text_for_fallback

                    object_to_validate = tool_calls

                    if not object_to_validate and raw_text_for_fallback:
                        logging.warning("No tool_calls received. Attempting to repair raw text fallback.")
                        repaired_object = repair_json_string(raw_text_for_fallback)
                        if repaired_object:
                            logging.info("Successfully repaired raw text into a JSON object.")
                            object_to_validate = repaired_object
                        else:
                            logging.error("Failed to repair raw text into a JSON object.")
                            self.plain_text_response_received.emit(raw_text_for_fallback)
                            return

                    if not object_to_validate:
                        logging.error("No valid object to process after attempting all fallbacks.")
                        self.plain_text_response_received.emit(raw_text_for_fallback)
                        return

                    validated_payload, validation_error = validate_object(object_to_validate, schema_name)
                    final_schema_name = schema_name

                    # --- FALLBACK MECHANISM (Pre-Correction) ---
                    if validation_error and schema_name == 'evaluation':
                        logging.warning(f"Initial validation for '{schema_name}' failed. Activating universal fallback parser.")
                        fallback_payload, fallback_schema_name = universal_fallback_parser(object_to_validate)
                        if fallback_payload:
                            validated_payload = fallback_payload
                            final_schema_name = fallback_schema_name
                            validation_error = None # Resolved by fallback
                        else:
                             logging.error("Universal fallback parser could not recover the response.")

                    # --- SELF-CORRECTION MECHANISM ---
                    if validation_error:
                        logging.warning(f"Validation failed for schema '{schema_name}'. Triggering self-correction mechanism.")

                        from brain import Brain
                        brain = Brain()

                        response_formats = brain.get_response_formats()
                        schema_info = response_formats.get(schema_name, {})
                        schema_example = f"Instructions: {schema_info.get('instructions', 'N/A')}\nExample: {schema_info.get('example', 'N/A')}"

                        corr_system_prompt, corr_user_prompt = brain.get_self_correction_prompts(
                            original_request=str(prompt),
                            schema_example=schema_example,
                            validation_error=validation_error,
                            malformed_response=raw_response_str
                        )

                        if not corr_system_prompt or not corr_user_prompt:
                            raise FatalError("Could not generate self-correction prompts.")

                        logging.info("Sending self-correction request to LLM.")
                        correction_response_dict = self.provider.get_response(
                            prompt=corr_user_prompt,
                            attachments=[],
                            system_prompt=corr_system_prompt,
                            history=[],
                            schema_name=schema_name
                        )
                        raw_llm_logger.info(json.dumps({"self_correction_response": correction_response_dict}, indent=2))

                        corr_tool_calls = correction_response_dict.get("tool_calls", {})
                        corr_raw_text = correction_response_dict.get("text", "")

                        corr_object_to_validate = corr_tool_calls
                        if not corr_object_to_validate and corr_raw_text:
                            corr_object_to_validate = repair_json_string(corr_raw_text)

                        validated_payload, validation_error = validate_object(corr_object_to_validate, schema_name)

                        if validation_error:
                            error_msg = f"Self-correction failed. The LLM did not provide a valid object on the second attempt for schema '{schema_name}'. Final error: {validation_error}"
                            logging.error(error_msg)
                            self.error_occurred.emit(error_msg)
                            return

                        logging.info("Self-correction successful! Proceeding with the corrected object.")

                    log_payload = {
                        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "direction": "RESPONSE",
                        "streamed": False,
                        "content": validated_payload
                    }
                    llm_logger.info(json.dumps(log_payload, ensure_ascii=False, indent=2))

                    payload = validated_payload.get('payload', {}) if final_schema_name in ('terminal_tool_code', 'browser_tool_code') else validated_payload
                    reasoning = payload.get('payload', {}).get('evaluation', {}).get('reason', '') if final_schema_name == 'evaluation' else ""

                    self.planning_response_received.emit(reasoning, payload, final_schema_name)
                    return

                # --- Streaming for Chat ---
                else:
                    supports_streaming_tools = self.provider.get_provider_config().get("supports_streaming_tools", False)
                    self.current_generation_id = None

                    try:
                        full_text_parts = []
                        all_tool_calls = []
                        response_generator = self.provider.stream_response(
                            prompt=prompt,
                            attachments=attachments,
                            system_prompt=system_prompt,
                            history=copy.deepcopy(history)
                        )

                        if supports_streaming_tools:
                            from src.computer.schemas.chat import ChatFunctionCall
                            from pydantic import ValidationError
                            # New path for providers that support structured streaming
                            for chunk in response_generator:
                                if self.is_cancelled:
                                    logging.info("LLM stream request cancelled by user.")
                                    break

                                chunk_type = chunk.get("type")
                                content = chunk.get("content")

                                if chunk_type == "text" and content:
                                    full_text_parts.append(content)
                                    self.response_chunk.emit(content)
                                elif chunk_type == "tool_code" and content:
                                    try:
                                        # Validate the individual tool call chunk
                                        validated_tool_call = ChatFunctionCall.model_validate(content)
                                        validated_content = validated_tool_call.model_dump(by_alias=True)
                                        all_tool_calls.append(validated_content)
                                        self.tool_call_received.emit(validated_content)
                                    except ValidationError as e:
                                        logging.warning(f"Skipping malformed tool call chunk due to validation error: {e}. Chunk: {content}")
                                        continue # Skip this chunk and continue with the stream

                        else:
                            # Legacy path for text-only streaming
                            is_first_chunk = True
                            for chunk in response_generator:
                                if self.is_cancelled:
                                    logging.info("LLM stream request cancelled by user.")
                                    break

                                if is_first_chunk and isinstance(chunk, str) and chunk.startswith("generation_id::"):
                                    self.current_generation_id = chunk.split("::", 1)[1]
                                    logging.info(f"Captured generation ID: {self.current_generation_id}")
                                    is_first_chunk = False
                                    continue

                                is_first_chunk = False
                                if chunk:
                                    chunk_str = str(chunk)
                                    full_text_parts.append(chunk_str)
                                    self.response_chunk.emit(chunk_str)

                        if self.is_cancelled:
                            return

                        full_response_text = "".join(full_text_parts)

                    finally:
                        logging.info(f"Stream finished or cancelled. Resetting generation ID from '{self.current_generation_id}'.")
                        self.current_generation_id = None

                    # Log the final aggregated response
                    log_content = {"text": full_response_text}
                    if all_tool_calls:
                        log_content["tool_calls"] = all_tool_calls

                    log_payload = {
                        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "direction": "RESPONSE",
                        "streamed": True,
                        "content": log_content
                    }
                    llm_logger.info(json.dumps(log_payload, ensure_ascii=False, indent=2))
                    raw_llm_logger.info(json.dumps(log_content, indent=2))

                    self.response_finished.emit(full_response_text)
                    return

            except RetryableError as e:
                provider_name = self.provider.get_provider_id() if self.provider else "the LLM provider"
                error_message = f"A retryable error occurred in LLMWorker: {e}"
                logging.error(error_message, exc_info=True)

                if self.is_cancelled:
                    return

                if attempt < max_retries - 1:
                    # Honor the server-suggested wait (Retry-After / RetryInfo)
                    # instead of the blind backoff when present (capped).
                    suggested = getattr(e, "retry_after", None)
                    try:
                        suggested = float(suggested) if suggested else 0
                    except (TypeError, ValueError):
                        suggested = 0
                    wait_seconds = min(max(retry_delay_seconds, suggested), 120.0)
                    logging.info(f"Attempt {attempt + 1} of {max_retries} failed. Retrying in {wait_seconds} seconds...")
                    self.retry_attempt.emit(attempt + 1, max_retries, str(e), wait_seconds)
                    import time
                    remaining = wait_seconds
                    while remaining > 0 and not self.is_cancelled:
                        time.sleep(min(1.0, remaining))
                        remaining = max(0.0, remaining - 1.0)
                        self.retry_countdown.emit(remaining)
                    retry_delay_seconds *= 2  # Exponential backoff
                else:
                    logging.error(f"All {max_retries} retry attempts failed for a retryable error.")
                    formatted_error = format_llm_error(provider_name, e)
                    if not self.is_cancelled:
                        self.error_occurred.emit(formatted_error)

            except FatalError as e:
                provider_name = self.provider.get_provider_id() if self.provider else "the LLM provider"
                error_message = f"A fatal error occurred in LLMWorker: {e}"
                logging.error(error_message, exc_info=True)

                if not self.is_cancelled:
                    formatted_error = format_llm_error(provider_name, e)
                    self.error_occurred.emit(formatted_error)
                break  # Exit the retry loop immediately

            except Exception as e:
                # Fallback for any other unexpected errors
                provider_name = self.provider.get_provider_id() if self.provider else "the LLM provider"
                error_message = f"An unexpected generic error occurred in LLMWorker: {e}"
                logging.error(error_message, exc_info=True)

                if not self.is_cancelled:
                    formatted_error = format_llm_error(provider_name, e)
                    self.error_occurred.emit(formatted_error)
                break # Treat unexpected errors as fatal

        logging.error("Exiting execute_request after all retries failed.")

    def cancel(self):
        self.cancel_current_stream()
        self.is_cancelled = True
