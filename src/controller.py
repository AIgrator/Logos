import logging
import io
import sys
import threading
import time
import os
import pathspec
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QFileDialog
from PIL import Image

from brain import Brain
from llm.llm_worker import LLMWorker
from settings_manager import SettingsManager
from ui.manager import UIManager
from ui.theme_manager import ThemeManager
from ui.settings_window import SettingsWindow
from handlers.chat_handler import ChatHandler
from handlers.vision_handler import VisionHandler
from handlers.terminal_handler import TerminalHandler
from states import ControllerState, VisionState
from hotkey_manager import HotkeyManager
from src.computer.computer import Computer
from indexer import Indexer
from src.utils.ignore_patterns import DEFAULT_GLOBAL_IGNORE_PATTERNS
# Heavy imports are deferred to background threads
# from vision.worker import VisionWorker

class AppController(QObject):
    llm_request_sent = Signal(str, list, str, list, str)
    # Signals to proxy worker results safely to the main thread
    analysis_finished = Signal(object, object, object)
    analysis_failed = Signal(str)
    vision_module_ready = Signal()
    files_found_signal = Signal(list)

    # --- Signals for thread-safe hotkey handling ---
    hotkey_toggle_mode_triggered = Signal()
    hotkey_pause_resume_triggered = Signal()
    hotkey_stop_execution_triggered = Signal()
    hotkey_pause_for_feedback_triggered = Signal()
    hotkey_cancel_request_triggered = Signal()
    hotkey_regenerate_plan_triggered = Signal()
    hotkey_edit_plan_triggered = Signal()
    hotkey_cancel_plan_triggered = Signal()
    hotkey_run_plan_triggered = Signal()

    def __init__(self, app, settings_manager: SettingsManager, thread_pool, local_server=None, working_directory=None):
        super().__init__()
        init_start = time.perf_counter()
        # Basic references
        self.app = app
        self.settings_manager = settings_manager
        self.theme_manager = ThemeManager(self.settings_manager)
        self.thread_pool = thread_pool
        self.settings = self.settings_manager.all()
        self.working_directory = working_directory if working_directory else os.getcwd()

        # --- Single Instance Lock ---
        self.local_server = local_server
        if self.local_server:
            self.local_server.newConnection.connect(self._handle_new_instance_connection)
        # --------------------------

        # Core services (lightweight part)
        self.brain = Brain(settings_manager)
        self.vision = None  # Will be initialized in a background thread
        self.is_vision_module_initialized = False
        self.indexer = Indexer('.')
        self.last_search_query = None
        self.indexer.index_updated.connect(self._on_index_updated)

        # --- Background Vision Initialization ---
        self.vision_thread = None
        self.vision_worker = None
        # --- Background Screenshotting ---
        self.screenshot_thread = QThread()
        self.screenshot_worker = None
        # ------------------------------------

        # --- Centralized Validator ---
        from llm.llm_parsing import validate_object
        self.validator = lambda data_object, schema_name: validate_object(data_object, schema_name)
        # ---------------------------

        # UI and state
        ui_start = time.perf_counter()
        self.ui = UIManager(self)
        ui_duration = time.perf_counter() - ui_start
        logging.info(f"UIManager initialized in {ui_duration:.3f}s")
        self.state = ControllerState.IDLE
        self.conversation_history = []
        self.pending_attachments = []
        self.thinking_timer = QTimer(self)
        self.thinking_time = 0
        self.settings_window = None

        # Handlers
        comp_start = time.perf_counter()
        self.computer = Computer(self.settings_manager, mode="chat")
        self.vision_handler = VisionHandler(self)
        self.terminal_handler = TerminalHandler(self, self.theme_manager, working_directory=self.working_directory)
        comp_duration = time.perf_counter() - comp_start
        logging.info(f"LocalDesktopComputer initialized in {comp_duration:.3f}s")

        # Hotkey Manager
        self.hotkey_manager = HotkeyManager(self, self.settings_manager)

        # LLM worker thread
        self.llm_thread = QThread()
        llm_start = time.perf_counter()
        self.llm_worker = LLMWorker(self.settings_manager, self.settings)
        llm_duration = time.perf_counter() - llm_start
        logging.info(f"LLMWorker instantiated in {llm_duration:.3f}s")
        self.llm_worker.moveToThread(self.llm_thread)
        # Prepare provider inside the worker thread to avoid blocking the main thread
        try:
            self.llm_thread.started.connect(self.llm_worker.prepare_provider)
        except Exception:
            # If the slot is missing for some reason, ignore and continue
            pass

        # Now that the worker is created, initialize handlers that depend on it
        self.chat_handler = ChatHandler(self)
        self.current_handler = self.chat_handler

        # Connect proxied signals to the handler
        self.analysis_finished.connect(self.vision_handler._on_analysis_complete)
        self.analysis_failed.connect(self.vision_handler._on_analysis_error)

        # Connect LLM worker signals
        self.llm_worker.response_chunk.connect(self.handle_response_chunk)
        self.llm_worker.response_finished.connect(self._handle_chat_response)
        self.llm_worker.planning_response_received.connect(self._handle_plan_response)
        self.llm_worker.plain_text_response_received.connect(self._handle_plain_text_response)
        self.llm_worker.retry_attempt.connect(self._handle_retry_attempt)
        self.llm_worker.error_occurred.connect(self.handle_llm_error)
        self.llm_request_sent.connect(self.llm_worker.execute_request)

        # Thread lifecycle
        self.app.aboutToQuit.connect(self.handle_shutdown)
        self.app.aboutToQuit.connect(self.llm_thread.quit)
        self.llm_thread.finished.connect(self.llm_worker.deleteLater)
        self.llm_thread.finished.connect(self.llm_thread.deleteLater)
        self.llm_thread.start()

        total_init_duration = time.perf_counter() - init_start
        logging.info(f"AppController.__init__ complete in {total_init_duration:.3f}s (UI: {ui_duration:.3f}s, LLMWorker: {llm_duration:.3f}s, LocalComputer: {comp_duration:.3f}s)")

        # --- Connect UI Signals ---
        self.ui.shutdown_requested.connect(self.handle_shutdown)
        self.ui.user_command_submitted.connect(self.handle_user_input)
        self.ui.plan_approved.connect(self._handle_plan_approval)
        self.ui.plan_cancel_clicked.connect(self._handle_plan_cancel)
        self.ui.regenerate_plan_clicked.connect(self._handle_regenerate_plan)
        self.ui.edit_plan_clicked.connect(self._handle_edit_plan)
        self.ui.screenshot_button_clicked.connect(self._handle_screenshot_request)
        self.ui.add_file_clicked.connect(self._handle_add_file_request)
        self.ui.add_folder_clicked.connect(self._handle_add_folder_request)
        self.ui.settings_button_clicked.connect(self._show_settings_window)
        self.ui.attachment_removed.connect(self._handle_remove_attachment)
        self.ui.chat_mode_selected.connect(self._activate_chat_mode)
        self.ui.terminal_mode_selected.connect(self._activate_terminal_mode)
        self.ui.vision_mode_selected.connect(self._activate_vision_mode)
        self.ui.cancel_requested.connect(self.handle_cancel_request)
        self.ui.pause_resume_clicked.connect(self._handle_pause_resume)
        self.ui.pause_for_feedback_clicked.connect(self._handle_pause_for_feedback)
        self.ui.stop_clicked.connect(self._handle_stop)
        self.ui.next_step_clicked.connect(self._handle_next_step)
        self.ui.toggle_fullscreen_button_clicked.connect(self._on_toggle_fullscreen_clicked)
        self.ui.file_search_requested.connect(self.search_files)
        self.ui.image_pasted.connect(self._handle_pasted_image)
        self.ui.files_pasted.connect(self._handle_pasted_files)
        self.ui.files_dropped.connect(self._handle_pasted_files)
        self.ui.path_selected_for_attachment.connect(self._handle_path_mention_for_attachment)

        # Thinking timer and settings change hook
        self.thinking_timer.timeout.connect(self._update_thinking_timer)
        self.settings_manager.settings_changed.connect(self._on_settings_changed)

        # Connect hotkey signals to slots
        self.hotkey_toggle_mode_triggered.connect(self.hotkey_toggle_mode)
        self.hotkey_pause_resume_triggered.connect(self.hotkey_pause_resume)
        self.hotkey_stop_execution_triggered.connect(self.hotkey_stop_execution)
        self.hotkey_pause_for_feedback_triggered.connect(self.hotkey_pause_for_feedback)
        self.hotkey_cancel_request_triggered.connect(self.hotkey_cancel_request)
        self.hotkey_regenerate_plan_triggered.connect(self.hotkey_regenerate_plan)
        self.hotkey_edit_plan_triggered.connect(self.hotkey_edit_plan)
        self.hotkey_cancel_plan_triggered.connect(self.hotkey_cancel_plan)
        self.hotkey_run_plan_triggered.connect(self.hotkey_run_plan)




    def execute_llm_request(self, system_prompt: str, user_prompt: str, schema_name: str = "chat") -> str:
        """
        Provides a direct, blocking way for handlers to make an LLM request.
        This is primarily for the 'Plan B' logic in TerminalHandler.
        Returns the plain text response from the LLM.
        """
        # This event will be used to wait for the response
        response_event = threading.Event()
        response_data = {"response": None, "error": None}

        @Slot(str)
        def on_response(response_text):
            response_data["response"] = response_text
            response_event.set()

        @Slot(str)
        def on_error(error_message):
            response_data["error"] = error_message
            response_event.set()

        # Temporarily connect signals for this specific request
        # Use plain_text_response_received for JSON mode as it's the designated fallback
        if schema_name and schema_name != "chat":
             self.llm_worker.plain_text_response_received.connect(on_response)
        else: # For simple chat/text mode
             self.llm_worker.response_finished.connect(on_response)
        self.llm_worker.error_occurred.connect(on_error)

        # Emit the request
        self.llm_request_sent.emit(user_prompt, [], system_prompt, [], schema_name)

        # Wait for the response_event to be set by one of the slots
        response_event.wait(timeout=120) # 120-second timeout

        # Disconnect the temporary signals to avoid side effects
        try:
            if schema_name and schema_name != "chat":
                self.llm_worker.plain_text_response_received.disconnect(on_response)
            else:
                self.llm_worker.response_finished.disconnect(on_response)
            self.llm_worker.error_occurred.disconnect(on_error)
        except (TypeError, RuntimeError) as e:
            # This can happen if the signal was already disconnected or the worker was destroyed
            logging.warning(f"Could not disconnect temporary signals: {e}")


        if response_data["error"]:
            raise RuntimeError(f"LLM request failed: {response_data['error']}")

        if response_data["response"] is None:
            raise TimeoutError("LLM request timed out after 120 seconds.")

        return response_data["response"]

    def handle_shutdown(self):
        """Initiates a graceful shutdown with a timeout, then forces exit."""
        logging.info("Shutdown requested. Cleaning up resources with a 1-second timeout...")

        # Run cleanup in a daemon thread. If the main thread exits, this one will be killed.
        cleanup_thread = threading.Thread(target=self.cleanup, daemon=True)
        cleanup_thread.start()

        # Wait for the cleanup thread to finish, but for a maximum of 1 second.
        cleanup_thread.join(timeout=1.0)

        if cleanup_thread.is_alive():
            logging.warning("Cleanup took too long. Forcing exit now.")
        else:
            logging.info("Cleanup complete. Forcing exit.")

        sys.exit(0)

    def cleanup(self):
        """Stops all background processes and animations before quitting."""
        logging.info("AppController cleanup initiated.")
        if self.indexer:
            logging.info("Stopping file indexer.")
            self.indexer.stop()
        if self.hotkey_manager:
            logging.info("Stopping hotkey manager.")
            self.hotkey_manager.stop()

    # --- Thread-safe Hotkey Requesters ---
    # These methods are called from the background thread of the keyboard listener.
    # They emit signals that are safely handled by the main Qt event loop.

    def request_hotkey_toggle_mode(self):
        self.hotkey_toggle_mode_triggered.emit()

    def request_hotkey_pause_resume(self):
        self.hotkey_pause_resume_triggered.emit()

    def request_hotkey_stop_execution(self):
        self.hotkey_stop_execution_triggered.emit()

    def request_hotkey_pause_for_feedback(self):
        self.hotkey_pause_for_feedback_triggered.emit()

    def request_hotkey_cancel_request(self):
        self.hotkey_cancel_request_triggered.emit()

    def request_hotkey_regenerate_plan(self):
        self.hotkey_regenerate_plan_triggered.emit()

    def request_hotkey_edit_plan(self):
        self.hotkey_edit_plan_triggered.emit()

    def request_hotkey_cancel_plan(self):
        self.hotkey_cancel_plan_triggered.emit()

    def request_hotkey_run_plan(self):
        self.hotkey_run_plan_triggered.emit()

    def run(self):
        self.ui.show_main_window()
        title_string = self._get_llm_title_string()
        self.ui.set_main_window_title(title_string)
        self.ui.set_title_bar_text(title_string)
        self._start_vision_initialization()
        self.indexer.start()
        self._check_initial_configuration()

    def _check_initial_configuration(self):
        """Checks if the active provider has a configured API key (from keyring) and shows a message if not."""
        try:
            active_provider_name = self.settings.get("active_provider")
            providers = self.settings.get("llm_providers", {})
            provider_config = providers.get(active_provider_name, {})

            if not provider_config.get("api_key"):
                self.ui.add_message(
                    "<i>Welcome to Logos AI Assistant! To get started, please go to <b>Settings</b>, select an AI provider, and enter your API key.</i>",
                    force_scroll=True
                )
        except Exception as e:
            logging.warning(f"Could not perform initial configuration check: {e}")

    def _start_file_indexing(self):
        """Moves the file indexer to a background thread and starts indexing."""
        self.indexer_thread.start()
        logging.info("Started background file indexing.")

    def _on_index_updated(self):
        """
        Slot that triggers when the indexer has finished updating.
        If a search is currently active, it re-runs the search to refresh the UI.
        """
        if self.last_search_query is not None:
            logging.debug(f"Index updated, re-running search for: '{self.last_search_query}'")
            self.search_files(self.last_search_query)

    def search_files(self, query: str):
        """Performs a file search and emits the results."""
        self.last_search_query = query if query else None
        results = self.indexer.search(query)
        self.files_found_signal.emit(results)

    def _start_vision_initialization(self):
        """Imports and initializes the vision module in a background thread."""
        self.ui.set_vision_mode_loading(True)

        try:
            from vision.worker import VisionWorker
        except ImportError as e:
            logging.error(f"Failed to import VisionWorker: {e}", exc_info=True)
            self._on_vision_init_error(f"Could not import VisionWorker: {e}")
            return

        self.vision_thread = QThread()
        self.vision_worker = VisionWorker(self.settings)
        self.vision_worker.moveToThread(self.vision_thread)

        self.vision_worker.vision_ready.connect(self._on_vision_instance_ready)
        self.vision_worker.finished.connect(self._on_vision_init_finished)
        self.vision_worker.error.connect(self._on_vision_init_error)

        self.vision_thread.started.connect(self.vision_worker.run)
        self.vision_thread.finished.connect(self.vision_thread.quit)
        self.vision_thread.finished.connect(self.vision_worker.deleteLater)
        self.vision_thread.finished.connect(self.vision_thread.deleteLater)

        self.app.aboutToQuit.connect(self.vision_thread.quit)

        self.vision_thread.start()
        logging.info("Started background initialization for Vision module.")

    def _on_vision_instance_ready(self, vision_instance):
        """Receives the fully initialized VisionManager instance from the worker."""
        logging.info("VisionManager instance received from background thread.")
        self.vision = vision_instance
        self.vision_handler.computer.set_vision_manager(vision_instance)

    def _on_vision_init_finished(self):
        """Callback for when the vision module is ready."""
        logging.info("Vision module initialization finished successfully.")
        # Stop the animation first, then update the button to its final state
        if self.ui and hasattr(self.ui, 'spinner_animation'):
            self.ui.spinner_animation.stop()
        self.ui.set_vision_mode_loading(False)
        self.is_vision_module_initialized = True
        self.vision_module_ready.emit()

    def _on_vision_init_error(self, error_message):
        logging.error(f"Vision initialization failed: {error_message}")
        self.is_vision_module_initialized = False
        self.ui.add_message(f"<b>Critical Error:</b> {error_message}", force_scroll=True)

    def _set_state(self, new_state: ControllerState):
        logging.info(f"AppController state changed from {self.state.name} to {new_state.name}")
        self.state = new_state

    def handle_user_input(self, user_text):
        if not self.llm_worker.provider:
            self.ui.add_message(
                "<i>To get started, please go to <b>Settings</b>, select an AI provider, and enter your API key.</i>",
                force_scroll=True
            )
            return

        if not user_text and not self.pending_attachments:
            return

        # Set the assistant name in the UI at the beginning of the interaction
        model_name = self._get_current_model_name()
        self.ui.set_assistant_name(model_name)

        self.ui.add_message(f"<b>You:</b> {user_text}")
        self.conversation_history.append({'role': 'user', 'content': user_text})

        # Process attachments based on the current mode
        attachments_for_handler = []
        if self.pending_attachments:
            if self.state == ControllerState.TERMINAL:
                for attachment in self.pending_attachments:
                    try:
                        attachment_type = attachment.get('type')
                        path = attachment.get('path')
                        if attachment_type == 'folder':
                            file_list_str = self._generate_filtered_file_list(path)
                            self.terminal_handler.command_history.append(file_list_str)
                            self.ui.add_message(f"<i>Folder attached for context: <code>{path}</code></i>", force_scroll=True)
                        elif attachment_type == 'file':
                            with open(path, encoding='utf-8') as f:
                                content = f.read()
                            file_info_for_llm = (
                                f"**Attached File:** `{path}`\n"
                                f"**File Content:**\n```\n{content}\n```"
                            )
                            self.terminal_handler.command_history.append(file_info_for_llm)
                            self.ui.add_message(f"<i>File attached for context: <code>{path}</code></i>", force_scroll=True)
                    except Exception as e:
                        logging.error(f"Error processing attachment for terminal: {e}")
                        self.ui.add_error_message(f"Error processing attachment {path}: {e}")
            else: # Chat or Vision mode
                for attachment in self.pending_attachments:
                    try:
                        if isinstance(attachment, dict):
                            attachment_type = attachment.get('type')
                        else:
                            # Raw payload (e.g. screenshot bytes queued directly).
                            attachments_for_handler.append(attachment)
                            continue
                        if attachment_type == 'file':
                             with open(attachment.get('path'), 'rb') as f:
                                attachments_for_handler.append(f.read())
                        elif attachment_type == 'image':
                            attachments_for_handler.append(attachment.get('data'))
                    except Exception as e:
                        logging.error(f"Error processing attachment for chat/vision: {e}")
                        self.ui.add_error_message(f"Could not process attachment: {e}")

        self.pending_attachments.clear()
        self.ui.clear_thumbnails()
        self._update_attachment_menu()

        if self.state == ControllerState.VISION:
            if self.vision_handler.state == VisionState.PAUSED_ON_ERROR:
                self.vision_handler.resume_with_guidance(user_text, attachments_for_handler)
            else:
                self.vision_handler.start_task(user_text, attachments_for_handler)
        elif self.state == ControllerState.TERMINAL:
            self.terminal_handler.start_task(user_text)
        else:
            self.chat_handler.start_chat(user_text, attachments_for_handler)

    def handle_response_chunk(self, chunk):
        self.current_handler.handle_response_chunk(chunk)

    def _handle_chat_response(self, full_response):
        if self.state == ControllerState.CHATTING:
            self.chat_handler.handle_chat_response(full_response)
        else:
            # The full response is already logged to the llm_communication.log file.
            # This warning is just to indicate that an unexpected response type was received.
            logging.warning("Received a simple chat response while in Vision mode. Ignoring.")

    def _handle_plan_response(self, reasoning: str, tool_calls_obj, schema_name: str):
        # The full response is already logged to the llm_communication.log file.
        if self.state == ControllerState.VISION:
            self.vision_handler.handle_plan_response(reasoning, tool_calls_obj, schema_name)
        elif self.state == ControllerState.TERMINAL:
            self.terminal_handler.handle_llm_response(reasoning, tool_calls_obj, schema_name)
        else:
            logging.warning("Received a plan response while in chat mode. Ignoring.")

    def _handle_plain_text_response(self, text: str):
        """Handles the case where a plain text response was received instead of a plan."""
        # The full response is already logged to the llm_communication.log file.
        logging.warning("Received unexpected plain text response in vision mode.")
        self.thinking_timer.stop()
        self.ui.show_thinking_ui(False)

        if self.state == ControllerState.VISION:
            model_name = self._get_current_model_name()

            # Sanitize text for HTML display
            import html
            escaped_text = html.escape(text)

            message = (
                f"<b>{model_name}:</b> I received an unexpected response instead of a plan. Here is the raw response:\n"
                f"<pre>{escaped_text}</pre>\n"
                "Your original request has been restored in the input field. Would you like to try sending it again?"
            )
            self.ui.add_message(message, force_scroll=True)

            # Restore the original user request to the input field
            if self.vision_handler.original_user_request:
                self.ui.user_input.setText(self.vision_handler.original_user_request)

            # Reset the state to allow for a new attempt
            self.vision_handler._set_state(VisionState.IDLE)
        else:
            logging.info("Ignoring plain text response as we are not in vision mode.")

    def _handle_retry_attempt(self, attempt_num, max_attempts, error):
        """Updates the UI to show that a retry is happening."""
        logging.info(f"Handling retry attempt {attempt_num}/{max_attempts} in UI.")
        self.ui.show_thinking_ui(True, f"Network error, retrying ({attempt_num}/{max_attempts})...")

    def handle_llm_error(self, error_message):
        """
        Handles a fatal LLM error after all retries have failed.
        Routes the error to the appropriate handler based on the current state.
        """
        self.thinking_timer.stop()
        self.ui.show_thinking_ui(False)

        if self.state == ControllerState.VISION:
            # The vision handler will manage UI changes (like switching to full mode)
            self.vision_handler.handle_fatal_error(error_message)
        elif self.state == ControllerState.CHATTING:
            self.chat_handler.handle_fatal_error(error_message)
        else:
            # Fallback for IDLE state or any other unexpected state
            self.ui.add_message(
                f"<b>A critical error occurred:</b> {error_message}<br>"
                "Please try your request again.",
                force_scroll=True
            )
            self._set_state(ControllerState.IDLE)

    def handle_cancel_request(self):
        if self.llm_worker:
            self.llm_worker.cancel()
        self.thinking_timer.stop()
        self.ui.show_thinking_ui(False)
        self.ui.add_message("<i>Request cancelled.</i>", force_scroll=True)

        # If we are in vision mode, just reset the vision handler to IDLE
        # but keep the controller in ASSISTING state. Otherwise, go back to global IDLE.
        if self.state == ControllerState.VISION:
            self.vision_handler._set_state(VisionState.IDLE)
        else:
            self._set_state(ControllerState.IDLE)

    def _get_current_model_name(self):
        """Gets the simple model name for use inside the chat UI."""
        try:
            active_provider_name = self.settings.get("active_provider")
            providers = self.settings.get("llm_providers", {})
            provider_settings = providers.get(active_provider_name, {})
            return provider_settings.get("model", "Logos AI Assistant")
        except Exception:
            return "Logos AI Assistant"

    def _format_openrouter_model_id(self, display_name: str) -> str:
        """Converts a display name like 'Claude Opus 4.1' to 'claude-opus-4.1'."""
        return display_name.lower().replace(' ', '-')

    def _get_llm_title_string(self) -> str:
        """Generates a detailed, user-friendly string for window titles."""
        try:
            active_provider_name = self.settings.get("active_provider")
            providers = self.settings.get("llm_providers", {})
            provider_config = providers.get(active_provider_name)

            if not provider_config:
                return "Logos AI Assistant"

            model_name = provider_config.get("model", "Unknown Model")
            provider_static_config = self.settings_manager.PROVIDER_CONFIG.get(active_provider_name, {})
            provider_display_name_main = provider_static_config.get("display_name", active_provider_name.replace('_', ' ').title())

            if active_provider_name == "openrouter":
                sub_provider_id, model_id = model_name.split('/', 1)
                models_by_provider = provider_static_config.get("models_by_provider", {})

                provider_display_name_sub = next(
                    (name for name in models_by_provider.keys() if name.lower() == sub_provider_id),
                    sub_provider_id.upper()
                )

                model_display_name = model_id
                if provider_display_name_sub in models_by_provider:
                    model_display_name = next(
                        (name for name in models_by_provider[provider_display_name_sub] if self._format_openrouter_model_id(name) == model_id),
                        model_id
                    )
                return f"OpenRouter: {provider_display_name_sub} - {model_display_name}"

            return f"{provider_display_name_main} - {model_name}"
        except Exception as e:
            logging.error(f"Failed to generate LLM title string: {e}", exc_info=True)
            return "Logos AI Assistant"

    def _on_settings_changed(self, new_settings):
        self.settings = new_settings
        if self.vision:
            self.vision.update_capture_settings(new_settings)
            # Also notify the parser adapter of potential settings changes
            if hasattr(self.vision, 'parser_adapter'):
                self.vision.parser_adapter.update_settings(new_settings)

        self.llm_worker.update_settings_and_provider(new_settings)

        self.chat_handler.settings = new_settings
        self.vision_handler.settings = new_settings
        logging.info("Settings have been updated and providers reloaded.")

        title_string = self._get_llm_title_string()
        self.ui.set_main_window_title(title_string)
        self.ui.set_title_bar_text(title_string)

    def _cleanup_and_switch_handler(self, new_handler, new_state):
        # If a response is streaming, cancel it before switching modes.
        if self.thinking_timer.isActive():
            logging.info(f"Cancelling in-flight request before switching to {new_state.name} mode.")
            if self.llm_worker:
                self.llm_worker.cancel()
            self.thinking_timer.stop()
            self.ui.show_thinking_ui(False)

        # If the current handler is the vision handler, ensure it's stopped cleanly.
        if self.current_handler == self.vision_handler:
            self.vision_handler.stop()

        self._set_state(new_state)
        self.current_handler = new_handler

    def _activate_chat_mode(self):
        self._cleanup_and_switch_handler(self.chat_handler, ControllerState.CHATTING)
        self.ui.set_active_mode('chat')
        self.ui.add_message("<i>Chat mode enabled.</i>", force_scroll=True)

    def _activate_terminal_mode(self):
        self._cleanup_and_switch_handler(self.terminal_handler, ControllerState.TERMINAL)
        self.ui.set_active_mode('terminal')
        self.ui.add_message("<i>Terminal mode enabled. I will now use terminal commands to accomplish tasks.</i>", force_scroll=True)

    def _activate_vision_mode(self):
        self._cleanup_and_switch_handler(self.vision_handler, ControllerState.VISION)
        self.ui.set_active_mode('vision')
        self.ui.add_message("<i>Vision mode enabled. Describe the task you want me to perform.</i>", force_scroll=True)
        self.vision_handler._set_state(VisionState.IDLE)

    def _deactivate_vision_mode(self):
        """Switches back to chat mode. Maintained for compatibility."""
        logging.info("Deactivating vision mode and switching to chat mode.")
        self._activate_chat_mode()

    def _handle_plan_approval(self):
        if self.state == ControllerState.VISION:
            self.vision_handler.handle_plan_approval()

    def _handle_regenerate_plan(self):
        """Triggered when the user clicks Regenerate: ask the vision handler to re-request a plan."""
        if self.state == ControllerState.VISION:
            # Set the assistant name in the UI before regenerating the plan
            model_name = self._get_current_model_name()
            self.ui.set_assistant_name(model_name)
            self.vision_handler.regenerate_plan()

    def _handle_plan_cancel(self):
        """Called when the user clicks Cancel on the plan controls.
        Clears the stored strategic plan and restores attachment buttons in the UI.
        """
        if self.state == ControllerState.VISION:
            # Instruct the vision handler to forget the plan
            try:
                self.vision_handler.clear_plan()
            except Exception as e:
                logging.error(f"Failed to clear plan: {e}")

            # Restore attachment buttons and hide plan controls
            try:
                self.ui.set_plan_controls_visible(False)
            except Exception as e:
                logging.error(f"Failed to update UI after plan cancel: {e}")

    def _handle_edit_plan(self):
        """Open the plan editor, validate and apply the edited plan.

        UX: The editor returns an edited plan. We accept it only if it passes
        validation in vision.apply_edited_plan. After Save, the
        user stays in chat and must click Approve & Run to start execution.
        """
        if self.state != ControllerState.VISION or self.vision_handler.state != VisionState.WAITING_FOR_APPROVAL:
            self.ui.add_message("<i>Edit is only available when a strategic plan is awaiting your approval.</i>", force_scroll=True)
            return

        # Open editor with the current plan (fallback to empty list)
        try:
            current_plan = self.vision_handler.action_plan or []
            edited = self.ui.open_plan_editor(current_plan)
            if edited is None:
                # User cancelled or error
                return

            accepted = self.vision_handler.apply_edited_plan(edited)
            if accepted:
                self.ui.add_message("<i>Edited plan saved. Please click 'Approve and Run' to start execution.</i>", force_scroll=True)
            else:
                self.ui.add_message("<i>Edited plan was rejected. Please fix errors and try again.</i>", force_scroll=True)
        except Exception as e:
            logging.error(f"Failed to open/apply plan editor: {e}")
            self.ui.add_message("<i>Could not open the plan editor due to an internal error.</i>", force_scroll=True)

    def _handle_stop(self):
        if self.state == ControllerState.VISION:
            self.vision_handler.stop()
        self._activate_chat_mode() # Fallback to chat mode after stopping

    def _handle_pause_resume(self):
        if self.state == ControllerState.VISION:
            self.vision_handler.pause_or_resume()

    def _handle_pause_for_feedback(self):
        if self.state == ControllerState.VISION:
            self.vision_handler.pause_for_feedback()

    def _handle_next_step(self):
        if self.state == ControllerState.VISION:
            self.vision_handler.next_step()

    def _update_thinking_timer(self):
        self.thinking_time += 1
        self.ui.update_timer(self.thinking_time)

    def _handle_screenshot_request(self):
        if self._get_image_attachment_count() >= 3:
            self.ui.add_message("<i>You can attach a maximum of 3 images.</i>")
            return

        if len(self.pending_attachments) >= 50:
            self.ui.add_message("<i>You can attach a maximum of 50 files.</i>")
            return

        try:
            from vision.screenshot_worker import ScreenshotWorker
        except ImportError as e:
            logging.error(f"Failed to import ScreenshotWorker: {e}", exc_info=True)
            self.ui.add_message("<i>Error: Could not import ScreenshotWorker.</i>")
            return

        # Reuse the thread if it's not running, otherwise it's safe to start again
        if not self.screenshot_thread.isRunning():
            self.screenshot_worker = ScreenshotWorker(self.ui.main_window)
            self.screenshot_worker.moveToThread(self.screenshot_thread)

            # Connect signals from the worker to slots in the controller
            self.screenshot_worker.screenshot_finished.connect(self._on_screenshot_finished)
            self.screenshot_worker.screenshot_failed.connect(self._on_screenshot_failed)

            # When the thread starts, it will trigger the worker's run method
            self.screenshot_thread.started.connect(self.screenshot_worker.run)

            # Clean up the worker and thread when done
            self.screenshot_worker.screenshot_finished.connect(self.screenshot_thread.quit)
            self.screenshot_worker.screenshot_failed.connect(self.screenshot_thread.quit)
            self.screenshot_thread.finished.connect(self.screenshot_worker.deleteLater)

            self.screenshot_thread.start()
            self.ui.add_message("<i>Taking screenshot...</i>")
        else:
            logging.warning("Screenshot request ignored, another one is already in progress.")
            self.ui.add_message("<i>A screenshot is already being taken. Please wait.</i>")

    def _on_screenshot_finished(self, screenshot_output):
        """Handles the successful completion of a background screenshot."""
        logging.info("Background screenshot capture finished successfully.")
        try:
            if isinstance(screenshot_output, Image.Image):
                image_for_thumbnail = screenshot_output
                buffer = io.BytesIO()
                image_for_thumbnail.save(buffer, format='WEBP', lossless=True)
                screenshot_bytes = buffer.getvalue()
            else:
                screenshot_bytes = screenshot_output
                image_for_thumbnail = Image.open(io.BytesIO(screenshot_bytes))

            self.pending_attachments.append({'type': 'image', 'data': screenshot_bytes})
            self.ui.add_attachment(image_for_thumbnail)
            self._update_attachment_menu()
        except Exception as e:
            logging.error(f"Failed to process screenshot after capture: {e}", exc_info=True)
            self.ui.add_message("<i>Could not process the screenshot.</i>")
        finally:
            # Ensure the thread is cleaned up
            if self.screenshot_thread.isRunning():
                self.screenshot_thread.quit()

    def _on_screenshot_failed(self, error_message):
        """Handles the failure of a background screenshot."""
        logging.error(f"Background screenshot capture failed: {error_message}")
        self.ui.add_message(f"<i>Screenshot failed: {error_message}</i>")
        # Ensure the thread is cleaned up
        if self.screenshot_thread.isRunning():
            self.screenshot_thread.quit()

    def _get_image_attachment_count(self):
        """Counts how many of the pending attachments are images."""
        import os
        IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp']
        image_count = 0
        for attachment in self.pending_attachments:
            if attachment.get('type') == 'image':
                image_count += 1
            elif attachment.get('type') == 'file':
                path = attachment.get('path', '')
                _, ext = os.path.splitext(path.lower())
                if ext in IMAGE_EXTENSIONS:
                    image_count += 1
        return image_count

    def _update_attachment_menu(self):
        """Updates the UI menu state based on current attachment counts."""
        image_count = self._get_image_attachment_count()
        total_count = len(self.pending_attachments)
        self.ui.update_attachment_menu_state(image_count, total_count)

    def _generate_filtered_file_list(self, path: str) -> str:
        """
        Scans a directory, filters out ignored files/folders using .gitignore
        or a global default list, and returns a structured string listing
        the contents for the LLM context.
        """
        # Find the .gitignore file
        gitignore_path = None
        current_dir = path
        while current_dir and current_dir != os.path.dirname(current_dir):
            potential_gitignore = os.path.join(current_dir, '.gitignore')
            if os.path.exists(potential_gitignore):
                gitignore_path = potential_gitignore
                break
            current_dir = os.path.dirname(current_dir)

        patterns = []
        if gitignore_path:
            try:
                with open(gitignore_path, encoding='utf-8') as f:
                    patterns = f.readlines()
                logging.info(f"Using .gitignore from: {gitignore_path}")
            except Exception as e:
                logging.warning(f"Could not read .gitignore at {gitignore_path}: {e}")
                patterns = DEFAULT_GLOBAL_IGNORE_PATTERNS
        else:
            patterns = DEFAULT_GLOBAL_IGNORE_PATTERNS
            logging.info("No .gitignore found, using default global ignore patterns.")

        spec = pathspec.PathSpec.from_lines('gitwildmatch', patterns)

        file_paths = []
        for root, _, files in os.walk(path):
            for file in files:
                full_path = os.path.join(root, file)
                relative_path = os.path.relpath(full_path, path)

                # Use pathspec to check if the file should be ignored
                if not spec.match_file(relative_path):
                    file_paths.append(relative_path)

        if not file_paths:
            return f"**Attached Folder:** `{path}` (The folder is empty or contains only ignored files)."

        file_list_str = "\n".join(f"- `{f}`" for f in sorted(file_paths))
        return (
            f"**Attached Folder:** `{path}`\n"
            f"**Files within the folder:**\n{file_list_str}"
        )

    def _add_attachments_from_paths(self, paths: list):
        """
        A unified method to handle adding files and folders as attachments.
        It processes a list of paths, adding them as structured dictionaries
        to `pending_attachments` and updating the UI. Folders are only
        added in Terminal mode.
        """
        IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp']

        for path in paths:
            if len(self.pending_attachments) >= 50:
                self.ui.add_message("<i>Maximum of 50 files reached. Some items were not added.</i>")
                break

            abs_path = os.path.abspath(path)
            if not os.path.exists(abs_path):
                logging.warning(f"Path '{path}' does not exist. Skipping.")
                continue

            is_folder = os.path.isdir(abs_path)

            if is_folder:
                if self.state == ControllerState.TERMINAL:
                    self.pending_attachments.append({'type': 'folder', 'path': abs_path})
                    self.ui.add_attachment(abs_path, is_folder=True)
                else:
                    # Silently ignore folder additions in non-terminal modes as the UI option is hidden.
                    logging.warning(f"Folder attachment '{abs_path}' ignored in non-terminal mode.")
            else: # Is a file
                _, ext = os.path.splitext(abs_path.lower())
                if ext in IMAGE_EXTENSIONS:
                    if self._get_image_attachment_count() >= 3:
                        self.ui.add_message(f"<i>You can only attach a maximum of 3 images. Skipping {os.path.basename(abs_path)}.</i>")
                        continue

                self.pending_attachments.append({'type': 'file', 'path': abs_path})
                self.ui.add_attachment(abs_path, is_folder=False)

        self._update_attachment_menu()

    def _handle_add_file_request(self):
        """Shows a file dialog and adds selected files as attachments."""
        if len(self.pending_attachments) >= 50:
            self.ui.add_message("<i>You can attach a maximum of 50 files.</i>")
            return

        file_dialog = QFileDialog(self.ui.main_window)
        file_dialog.setFileMode(QFileDialog.ExistingFiles)

        if self.state == ControllerState.TERMINAL:
            file_dialog.setNameFilter("Text & Code Files (*.txt *.py *.js *.html *.css *.json *.xml *.md *.csv *.log);;All Files (*)")
        else:
            file_dialog.setNameFilter("All Files (*);;Images (*.png *.jpg *.jpeg);;Text Files (*.txt)")

        if file_dialog.exec():
            file_paths = file_dialog.selectedFiles()
            self._add_attachments_from_paths(file_paths)

    def _handle_add_folder_request(self):
        """Shows a folder dialog and adds the contents as attachments."""
        if len(self.pending_attachments) >= 50:
            self.ui.add_message("<i>You can attach a maximum of 50 files.</i>")
            return

        folder_path = QFileDialog.getExistingDirectory(self.ui.main_window, "Select Folder")
        if folder_path:
            self._add_attachments_from_paths([folder_path])

    @Slot(str)
    def _handle_path_mention_for_attachment(self, path: str):
        """Handles a file/folder path selected via '@' mention."""
        logging.info(f"Received path '{path}' from mention for attachment.")
        self._add_attachments_from_paths([path])

    def _handle_remove_attachment(self, index):
        if 0 <= index < len(self.pending_attachments):
            self.pending_attachments.pop(index)
            self.ui.remove_thumbnail(index)
            self._update_attachment_menu()

    def _show_settings_window(self):
        if not self.settings_window:
            self.settings_window = SettingsWindow(self.settings_manager, self)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def _handle_pasted_image(self, image_bytes: bytes):
        """Handles a pasted image by adding its byte data as an attachment."""
        if self._get_image_attachment_count() >= 3:
            self.ui.add_message("<i>You can attach a maximum of 3 images.</i>")
            return
        if len(self.pending_attachments) >= 50:
            self.ui.add_message("<i>You can attach a maximum of 50 files.</i>")
            return
        try:
            self.pending_attachments.append({'type': 'image', 'data': image_bytes})
            pil_image = Image.open(io.BytesIO(image_bytes))
            self.ui.add_attachment(pil_image)
            self._update_attachment_menu()
        except Exception as e:
            logging.error(f"Error handling pasted image: {e}")

    def _handle_pasted_files(self, file_paths: list):
        """Handles pasted files by adding them as attachments."""
        self._add_attachments_from_paths(file_paths)

    def _on_toggle_fullscreen_clicked(self):
        if self.state == ControllerState.VISION and self.vision_handler.state in [VisionState.EXECUTING, VisionState.PAUSED]:
            self.vision_handler.pre_pause_state = self.vision_handler.state
            self.vision_handler._set_state(VisionState.PAUSED)
            self.ui.add_message("<i>Execution paused due to mode switch.</i>")
            self.ui.set_paused(True)
            self.ui.set_step_controls_enabled(False)



    def handle_vision_error(self):
        """Handles the 'smart pause' state when the assistant gets stuck."""
        logging.info("Handling vision error: switching to full mode and unlocking UI for user guidance.")

        # Ensure all execution-related UI is turned off
        self.ui.show_thinking_ui(False)
        self.ui.set_active_mode('vision')
        self.ui.set_paused(False) # Ensure pause icon is reset

        # Switch to full mode so the user has room to type
        if self.ui.is_compact:
            self.ui.switchToFullMode()

        # Unlock the input field for the user to provide guidance
        self.ui.user_input.setDisabled(False)
        self.ui.send_button.setDisabled(False)
        self.ui.user_input.setPlaceholderText("Provide guidance or suggest a next step...")

    # --- Hotkey Handlers ---

    def hotkey_toggle_mode(self):
        """Safely toggles between Chat and vision modes."""
        if not self.is_vision_module_initialized:
            logging.warning("Cannot toggle mode: Vision module is not yet initialized.")
            return

        if self.state == ControllerState.VISION:
            # Only allow deactivation if the assistant is in a safe, non-executing state
            safe_states = [VisionState.IDLE, VisionState.PAUSED_ON_ERROR, VisionState.WAITING_FOR_APPROVAL]
            if self.vision_handler.state in safe_states:
                logging.info("Deactivating vision mode via hotkey.")
                self._deactivate_vision_mode()
            else:
                logging.warning(f"Cannot toggle mode while assistant is in state: {self.vision_handler.state.name}")
        else:
            # If not in vision mode, it's always safe to activate it.
            logging.info("Activating vision mode via hotkey.")
            self._activate_vision_mode()

    def hotkey_pause_resume(self):
        if self.state == ControllerState.VISION:
            logging.info("Toggling pause/resume via hotkey.")
            self.vision_handler.pause_or_resume()
        else:
            logging.warning("Cannot pause/resume: Not in vision mode.")

    def hotkey_stop_execution(self):
        if self.state == ControllerState.VISION:
            logging.info("Stopping execution via hotkey.")
            self.vision_handler.stop()
        else:
            logging.warning("Cannot stop execution: Not in vision mode.")

    def hotkey_pause_for_feedback(self):
        if self.state == ControllerState.VISION:
            logging.info("Pausing for user feedback via hotkey.")
            self.vision_handler.pause_for_feedback()
        else:
            logging.warning("Cannot pause for feedback: Not in vision mode.")

    def hotkey_cancel_request(self):
        if self.thinking_timer.isActive():
            logging.info("Canceling request via hotkey.")
            self.handle_cancel_request()
        else:
            logging.warning("Cannot cancel request: No active request.")

    def hotkey_regenerate_plan(self):
        if self.state == ControllerState.VISION and self.vision_handler.state == VisionState.WAITING_FOR_APPROVAL:
            logging.info("Regenerating plan via hotkey.")
            self.vision_handler.regenerate_plan()
        else:
            logging.warning("Cannot regenerate plan: Not awaiting approval.")

    def hotkey_edit_plan(self):
        if self.state == ControllerState.VISION and self.vision_handler.state == VisionState.WAITING_FOR_APPROVAL:
            logging.info("Editing plan via hotkey.")
            self._handle_edit_plan()
        else:
            logging.warning("Cannot edit plan: Not awaiting approval.")

    def hotkey_cancel_plan(self):
        if self.state == ControllerState.VISION and self.vision_handler.state == VisionState.WAITING_FOR_APPROVAL:
            logging.info("Canceling plan via hotkey.")
            self.vision_handler.clear_plan()
        else:
            logging.warning("Cannot cancel plan: Not awaiting approval.")

    def hotkey_run_plan(self):
        if self.state == ControllerState.VISION and self.vision_handler.state == VisionState.WAITING_FOR_APPROVAL:
            logging.info("Running plan via hotkey.")
            self._handle_plan_approval()
        else:
            logging.warning("Cannot run plan: Not awaiting approval.")

    def _handle_new_instance_connection(self):
        """
        Called when another instance of the app tries to start.
        Brings the main window to the front.
        """
        logging.info("New instance connection detected. Activating main window.")
        socket = self.local_server.nextPendingConnection()
        if socket:
            # Clean up the socket to prevent resource leaks
            socket.readyRead.connect(socket.deleteLater)

        self.ui.main_window.showNormal() # Ensure it's not minimized
        self.ui.main_window.raise_()
        self.ui.main_window.activateWindow()
