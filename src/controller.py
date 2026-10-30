import html
import json
import logging
import io
import threading
import time
import os
import uuid
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QGroupBox, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QTextBrowser, QVBoxLayout,
)
from PIL import Image

from brain import Brain
from build_info import get_build_id
from llm.custom_providers import provider_is_configured
from llm.error_formatter import CONTINUE_LINK
from llm.llm_worker import LLMWorker
from llm.mode_capabilities import ModeCapabilities, SUBSECTIONS
from project_manager import event_text
import attachments as attachment_store
from context import context_stats, handoff, model_context, project_rules, skills
from context.tokens import ModelMetadata
from settings_manager import SettingsManager
from ui.manager import UIManager
from ui.text_render import render_text
from ui.theme_manager import ThemeManager
from ui.settings_window import SettingsWindow
from handlers.chat_handler import ChatHandler
from handlers.vision_handler import VisionHandler
from handlers.terminal_handler import TerminalHandler
from handlers.browser_handler import BrowserHandler
from states import ControllerState, VisionState
from hotkey_manager import HotkeyManager
from src.computer.computer import Computer
from indexer import Indexer
# Heavy imports are deferred to background threads

class AppController(QObject):
    llm_request_sent = Signal(str, list, str, list, str, str, int)
    summary_requested = Signal(str, str, str, str)
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

    # How many messages may wait in the chat queue while a reply streams.
    CHAT_QUEUE_MAX = 5
    # Minimum pause between automatic compactions of the same session.
    AUTO_COMPACT_COOLDOWN_SECONDS = 120.0
    # Control tools that are not shown as instruments in the right panel.
    ACTIVITY_HIDDEN_TOOLS = frozenset({
        "use_skill", "finish", "communicate_to_user", "display_message"})

    def __init__(self, app, settings_manager: SettingsManager, local_server=None, working_directory=None,
                 project_manager=None):
        super().__init__()
        init_start = time.perf_counter()
        # Basic references
        self.app = app
        self.settings_manager = settings_manager
        self.theme_manager = ThemeManager(self.settings_manager)
        self.settings = self.settings_manager.all()
        self.project_manager = project_manager
        self.project_root = ""
        if self.project_manager is not None:
            self.project_root = self.project_manager.get_active_root()
        elif working_directory:
            self.project_root = working_directory
        if self.project_root:
            self.project_root = os.path.abspath(self.project_root)
        self.working_directory = self.project_root
        self.session_file = ""
        self.session_id = ""
        self.session_mode = "chat"
        self.session_turn_id = ""
        self.session_objective = ""
        self.session_events = []
        self._session_event_count = 0
        self._persisted_message_count = 0
        self._handoff_summary = ""
        self._handoff_meta = {}
        self._summary_pending = False
        self._summary_force = False
        self._summary_request_context = None
        self._manual_compact_pending = False
        self._compaction_trigger = ""
        self._manual_compact_focus = ""
        self._auto_compact_last_at = 0.0
        self._summary_failures = 0
        self._summary_retry_at = 0.0
        self._context_warning_file = ""
        self._summary_timer = QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.timeout.connect(self._maybe_refresh_summary)
        # A live theme switch re-renders the transcript, but only once the
        # active mode is at rest: replaying mid-stream would orphan the live
        # message the widget is still appending chunks to.
        self._theme_replay_pending = False
        # Auto-compaction is checked only when work has settled; the debounce
        # keeps it from firing in the middle of a burst of journal events.
        self._auto_compact_timer = QTimer(self)
        self._auto_compact_timer.setSingleShot(True)
        self._auto_compact_timer.timeout.connect(self._maybe_auto_compact)
        self._context_indicator_timer = QTimer(self)
        self._context_indicator_timer.setSingleShot(True)
        self._context_indicator_timer.timeout.connect(self._refresh_context_indicator)

        # --- Single Instance Lock ---
        self.local_server = local_server
        if self.local_server:
            self.local_server.newConnection.connect(self._handle_new_instance_connection)
        # --------------------------

        # Core services (lightweight part)
        self.brain = Brain()
        self.vision = None  # Will be initialized in a background thread
        self.is_vision_module_initialized = False
        self.indexer = None
        if self.project_root:
            self.indexer = Indexer(self.project_root)
            # File index lives for Terminal mode only (user choice): auto-rebuilds
            # anywhere else are skipped; entering Terminal forces a fresh build.
            self.indexer.is_active_cb = lambda: self.state == ControllerState.TERMINAL
            self.indexer.index_updated.connect(self._on_index_updated)
        self.last_search_query = None
        # Created lazily when Terminal mode starts; owned by the controller so
        # it survives mode switches and is stopped once on shutdown.
        self._mcp_manager = None

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
        # Messages typed while a chat reply is streaming wait here and are
        # answered in order (FIFO) once the current reply finishes.
        self.chat_queue = []
        self.thinking_timer = QTimer(self)
        self.thinking_time = 0
        self.settings_window = None
        self._shutting_down = False
        self._load_project_session()
        self._load_handoff_state()

        # Handlers
        comp_start = time.perf_counter()
        self.computer = Computer(self.settings_manager, mode="chat",
                                 working_directory=self.working_directory)
        self._last_llm_request = None  # (user_prompt, attachments, system_prompt, history, schema) for Retry
        self._last_llm_error = False  # True if the last request ended with an error card
        self.vision_handler = VisionHandler(self)
        self.terminal_handler = TerminalHandler(self, self.theme_manager, working_directory=self.working_directory)
        self.browser_handler = BrowserHandler(self, working_directory=self.working_directory)
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
        self.llm_worker.structured_response_received.connect(self._handle_structured_response)
        self.llm_worker.plain_text_response_received.connect(self._handle_plain_text_response)
        self.llm_worker.retry_attempt.connect(self._handle_retry_attempt)
        self.llm_worker.retry_countdown.connect(self._handle_retry_countdown)
        self.llm_worker.error_occurred.connect(self.handle_llm_error)
        self.llm_worker.summary_finished.connect(self._handle_summary_response)
        self.llm_worker.summary_failed.connect(self._handle_summary_error)
        self.llm_worker.usage_received.connect(self._handle_usage)
        self.llm_request_sent.connect(self.llm_worker.execute_request)
        self.summary_requested.connect(self.llm_worker.execute_summary_request)

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
        self.ui.screenshot_button_clicked.connect(self._handle_screenshot_request)
        self.ui.add_file_clicked.connect(self._handle_add_file_request)
        self.ui.add_folder_clicked.connect(self._handle_add_folder_request)
        self.ui.settings_button_clicked.connect(self._show_settings_window)
        self.ui.attachment_removed.connect(self._handle_remove_attachment)
        self.ui.chat_mode_selected.connect(self._activate_chat_mode)
        self.ui.terminal_mode_selected.connect(self._activate_terminal_mode)
        self.ui.vision_mode_selected.connect(self._activate_vision_mode)
        self.ui.browser_mode_selected.connect(self._activate_browser_mode)
        self.ui.retry_requested.connect(self._handle_retry_request)
        self.ui.continue_requested.connect(self._handle_continue_request)
        self.ui.job_stop_requested.connect(self._handle_job_stop)
        self.ui.model_selected.connect(self.switch_model)
        self.ui.reasoning_effort_changed.connect(self.set_reasoning_effort)
        self.ui.cancel_requested.connect(self.handle_cancel_request)
        self.ui.pause_resume_clicked.connect(self._handle_pause_resume)
        self.ui.pause_for_feedback_clicked.connect(self._handle_pause_for_feedback)
        self.ui.stop_clicked.connect(self._handle_stop)
        self.ui.toggle_fullscreen_button_clicked.connect(self._on_toggle_fullscreen_clicked)
        self.ui.file_search_requested.connect(self.search_files)
        self.ui.image_pasted.connect(self._handle_pasted_image)
        self.ui.files_pasted.connect(self._handle_pasted_files)
        self.ui.files_dropped.connect(self._handle_pasted_files)
        self.ui.path_selected_for_attachment.connect(self._handle_path_mention_for_attachment)
        self.ui.project_switch_requested.connect(self.switch_project)
        self.ui.project_open_requested.connect(self._handle_open_project)
        self.ui.session_open_requested.connect(self.open_session)
        self.ui.new_session_requested.connect(self.new_session)
        self.ui.project_renamed.connect(self.rename_project)
        self.ui.project_removed.connect(self.remove_project)
        self.ui.session_renamed.connect(self.rename_session)
        self.ui.session_deleted.connect(self.delete_session)
        self.ui.context_requested.connect(self._show_context_summary)
        self.ui.journal_requested.connect(self._show_session_journal)
        self.ui.home_requested.connect(self.ui.show_home)

        # Thinking timer and settings change hook
        self.thinking_timer.timeout.connect(self._update_thinking_timer)
        self.settings_manager.settings_changed.connect(self._on_settings_changed)

        # Connect hotkey signals to slots
        self.hotkey_toggle_mode_triggered.connect(self.hotkey_toggle_mode)
        self.hotkey_pause_resume_triggered.connect(self.hotkey_pause_resume)
        self.hotkey_stop_execution_triggered.connect(self.hotkey_stop_execution)
        self.hotkey_pause_for_feedback_triggered.connect(self.hotkey_pause_for_feedback)
        self.hotkey_cancel_request_triggered.connect(self.hotkey_cancel_request)




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
        self.llm_request_sent.emit(user_prompt, [], system_prompt, [], schema_name, "sync", 0)

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
        """Fast, bounded shutdown: hide at once, stop everything with budgets, quit.

        The old flow (daemon cleanup thread + sys.exit) was theater: sys.exit
        still waits for non-daemon threads, so a worker sleeping in a retry
        backoff or a blocked HTTP call hung the app for up to minutes.
        """
        if self._shutting_down:
            logging.info("Shutdown already in progress; ignoring duplicate request.")
            return
        self._shutting_down = True
        try:
            self.ui.main_window.hide()
        except Exception:
            pass
        logging.info("Shutdown requested. Cancelling work, then bounded cleanup.")
        try:
            if self.llm_worker:
                self.llm_worker.cancel()
        except Exception as e:
            logging.warning(f"LLM cancel failed during shutdown: {e}")
        self.cleanup()
        try:
            self.app.quit()
        except Exception as e:
            logging.warning(f"App quit failed during shutdown: {e}")

    def _stop_thread(self, thread, name: str, timeout_ms: int):
        try:
            if thread is None:
                return
            try:
                thread.quit()
            except Exception:
                pass
            try:
                running = thread.isRunning()
            except Exception:
                running = True
            if running and not thread.wait(timeout_ms):
                logging.warning(f"{name} thread did not finish in {timeout_ms}ms; continuing shutdown.")
        except Exception as e:
            logging.warning(f"Stopping {name} thread failed: {e}")

    def cleanup(self):
        """Stops background processes with per-step guards and time budgets."""
        logging.info("AppController cleanup initiated.")
        self._persist_session()
        try:
            computer = getattr(getattr(self, "terminal_handler", None),
                               "computer", None)
            manager = getattr(computer, "shell_jobs", None)
            if manager is not None:
                manager.kill_all()
        except Exception as e:
            logging.warning(f"Shell job shutdown failed: {e}")
        try:
            vision = getattr(self, "vision_handler", None)
            if vision is not None:
                vision.stop()
        except Exception as e:
            logging.warning(f"Vision shutdown failed: {e}")
        self._stop_thread(getattr(self, "llm_thread", None), "llm", 2000)
        self._stop_thread(getattr(self, "vision_thread", None), "vision", 2500)
        self._stop_thread(getattr(self, "screenshot_thread", None), "screenshot", 1000)
        try:
            web = getattr(getattr(self, "terminal_handler", None), "web_thread", None)
            if web is not None:
                try:
                    web.cancel()
                except Exception:
                    pass
                if web.isRunning() and not web.wait(1000):
                    logging.warning("Terminal web thread did not finish in 1000ms; continuing shutdown.")
        except Exception as e:
            logging.warning(f"Terminal web thread stop failed: {e}")
        try:
            exec_thread = getattr(getattr(self, "terminal_handler", None), "_exec_thread", None)
            if exec_thread is not None:
                try:
                    exec_thread.quit()
                except Exception:
                    pass
                if exec_thread.isRunning() and not exec_thread.wait(2000):
                    logging.warning("Terminal exec thread did not finish in 2000ms; continuing shutdown.")
        except Exception as e:
            logging.warning(f"Terminal exec thread stop failed: {e}")
        try:
            manager = getattr(self, "_mcp_manager", None)
            if manager is not None:
                logging.info("Stopping MCP manager.")
                manager.stop()
                self._mcp_manager = None
        except Exception as e:
            logging.warning(f"MCP shutdown failed: {e}")
        try:
            handler = getattr(self, "browser_handler", None)
            shutdown = getattr(handler, "shutdown", None)
            if callable(shutdown):
                shutdown()
        except Exception as e:
            logging.warning(f"Browser shutdown failed during shutdown: {e}")
        try:
            if self.indexer:
                logging.info("Stopping file indexer.")
                self.indexer.stop()
        except Exception as e:
            logging.warning(f"Indexer stop failed: {e}")
        try:
            if getattr(self, "hotkey_manager", None):
                logging.info("Stopping hotkey manager.")
                self.hotkey_manager.stop()
        except Exception as e:
            logging.warning(f"Hotkey stop failed: {e}")

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

    def run(self):
        has_project = bool(self.project_root)
        if has_project:
            self._restore_session_mode(self.session_mode)
            self.ui.set_project_name(self.project_manager.get_active_name(), self.project_root)
            self.ui.show_workspace()
        else:
            self.ui.show_home()
        self.ui.show_main_window()
        title_string = self._get_llm_title_string()
        window_title = self._get_llm_window_title()
        self.ui.set_main_window_title(window_title)
        self.ui.set_title_bar_text(window_title)
        self.ui.refresh_model_button(title_string)
        self.ui.set_assistant_name(self._get_current_model_name())
        if has_project:
            self._restore_agent_history()
            self._replay_session()
            self._schedule_summary_refresh()
        self._start_vision_initialization()
        if self.indexer is not None:
            self.indexer.start()
        self._check_initial_configuration()
        # Transcript HTML carries theme colors baked at render time, so a live
        # theme switch must rebuild it. Connected after the startup replay so
        # a restored session is not rendered twice.
        self.theme_manager.theme_changed.connect(self._on_theme_changed)

    def _check_initial_configuration(self):
        """Checks if the active provider has a configured API key (from keyring) and shows a message if not."""
        try:
            active_provider_name = self.settings.get("active_provider")

            if not provider_is_configured(self.settings, active_provider_name):
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
        if self.indexer is None:
            self.files_found_signal.emit([])
            return
        results = self.indexer.search(query)
        self.files_found_signal.emit(results)

    # ------------------------------------------------------------- projects
    @staticmethod
    def _valid_mode(mode: str) -> str:
        return mode if mode in ("chat", "terminal", "browser", "vision") else "chat"

    def _current_mode(self) -> str:
        return self._valid_mode(getattr(self.ui, "_active_mode_key", "chat"))

    def _restore_session_mode(self, mode: str):
        mode = self._valid_mode(mode)
        self.session_mode = mode
        handlers = getattr(self, "chat_handler", None)
        if handlers is None:
            return
        handler, state = {
            "chat": (self.chat_handler, ControllerState.IDLE),
            "terminal": (self.terminal_handler, ControllerState.TERMINAL),
            "browser": (self.browser_handler, ControllerState.BROWSER),
            "vision": (self.vision_handler, ControllerState.VISION),
        }[mode]
        self.current_handler = handler
        self._set_state(state)
        if mode == "terminal":
            self.ensure_mcp_manager()
        try:
            self.ui.set_active_mode(mode)
        except Exception as e:
            logging.warning(f"Could not restore UI mode {mode}: {e}")

    def _session_has_content(self) -> bool:
        return bool(self._session_event_count or self.conversation_history)

    def _create_new_session(self, mode: str = ""):
        mode = self._valid_mode(mode or self._current_mode())
        self.session_file = self.project_manager.new_session_file(mode=mode)
        self.session_id = self.project_manager.session_id(self.session_file)
        self.session_objective = ""
        self.session_mode = mode
        self.session_turn_id = ""
        self.session_events = []
        self._session_event_count = 0
        self._persisted_message_count = 0
        self._handoff_summary = ""
        self._handoff_meta = {}
        self._summary_force = False
        self._manual_compact_pending = False
        self._compaction_trigger = ""
        self._manual_compact_focus = ""
        self._schedule_context_indicator_update()
        return self.session_file

    def _ensure_session_objective(self, user_text: str):
        """Derives the session goal from the first request unless one is set."""
        if (self.session_objective or not self.session_file
                or self.project_manager is None):
            return
        text = " ".join(str(user_text or "").split())
        if not text:
            return
        objective = text[:200] + ("..." if len(text) > 200 else "")
        try:
            if self.project_manager.set_session_objective(self.session_file, objective):
                self.session_objective = objective
                self.ui.refresh_sidebar()
        except Exception as e:
            logging.warning(f"Could not store session objective: {e}")

    def _prepare_session_for_mode(self, mode: str):
        """Keep one mode per session; a non-empty mode change starts a new one."""
        mode = self._valid_mode(mode)
        if self.project_manager is None or not self.project_root:
            self.session_mode = mode
            return
        if mode == self.session_mode:
            return
        previous_file = self.session_file
        previous_id = self.session_id
        context_settings = self._summary_settings()
        handoff_on_switch = bool(
            context_settings.get("handoff_enabled", True)
            and context_settings.get("handoff_on_mode_switch", True))
        if self._session_has_content() and handoff_on_switch:
            self._capture_handoff_summary()
        try:
            if self.session_file and not os.path.exists(self.session_file):
                self._create_new_session(mode)
                return
            if not self._session_has_content():
                if not self.session_file:
                    self.session_mode = mode
                    return
                if self.project_manager.set_session_mode(self.session_file, mode):
                    self.session_mode = mode
                    return
                self._create_new_session(mode)
                return
            self._persist_session()
        except Exception as e:
            logging.warning(f"Could not prepare session for {mode} mode: {e}")
        self.conversation_history.clear()
        self.pending_attachments.clear()
        try:
            self.ui.clear_thumbnails()
            self._update_attachment_menu()
        except Exception as e:
            logging.warning(f"Could not clear attachments on mode change: {e}")
        try:
            self._create_new_session(mode)
            if handoff_on_switch:
                self._seed_session_summary(previous_file, previous_id)
        except Exception as e:
            logging.warning(f"Could not create a new {mode} session: {e}")
            self.session_file = ""
        self._reset_agent_histories()
        try:
            self.ui.clear_chat()
        except Exception:
            pass

    def _load_project_session(self):
        """Restores the most recent session stored in the project."""
        self.session_file = ""
        self.session_id = ""
        self.session_objective = ""
        self.session_events = []
        self._session_event_count = 0
        self._persisted_message_count = 0
        if self.project_manager is None or not self.project_manager.get_active_root():
            return
        try:
            latest = self.project_manager.latest_session_file()
            if latest:
                self._load_session_file(latest)
            else:
                self._create_new_session(self._current_mode())
        except Exception as e:
            logging.warning(f"Could not restore project session: {e}")

    def _load_session_file(self, path: str):
        """Loads a specific session file into the shared conversation list."""
        if self.project_manager is None or not path:
            return
        data = self.project_manager.load_session_data(path)
        if not path.endswith(".jsonl"):
            migrated = self.project_manager.migrate_session(path, data.get("mode") or "chat")
            if migrated != path:
                path = migrated
                data = self.project_manager.load_session_data(path)
        messages = data.get("messages") or []
        events = data.get("events") or []
        self.session_file = path
        self.session_id = data.get("id") or self.project_manager.session_id(path)
        self.session_objective = data.get("objective") or ""
        self.session_mode = self._valid_mode(data.get("mode") or "chat")
        self.session_turn_id = ""
        self.session_events = list(events)
        self._session_event_count = len(events)
        self._persisted_message_count = sum(
            1 for event in events if event.get("type") == "message")
        self._summary_force = False
        self._manual_compact_pending = False
        self._compaction_trigger = ""
        self._manual_compact_focus = ""
        self.conversation_history.clear()
        self.conversation_history.extend(messages)
        if getattr(self, "chat_handler", None) is not None:
            self._restore_session_mode(self.session_mode)
        self._load_handoff_state()
        self._restore_agent_history()
        self._schedule_context_indicator_update()
        logging.info(f"Loaded session with {len(messages)} messages from {path}")

    def _session_snapshot(self) -> list:
        """Serializable subset of the conversation (plain role/content turns)."""
        snapshot = []
        for item in getattr(self, "conversation_history", []) or []:
            if (isinstance(item, dict)
                    and item.get("role") in ("user", "assistant", "system")
                    and isinstance(item.get("content"), str)):
                snapshot.append({"role": item["role"], "content": item["content"]})
        return snapshot

    def append_session_event(self, event_type: str, **fields):
        """Append one typed event to the active session's JSONL log."""
        if self.project_manager is None or not self.project_manager.get_active_root():
            return None
        try:
            if not self.session_file or not os.path.exists(self.session_file):
                self._create_new_session(self.session_mode)
            if not self.session_file.endswith(".jsonl"):
                self.session_file = self.project_manager.migrate_session(
                    self.session_file, self.session_mode)
            event = self.project_manager.make_event(event_type, **fields)
            stored = self.project_manager.append_event(self.session_file, event)
            if stored is None:
                return None
            self.session_events.append(stored)
            self._session_event_count += 1
            self._schedule_context_indicator_update()
            self._schedule_auto_compact_check()
            return stored
        except Exception as e:
            logging.warning(f"Could not append session event '{event_type}': {e}")
            return None

    def _persist_session(self, attachments: list = None):
        if self.project_manager is None or not self.project_manager.get_active_root():
            return
        pending_attachments = list(attachments or [])
        try:
            snapshot = self._session_snapshot()
            if not snapshot:
                return
            if not self.session_file or not os.path.exists(self.session_file):
                self._create_new_session(self.session_mode)
            if not self.session_file.endswith(".jsonl"):
                self.session_file = self.project_manager.migrate_session(
                    self.session_file, self.session_mode)
            if len(snapshot) < self._persisted_message_count:
                self._persisted_message_count = 0
            if not self.session_turn_id:
                self.session_turn_id = uuid.uuid4().hex
            while self._persisted_message_count < len(snapshot):
                item = snapshot[self._persisted_message_count]
                extra = {}
                if pending_attachments:
                    extra["attachments"] = pending_attachments
                    pending_attachments = []
                event = self.append_session_event(
                    "message",
                    turn_id=self.session_turn_id,
                    role=item["role"],
                    content=[{"type": "text", "text": item["content"]}],
                    **extra,
                )
                if event is None:
                    break
                self._persisted_message_count += 1
        except Exception as e:
            logging.warning(f"Could not persist project session: {e}")

    def _handler_for_mode(self, mode: str):
        mode = self._valid_mode(mode)
        return getattr(self, {
            "chat": "chat_handler",
            "terminal": "terminal_handler",
            "browser": "browser_handler",
            "vision": "vision_handler",
        }[mode], None)

    def _reset_agent_history(self, handler):
        if handler is None or not hasattr(handler, "command_history"):
            return
        cancel_wait = getattr(handler, "cancel_job_wait", None)
        if callable(cancel_wait):
            try:
                cancel_wait()
            except Exception:
                pass
        for attr, value in (
                ("original_user_request", None),
                ("command_history", []),
                ("_stashed_response", None),
                ("_tool_in_flight", False),
                ("_last_action_sig", None),
                ("_consecutive_errors", 0),
                ("_assessment_sent", False),
                ("_active_call_id", None),
                ("_active_tool_name", ""),
                ("_active_turn_id", ""),
                ("_awaiting_job_id", None),
                ("_job_wait_rounds", 0),
                ("_pending_action_name", None),
                ("_pending_action_args", None)):
            if hasattr(handler, attr):
                setattr(handler, attr, value)

    def _reset_agent_histories(self):
        for name in ("terminal_handler", "browser_handler", "vision_handler"):
            self._reset_agent_history(getattr(self, name, None))

    def _load_handoff_state(self, create_fallback: bool = True) -> str:
        """Load this session's summary and create a fallback when missing."""
        self._handoff_summary = ""
        self._handoff_meta = {}
        if not self.session_file:
            return ""
        self._handoff_meta = handoff.read_meta(self.session_file)
        summary = handoff.read_summary(self.session_file)
        if not summary and create_fallback and self.session_events:
            summary = handoff.fallback_summary(self.session_events)
            if summary:
                summary = handoff.write_summary(
                    self.session_file,
                    summary,
                    source_session=self.session_file,
                    event_count=len(self.session_events),
                    source="fallback",
                )
                self._handoff_meta = handoff.read_meta(self.session_file)
        self._handoff_summary = summary or ""
        return self._handoff_summary

    def _seed_session_summary(self, source_file: str, source_id: str = "") -> bool:
        """Copy the previous session's summary once into a new mode session."""
        if (not source_file or not self.session_file
                or os.path.normcase(os.path.abspath(source_file))
                == os.path.normcase(os.path.abspath(self.session_file))):
            return False
        summary = handoff.read_summary(source_file)
        if not summary:
            return False
        written = handoff.write_summary(
            self.session_file,
            summary,
            source_session=source_file,
            event_count=0,
            source="handoff",
            handoff_from=source_id or os.path.basename(source_file),
        )
        if not written:
            return False
        self._handoff_summary = written
        self._handoff_meta = handoff.read_meta(self.session_file)
        return True

    def project_baseline_prompt(self) -> str:
        """Stable AGENTS.md rules for the active project (empty when absent)."""
        if not self.project_root:
            return ""
        return project_rules.injection_block(self.project_root)

    def skills_prompt(self) -> str:
        """Name+description list of installed Agent Skills for the active project."""
        return skills.prompt_block(self.project_root)

    def consume_handoff_seed(self):
        """Mark a mode-switch seed as sent so later requests do not repeat it."""
        if not self.session_file or self._handoff_meta.get("source") != "handoff":
            return
        meta = handoff.mark_handoff_consumed(self.session_file)
        if meta:
            self._handoff_meta = meta

    def chat_context_prompt(self) -> str:
        """System context for Chat: rules, handoff seed, compacted summary."""
        blocks = [
            self.project_baseline_prompt(),
            self.handoff_prompt(),
            self.compaction_block("chat"),
        ]
        return "\n\n".join(block for block in blocks if block)

    def chat_model_history(self) -> list:
        """Model-facing copy of Chat history; the journal keeps the full list."""
        context_cfg = self._summary_settings()
        history = model_context.compact_chat_history(
            self.conversation_history, context_cfg)
        covered = self._compacted_message_count()
        return history[covered:] if covered else history

    def _compaction_meta(self) -> dict:
        if self._handoff_meta:
            return self._handoff_meta
        if not self.session_file:
            return {}
        return handoff.read_meta(self.session_file)

    def _compacted_event_count(self) -> int:
        if not self.session_events:
            return 0
        return handoff.compacted_event_count(
            self._compaction_meta(), self.session_events)

    def _compacted_message_count(self) -> int:
        """Journal messages covered by the compaction boundary."""
        covered_events = self._compacted_event_count()
        if not covered_events:
            return 0
        return sum(
            1 for event in self.session_events[:covered_events]
            if event.get("type") == "message")

    def compaction_block(self, mode: str = "") -> str:
        """Prompt block standing in for the compacted part of the history."""
        covered = self._compacted_event_count()
        if not covered or not self.session_file:
            return ""
        summary = handoff.read_summary(self.session_file) or self._handoff_summary
        if not summary:
            return ""
        return handoff.compaction_block(
            summary, mode=mode or self.session_mode, covered_events=covered)

    def _history_cut_key(self) -> int:
        """Non-protected entries currently in the agent history.

        Stable against protected blocks being rebuilt at the front, unlike a
        raw list index. Chat has no marker history, so it reports 0.
        """
        if self.session_mode == "chat":
            return 0
        handler = self._handler_for_mode(self.session_mode)
        history = getattr(handler, "command_history", None) if handler else None
        if not isinstance(history, list):
            return 0
        return sum(
            1 for entry in history
            if not str(entry).startswith(model_context.COMPACTION_MARKER)
            and not model_context.is_protected_entry(str(entry)))

    def _apply_compaction_marker(self, history_cut: int):
        """Place the boundary marker after the compacted part of the history."""
        if self.session_mode == "chat":
            return
        handler = self._handler_for_mode(self.session_mode)
        history = getattr(handler, "command_history", None) if handler else None
        if not isinstance(history, list):
            return
        kept = [
            entry for entry in history
            if not str(entry).startswith(model_context.COMPACTION_MARKER)
        ]
        target = max(0, int(history_cut or 0))
        position = 0
        while (position < len(kept)
               and model_context.is_protected_entry(str(kept[position]))):
            position += 1
        seen = 0
        while position < len(kept) and seen < target:
            if not model_context.is_protected_entry(str(kept[position])):
                seen += 1
            position += 1
        kept.insert(position, model_context.COMPACTION_MARKER)
        handler.command_history = kept

    def _apply_compaction(self, covered: int,
                          history_cut: int | None = None) -> bool:
        """Persist the boundary and cut the live history at it."""
        covered = int(covered or 0)
        if not self.session_file or covered <= 0:
            return False
        event_id = str(self.session_events[covered - 1].get("id") or "")
        if not event_id:
            return False
        meta = handoff.set_compaction_boundary(
            self.session_file, event_id, covered)
        if not meta:
            return False
        trigger = self._compaction_trigger or "manual"
        self._clear_pending_compaction(cooldown=True)
        self._handoff_meta = meta
        self._apply_compaction_marker(
            self._history_cut_key() if history_cut is None else history_cut)
        self._refresh_agent_context()
        self._add_compaction_message(trigger, covered, len(self.session_events))
        self._schedule_context_indicator_update()
        return True

    def _maybe_finish_pending_compaction(self, from_timer: bool = False):
        if not self._manual_compact_pending:
            return
        if not self.session_file or not self.session_events:
            self._clear_pending_compaction()
            return
        busy = self._compaction_busy_reason()
        if busy:
            self._summary_timer.start(2000)
            if not from_timer:
                self.ui.add_message(
                    "<i>Compaction is waiting for the current step to finish…</i>")
            return
        trigger = self._compaction_trigger or "manual"
        self._request_summary_update(
            force=True, advance_boundary=True, trigger=trigger,
            focus=self._manual_compact_focus)

    def _clear_pending_compaction(self, *, cooldown: bool = False):
        self._manual_compact_pending = False
        self._compaction_trigger = ""
        self._manual_compact_focus = ""
        if cooldown:
            self._auto_compact_last_at = time.monotonic()

    @staticmethod
    def _compaction_failure_message(trigger: str, detail: str) -> str:
        label = ("Automatic compaction failed" if trigger == "auto"
                 else "Compaction failed")
        return f"<i>{label}: {detail}</i>"

    def _add_compaction_message(self, trigger: str, covered: int, total: int):
        if trigger == "auto":
            self.ui.add_message(
                f"<i>Context was running low, so older events were compacted "
                f"automatically: {covered:,} of {total:,} events are now "
                "represented by the summary.</i>")
        else:
            self.ui.add_message(
                f"<i>Session context compacted: {covered:,} of {total:,} "
                "events are now represented by the summary.</i>")

    def _compaction_busy_reason(self) -> str:
        """Why live compaction must wait, or an empty string."""
        if self.session_mode == "chat":
            if self.thinking_timer.isActive():
                return ("Wait for the current reply to finish before "
                        "compacting the session context.")
            return ""
        handler = self._handler_for_mode(self.session_mode)
        if handler is None:
            return ""
        if getattr(handler, "_tool_in_flight", False):
            return ("Wait for the running tool call to finish before "
                    "compacting the session context.")
        if getattr(handler, "_awaiting_job_id", None) is not None:
            return ("Wait for the background job to report before "
                    "compacting the session context.")
        return ""

    def _is_agent_idle(self) -> bool:
        """Whether the active mode has no reply, tool or job in progress."""
        if self.session_mode == "chat":
            return bool(
                self.state == ControllerState.IDLE
                and not self.thinking_timer.isActive()
                and not self.chat_queue)
        handler = self._handler_for_mode(self.session_mode)
        if handler is None:
            return False
        return str(getattr(handler.state, "name", "")) == "IDLE"

    def _schedule_auto_compact_check(self):
        timer = getattr(self, "_auto_compact_timer", None)
        if timer is not None and not timer.isActive():
            timer.start(1500)

    def _maybe_auto_compact(self):
        context_settings = self._summary_settings()
        if not context_settings.get("auto_compact", True):
            return
        if not context_settings.get("handoff_enabled", True):
            return
        if self.session_mode not in ("chat", "terminal", "browser"):
            return
        if not self.project_manager or not self.project_root or not self.session_file:
            return
        if (not self.session_events or self._summary_pending
                or self._manual_compact_pending):
            return
        if time.monotonic() < self._summary_retry_at:
            return
        if (time.monotonic() - self._auto_compact_last_at
                < self.AUTO_COMPACT_COOLDOWN_SECONDS):
            return
        if not self._is_agent_idle() or self._compaction_busy_reason():
            return
        covered = self._compacted_event_count()
        if len(self.session_events) - covered < handoff.MIN_EVENTS_FOR_SUMMARY:
            return
        stats = self._context_stats_snapshot()
        if not context_stats.needs_auto_compaction(stats):
            return
        logging.info(
            "Auto-compacting session context: %s of %s tokens used.",
            int(stats.get("context_tokens") or 0),
            int(stats.get("window") or 0))
        self._auto_compact_last_at = time.monotonic()
        self._manual_compact_pending = True
        self._compaction_trigger = "auto"
        self._manual_compact_focus = ""
        self.ui.add_message(
            "<i>Context window is almost full: compacting older events "
            "into the session summary…</i>")
        self._request_summary_update(
            force=True, advance_boundary=True, trigger="auto")

    def _schedule_context_indicator_update(self):
        timer = getattr(self, "_context_indicator_timer", None)
        if timer is not None:
            timer.start(300)
        else:
            self._refresh_context_indicator()

    def _refresh_context_indicator(self):
        setter = getattr(getattr(self, "ui", None), "set_context_indicator", None)
        stats = None
        if self.session_events:
            provider_id, model_id = self._active_model_ids()
            if provider_id and model_id:
                try:
                    stats = self._context_stats_snapshot()
                except Exception as e:
                    logging.debug(
                        "Could not refresh the context indicator: %s", e, exc_info=True)
                    return
        if callable(setter):
            setter(stats)
        self._update_session_panel(stats or {})

    def _update_session_panel(self, stats: dict):
        setter = getattr(getattr(self, "ui", None), "set_session_panel", None)
        if not callable(setter):
            return
        try:
            setter(self.session_panel_payload(stats))
        except Exception as e:
            logging.debug(
                "Could not refresh the session panel: %s", e, exc_info=True)

    def session_panel_payload(self, stats: dict | None = None) -> dict:
        """Values for the right panel's Session and Activity tabs."""
        stats = stats or {}
        provider_id, model_id = self._active_model_ids()
        provider_label = ""
        if provider_id:
            provider_info = (
                getattr(self.settings_manager, "PROVIDER_CONFIG", {}) or {}
            ).get(provider_id) or {}
            provider_label = (
                provider_info.get("display_name")
                or provider_id.replace("_", " ").replace("-", " ").title())
        header = {}
        if self.project_manager is not None and self.session_file:
            header = self.project_manager.read_session_header(self.session_file)
        counts = stats.get("counts") or {}
        title = str(header.get("title") or "").strip()
        objective = str(
            header.get("objective") or self.session_objective or "").strip()
        attachments = self._session_attachment_records()
        return {
            "title": title or objective,
            "provider": provider_label,
            "model": model_id,
            "window": int(stats.get("window") or 0),
            "total_tokens": int(stats.get("actual_tokens") or 0),
            "percent": float(stats.get("percent") or 0.0),
            "input_tokens": int(stats.get("latest_input_tokens") or 0),
            "output_tokens": int(stats.get("latest_output") or 0),
            "reasoning_tokens": int(stats.get("latest_reasoning") or 0),
            "cache_read": int(stats.get("latest_cache_read") or 0),
            "cache_write": int(stats.get("latest_cache_write") or 0),
            "messages": int(counts.get("messages") or 0),
            "user_messages": int(counts.get("user_messages") or 0),
            "assistant_messages": int(counts.get("assistant_messages") or 0),
            "tool_calls": int(counts.get("tool_calls") or 0),
            "created_at": str(
                header.get("created_at") or stats.get("first_ts") or ""),
            "last_activity": str(stats.get("last_ts") or ""),
            "summary": self._session_outcome_summary(header),
            "skills": self._session_skill_records(),
            "tools": self._session_tool_records(),
            "attachments": attachments,
        }

    def _session_outcome_summary(self, header: dict) -> dict:
        """User-facing recap of this session, derived from its own journal."""
        events = [event for event in getattr(self, "session_events", []) or []
                  if isinstance(event, dict)]
        objective = str(header.get("objective")
                        or getattr(self, "session_objective", "") or "").strip()
        done = []
        seen_skills = set()
        for event in events:
            event_type = event.get("type")
            if event_type == "message" and event.get("role") == "assistant":
                text = " ".join(event_text(event).split())
                if not text:
                    continue
                if len(text) > 300:
                    text = text[:299].rstrip() + "…"
                done.append(f"- {text}")
            elif event_type == "tool_call" and event.get("name") == "use_skill":
                args = event.get("input")
                name = str(args.get("name") or "").strip() if isinstance(
                    args, dict) else ""
                if name and name.casefold() not in seen_skills:
                    seen_skills.add(name.casefold())
                    done.append(f"- Used the {name} skill.")
        updated_at = ""
        for event in reversed(events):
            stamp = str(event.get("ts") or "").strip()
            if stamp:
                updated_at = stamp
                break
        return {
            "objective": objective,
            "done": "\n".join(done[-6:]),
            "current": "",
            "next": "",
            "updated_at": updated_at,
            "source": "journal",
        }

    def _session_skill_records(self) -> list:
        """Skills loaded through use_skill, in first-use order."""
        records = []
        positions = {}
        for event in getattr(self, "session_events", []) or []:
            if not isinstance(event, dict) or event.get("type") != "tool_call":
                continue
            if event.get("name") != "use_skill":
                continue
            args = event.get("input")
            name = str(args.get("name") or "").strip() if isinstance(
                args, dict) else ""
            if not name:
                continue
            key = name.casefold()
            if key in positions:
                records[positions[key]]["count"] += 1
                continue
            positions[key] = len(records)
            records.append({"name": name, "count": 1})
        return records

    def _session_tool_records(self) -> list:
        """Named tools invoked in this session, in first-use order."""
        records = []
        positions = {}
        for event in getattr(self, "session_events", []) or []:
            if not isinstance(event, dict) or event.get("type") != "tool_call":
                continue
            name = str(event.get("name") or "").strip()
            if not name or name in self.ACTIVITY_HIDDEN_TOOLS:
                continue
            key = name.casefold()
            if key in positions:
                records[positions[key]]["count"] += 1
                continue
            positions[key] = len(records)
            records.append({"name": name, "count": 1})
        return records

    def _session_attachment_records(self) -> list:
        """Files and screenshots the user attached in this session."""
        records = []
        seen = set()
        root = getattr(self, "project_root", "") or ""
        for event in getattr(self, "session_events", []) or []:
            if not isinstance(event, dict) or event.get("type") != "message":
                continue
            for record in event.get("attachments") or []:
                if not isinstance(record, dict):
                    continue
                path = attachment_store.resolve_path(record, root)
                key = (str(record.get("sha256") or ""),
                       path or str(record.get("name") or ""))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = int(record.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                kind = str(record.get("kind") or "file")
                status = str(record.get("status") or "ok")
                if status == "ok" and path:
                    if not os.path.exists(path) or (
                            kind == "folder" and not os.path.isdir(path)):
                        status = "missing"
                    elif kind != "folder":
                        try:
                            if size and os.path.getsize(path) != size:
                                status = "changed"
                        except OSError:
                            status = "missing"
                records.append({
                    "name": str(record.get("name") or (
                        os.path.basename(path) if path else "attachment")),
                    "kind": kind,
                    "path": path,
                    "size": size,
                    "status": status,
                })
        return records

    def handoff_prompt(self, include_same_session: bool = False) -> str:
        """Summary block for a mode that did not produce the current journal.

        A fresh Computer Use chain cannot replay local events, so Vision asks
        for the summary even when it was produced by the same session.
        """
        if self._compacted_event_count():
            # The compaction block already carries the same summary.
            return ""
        if not self._handoff_summary:
            self._load_handoff_state(create_fallback=False)
        if not self._handoff_summary:
            return ""
        if self._handoff_meta.get("handoff_consumed"):
            return ""
        if (not include_same_session and self.session_events
                and not handoff.should_inject(self._handoff_meta, self.session_file)):
            return ""
        return handoff.injection_block(self._handoff_summary, self.session_mode)

    def session_context_prompt(self) -> str:
        """Objective and journal-derived state of the session being resumed.

        Only Browser and Vision need it: their live page/desktop cannot be
        replayed, while Chat and Terminal rebuild their history from events.
        """
        if self.session_mode not in ("browser", "vision"):
            return ""
        if not self.session_events and not self.session_objective:
            return ""
        summary = handoff.session_summary(
            self.session_events, objective=self.session_objective)
        return handoff.session_injection_block(summary, self.session_mode)

    def _capture_handoff_summary(self) -> str:
        """Freeze this session's own summary before it is left behind."""
        if not self.session_events or not self.session_file:
            return self._handoff_summary
        meta = handoff.read_meta(self.session_file)
        summary = handoff.read_summary(self.session_file) or self._handoff_summary
        same_session = bool(
            meta.get("source_session")
            and os.path.normcase(os.path.abspath(str(meta["source_session"])))
            == os.path.normcase(os.path.abspath(self.session_file)))
        if (same_session and meta.get("source") == "llm"
                and int(meta.get("event_count") or 0) >= len(self.session_events)):
            self._handoff_summary = summary
            self._handoff_meta = meta
            return summary
        fallback = handoff.fallback_summary(self.session_events, previous=summary)
        if fallback:
            self._handoff_summary = handoff.write_summary(
                self.session_file,
                fallback,
                source_session=self.session_file,
                event_count=len(self.session_events),
                source="fallback",
            )
            self._handoff_meta = handoff.read_meta(self.session_file)
        self._schedule_summary_refresh(force=True)
        return self._handoff_summary

    def _schedule_summary_refresh(self, force: bool = False):
        self._summary_force = bool(force or self._summary_force)
        if self._summary_timer is not None and self.project_root and self.session_events:
            self._summary_timer.start(1200)

    def _summary_settings(self) -> dict:
        return (self.settings or {}).get("context", {}) or {}

    def _active_model_ids(self) -> tuple[str, str]:
        settings = self.settings or {}
        provider_id = settings.get("active_provider") or ""
        providers = settings.get("llm_providers", {}) or {}
        model_id = (providers.get(provider_id) or {}).get("model") or ""
        return provider_id, model_id

    def _summary_model_ids(self) -> tuple[str, str]:
        """Resolve ``context.summary_model`` (provider:model or bare model)."""
        provider_id, model_id = self._active_model_ids()
        raw = str(self._summary_settings().get("summary_model") or "").strip()
        if not raw:
            return provider_id, model_id
        for separator in (":", "/"):
            if separator in raw:
                summary_provider, summary_model = raw.split(separator, 1)
                summary_provider = summary_provider.strip()
                summary_model = summary_model.strip()
                if summary_provider and summary_model:
                    return summary_provider, summary_model
                break
        return provider_id, raw

    def _maybe_refresh_summary(self):
        if (self.project_manager is None or not self.project_root
                or not self.session_events or self._summary_pending):
            return
        if self._manual_compact_pending:
            self._maybe_finish_pending_compaction(from_timer=True)
            return
        context_settings = self._summary_settings()
        if not context_settings.get("handoff_enabled", True):
            return
        force = self._summary_force
        if not force and time.monotonic() < self._summary_retry_at:
            return
        if self.thinking_timer.isActive() and not force:
            return
        provider_id, model_id = self._active_model_ids()
        if not provider_id or not model_id:
            return
        meta = handoff.read_meta(self.session_file) or self._handoff_meta
        ratio = context_settings.get(
            "summary_threshold_ratio", handoff.DEFAULT_THRESHOLD_RATIO)
        if not handoff.needs_refresh(
                self.session_events, provider_id, model_id, meta, self.session_file,
                threshold_ratio=float(ratio or handoff.DEFAULT_THRESHOLD_RATIO),
                force=force):
            self._summary_force = False
            return
        self._request_summary_update(force=force)

    def _request_summary_update(self, force: bool = False,
                                advance_boundary: bool = False,
                                trigger: str = "",
                                focus: str = ""):
        if self._summary_pending:
            if advance_boundary:
                self._manual_compact_pending = True
                self._compaction_trigger = trigger or "manual"
                self._manual_compact_focus = (focus or "").strip()
            self._summary_force = True
            return
        session_file = self.session_file
        if not session_file or not self.session_events:
            return
        if advance_boundary:
            trigger = trigger or "manual"
            focus = (focus or "").strip()
            self._compaction_trigger = trigger
            self._manual_compact_focus = focus
        else:
            trigger = "background"
            focus = ""
        previous = handoff.read_summary(session_file) or self._handoff_summary
        meta = handoff.read_meta(session_file)
        same_session = bool(
            self.session_file and meta.get("source_session")
            and os.path.normcase(os.path.abspath(str(meta["source_session"])))
            == os.path.normcase(os.path.abspath(self.session_file)))
        processed = int(meta.get("event_count") or 0) if (
            same_session and meta.get("source") == "llm") else 0
        new_events = self.session_events[processed:]
        if not new_events and not focus:
            self._summary_force = False
            if advance_boundary:
                covered = handoff.summary_coverage(meta, self.session_events)
                self._apply_compaction(
                    covered, self._history_cut_key())
            return
        provider_id, model_id = self._summary_model_ids()
        max_tokens = int(self._summary_settings().get(
            "summary_max_tokens", handoff.DEFAULT_MAX_TOKENS) or handoff.DEFAULT_MAX_TOKENS)
        system_prompt, user_prompt = handoff.build_summary_prompts(
            previous,
            new_events,
            project_name=self.project_manager.get_active_name() if self.project_manager else "",
            mode=self.session_mode,
            max_tokens=max_tokens,
            focus=focus,
        )
        event_count = len(self.session_events)
        self._summary_request_context = {
            "session_file": session_file,
            "source_session": session_file,
            "event_count": event_count,
            "event_id": str(self.session_events[event_count - 1].get("id") or ""),
            "provider": provider_id,
            "model": model_id,
            "advance_boundary": bool(advance_boundary),
            "trigger": trigger,
            "focus": focus,
            "history_cut": self._history_cut_key() if advance_boundary else 0,
        }
        self._summary_pending = True
        self._summary_force = False
        self.summary_requested.emit(
            system_prompt, user_prompt, provider_id, model_id)

    def _handle_summary_response(self, text: str):
        context = self._summary_request_context
        self._summary_request_context = None
        self._summary_pending = False
        if not context:
            return
        self._summary_failures = 0
        self._summary_retry_at = 0.0
        advance = bool(context.get("advance_boundary"))
        trigger = str(context.get("trigger") or ("manual" if advance else "background"))
        session_file = context.get("session_file") or ""
        if not session_file or not os.path.exists(session_file):
            if advance:
                self._clear_pending_compaction(cooldown=(trigger == "auto"))
                self.ui.add_message(self._compaction_failure_message(
                    trigger, "the session is no longer available."))
            self._schedule_context_indicator_update()
            return
        previous = handoff.read_summary(session_file)
        normalized = handoff.normalize_summary(text, fallback=previous)
        if not normalized:
            logging.warning("Handoff summary was empty after normalization.")
            if advance:
                self._clear_pending_compaction(cooldown=(trigger == "auto"))
                self.ui.add_message(self._compaction_failure_message(
                    trigger,
                    "the summary came back empty. Nothing was removed."))
            self._schedule_context_indicator_update()
            return
        event_count = int(context.get("event_count") or 0)
        event_id = str(context.get("event_id") or "")
        written = handoff.write_summary(
            session_file,
            normalized,
            source_session=session_file,
            event_count=event_count,
            provider=context.get("provider", ""),
            model=context.get("model", ""),
            source="llm",
            compacted_through=event_id if advance else "",
            compacted_event_count=event_count if advance else 0,
        )
        if not written:
            if advance:
                self._clear_pending_compaction(cooldown=(trigger == "auto"))
                self.ui.add_message(self._compaction_failure_message(
                    trigger,
                    "the summary could not be saved. Nothing was removed."))
            self._schedule_context_indicator_update()
            return
        if session_file == self.session_file:
            self._handoff_summary = written
            self._handoff_meta = handoff.read_meta(session_file)
            if advance:
                trigger = str(context.get("trigger") or "manual")
                self._clear_pending_compaction(cooldown=True)
                covered = handoff.compacted_event_count(
                    self._handoff_meta, self.session_events)
                if covered:
                    self._apply_compaction_marker(
                        int(context.get("history_cut") or 0))
                    self._add_compaction_message(
                        trigger, covered, len(self.session_events))
                else:
                    self.ui.add_message(self._compaction_failure_message(
                        trigger,
                        "the boundary could not be recorded. Nothing was removed."))
                self._refresh_agent_context()
            else:
                self._refresh_agent_context()
        elif advance:
            self._clear_pending_compaction(cooldown=True)
            logging.info("Compaction finished for a session that is no "
                         "longer active: %s", session_file)
        logging.info("Handoff summary updated (%d chars).", len(written))
        self._schedule_context_indicator_update()
        if self._manual_compact_pending:
            self._maybe_finish_pending_compaction()
        elif self._summary_force:
            self._schedule_summary_refresh(force=True)
        else:
            self._schedule_auto_compact_check()

    def _handle_summary_error(self, message: str):
        self._summary_request_context = None
        self._summary_pending = False
        pending = self._manual_compact_pending
        trigger = self._compaction_trigger or "manual"
        if pending:
            self._clear_pending_compaction()
            self.ui.add_message(self._compaction_failure_message(
                trigger,
                "the session summary could not be refreshed. "
                "Nothing was removed."))
        self._summary_failures += 1
        self._summary_force = False
        delay = min(300.0, 15.0 * (2 ** min(self._summary_failures - 1, 4)))
        self._summary_retry_at = time.monotonic() + delay
        self._schedule_context_indicator_update()
        logging.warning(
            "Handoff summary update failed (%d); retry in %.0fs: %s",
            self._summary_failures, delay, message)

    def record_provider_usage(self, usage: dict):
        """Record usage reported by provider calls outside the LLM worker."""
        self._handle_usage(usage)

    def _maybe_warn_context_usage(self, usage: dict):
        if not self.ui or self._context_warning_file == self.session_file:
            return
        total = int(usage.get("total") or 0)
        if not total:
            total = sum(int(usage.get(key) or 0) for key in
                        ("input", "output", "reasoning", "cache_read", "cache_write"))
        if not total:
            return
        provider_id = str(usage.get("provider") or "")
        model_id = str(usage.get("model") or "")
        if not provider_id or not model_id:
            provider_id, model_id = self._active_model_ids()
        if not provider_id or not model_id:
            return
        window = ModelMetadata.context_window(provider_id, model_id)
        if not window:
            return
        ratio = total / window
        if ratio < 0.8:
            return
        self._context_warning_file = self.session_file
        hint = ("Use Compact now in Session context to replace older history "
                "with the summary.")
        if (self._summary_settings().get("auto_compact", True)
                and self.session_mode in ("chat", "terminal", "browser")):
            hint = ("Use Compact now in Session context, or let the automatic "
                    "compaction run once the current step finishes.")
        self.ui.add_message(
            f"<i>Context window is {ratio * 100:.0f}% full "
            f"({total:,} / {window:,} tokens). {hint}</i>")

    def _handle_usage(self, usage: dict):
        if not isinstance(usage, dict) or not usage:
            return
        purpose = str(usage.get("purpose") or "completion")
        fields = {"purpose": purpose}
        for key in ("provider", "model"):
            value = usage.get(key)
            if value:
                fields[key] = str(value)
        for key in ("input", "output", "reasoning", "cache_read", "cache_write",
                    "total"):
            value = usage.get(key)
            if value is not None:
                try:
                    fields[key] = int(value)
                except (TypeError, ValueError):
                    continue

        if purpose == "completion":
            self._maybe_warn_context_usage(fields)

        target_session = self.session_file
        if purpose == "summary" and self._summary_request_context:
            target_session = (
                self._summary_request_context.get("source_session")
                or target_session
            )
        turn_id = "" if purpose == "summary" else (self.session_turn_id or "")
        if target_session and target_session != self.session_file:
            event = self.project_manager.make_event(
                "usage", turn_id=turn_id, **fields) if self.project_manager else None
            if event and self.project_manager.append_event(target_session, event) is not None:
                return
        self.append_session_event("usage", turn_id=turn_id, **fields)

    def _refresh_agent_context(self):
        """Refresh generated project-rule and handoff blocks in agent history."""
        if self.session_mode == "chat":
            return
        handler = self._handler_for_mode(self.session_mode)
        if handler is None or not hasattr(handler, "command_history"):
            return
        handoff_prefix = f"**{handoff.HANDOFF_LABEL}"
        history = [
            item for item in (handler.command_history or [])
            if not (str(item).startswith(handoff_prefix)
                    or str(item).startswith(project_rules.BLOCK_PREFIX))
        ]
        blocks = [
            block for block in (
                self.project_baseline_prompt(), self.handoff_prompt()) if block
        ]
        if blocks:
            history[0:0] = blocks
        handler.command_history = history

    def _restore_agent_history(self):
        """Rebuild the current agent's model-facing history from JSONL events."""
        self._reset_agent_histories()
        if self.session_mode == "chat":
            return
        handler = self._handler_for_mode(self.session_mode)
        if handler is None:
            return
        session_block = self.session_context_prompt()
        if not self.session_events:
            blocks = [
                block for block in (
                    self.project_baseline_prompt(), session_block,
                    self.handoff_prompt()) if block
            ]
            if blocks:
                handler.command_history = blocks
            return
        covered = self._compacted_event_count()
        if covered and not (
                handoff.read_summary(self.session_file) or self._handoff_summary):
            covered = 0
        history = []
        last_user_request = None
        last_action_sig = None
        for event in self.session_events:
            if event.get("type") == "message" and event.get("role") == "user":
                text = event_text(event)
                if text:
                    last_user_request = text
            elif event.get("type") == "tool_call":
                args = event.get("input") or {}
                if not isinstance(args, dict):
                    args = {}
                last_action_sig = (
                    str(event.get("name") or "action"),
                    json.dumps(args, sort_keys=True, ensure_ascii=False))
        for event in self.session_events[covered:]:
            event_type = event.get("type")
            if event_type == "message":
                if event.get("role") != "user":
                    continue
                text = event_text(event)
                attachments = self._format_event_attachments(event)
                if text:
                    history.append(f"**User Request:** {text}")
                elif not attachments:
                    continue
                if attachments:
                    history.append(f"**Attachments:**\n{attachments}")
            elif event_type == "reasoning":
                text = event_text(event)
                if text:
                    history.append(f"**Model reasoning:** {text}")
            elif event_type == "tool_call":
                name = str(event.get("name") or "action")
                args = event.get("input") or {}
                if not isinstance(args, dict):
                    args = {}
                history.append(
                    f"**Agent Command:** `{name}"
                    f"({json.dumps(args, ensure_ascii=False)})`")
            elif event_type == "tool_result":
                history.append(event_text(event) or "**Tool Result:** (no output)")
            elif event_type == "job_result":
                history.append(
                    event_text(event)
                    or f"**Background job #{event.get('job_id', '?')} finished.**")
            elif event_type == "error":
                message = str(event.get("message") or "").strip()
                if message:
                    history.append(f"**Error:** {message}")
        blocks = [
            block for block in (
                self.project_baseline_prompt(), session_block,
                self.handoff_prompt()) if block
        ]
        history[0:0] = blocks
        if covered:
            history.insert(len(blocks), model_context.COMPACTION_MARKER)
        handler.command_history = history
        if last_user_request:
            handler.original_user_request = last_user_request
        if last_action_sig is not None:
            handler._last_action_sig = last_action_sig
        mode_name = getattr(handler, "MODE_NAME", type(handler).__name__)
        compacted_note = f", {covered} earlier compacted" if covered else ""
        logging.info(
            f"Rebuilt {mode_name} history with {len(history)} entries "
            f"from {len(self.session_events)} session events{compacted_note}.")

    def _event_attachment_records(self, event: dict) -> list:
        """Attachment metadata with its current on-disk status."""
        records = []
        for record in event.get("attachments") or []:
            if not isinstance(record, dict):
                continue
            item = dict(record)
            if self.project_manager is not None:
                try:
                    item["status"] = attachment_store.verify_attachment(
                        self.project_manager, self.session_file, item,
                        self.project_root)
                except Exception as e:
                    logging.warning(f"Could not verify attachment: {e}")
                    item["status"] = "missing"
            records.append(item)
        return records

    def _format_event_attachments(self, event: dict) -> str:
        """Model-facing lines for attachments recorded on a user message."""
        return attachment_store.context_block(
            self._event_attachment_records(event))

    def _journal_attachment_lines(self, event: dict) -> str:
        """Debug metadata for attachments, shown under a journal message."""
        lines = []
        for record in self._event_attachment_records(event):
            name = str(record.get("name") or "attachment")
            kind = str(record.get("kind") or "file")
            size = attachment_store.format_size(record.get("size"))
            digest = str(record.get("sha256") or "(none)")
            stored = "yes" if record.get("stored") else "no"
            status = str(record.get("status") or "ok")
            path = str(record.get("path") or "") or "(none)"
            lines.append(
                f"  - {name} [{kind}] size={size} sha256={digest} "
                f"stored={stored} status={status}\n"
                f"    path: {path}")
        if not lines:
            return ""
        return "attachments:\n" + "\n".join(lines)

    def _replay_tool_event(self, event: dict):
        """Generic fallback for modes without their own event renderer."""
        event_type = event.get("type")
        if event_type == "tool_call":
            name = str(event.get("name") or "action")
            args = event.get("input") or {}
            self.ui.add_message(f"<b>Executing:</b> {html.escape(name)}")
            self.ui.add_message({
                "type": "collapsible",
                "title": "Arguments",
                "content": json.dumps(args, ensure_ascii=False, indent=2),
            })
            return
        status = str(event.get("status") or "")
        if event_type == "job_result":
            title = (f"Background job #{event.get('job_id', '?')} · "
                     f"{status or 'result'}")
        else:
            title = f"{event.get('name') or 'Tool'} · {status or 'result'}"
        text = event_text(event) or "(no output)"
        if text.startswith("**Tool Result:**\n"):
            text = text[len("**Tool Result:**\n"):].strip()
            if text.startswith("```") and text.endswith("```"):
                text = text[3:-3].strip("\n")
        shown = text
        if len(shown) > 30000:
            shown = shown[:30000] + (
                f"\n… [truncated for display, {len(text) - 30000} chars hidden]")
        block = {
            "type": "collapsible",
            "title": title,
            "content": shown,
            "plain_content": text,
            "copy_payloads": {"output": text},
            "state": "collapsed",
        }
        if status == "error":
            block["title_color"] = self.theme_manager.get_style("ERROR_COLOR")
        self.ui.add_message(block)

    def _replay_session(self):
        """Render the loaded session transcript and tool cards into the UI."""
        # A full replay always renders with the live theme, so it also
        # satisfies any deferred theme re-render.
        self._theme_replay_pending = False
        events = list(self.session_events or [])
        if not events or getattr(self, "ui", None) is None:
            return
        try:
            self.ui.clear_chat()
        except Exception:
            pass
        handler = self._handler_for_mode(self.session_mode)
        for event in events:
            event_type = event.get("type")
            if event_type == "message":
                text = event_text(event)
                role = event.get("role")
                if role == "user":
                    records = self._event_attachment_records(event)
                    if text or records:
                        self.ui.add_message(
                            self.ui.build_user_message_html(
                                text, attachments=records), role="user")
                elif not text:
                    continue
                elif role == "assistant":
                    self.ui.add_message(
                        render_text(text, markdown_ok=True))
                else:
                    self.ui.add_message(
                        f"<p><i>{html.escape(text)}</i></p>")
            elif event_type == "reasoning":
                text = event_text(event)
                if text:
                    self.ui.add_reasoning(text, force_scroll=False)
            elif event_type in ("tool_call", "tool_result", "job_result"):
                replayed = False
                replay = getattr(handler, "replay_event", None)
                if callable(replay):
                    try:
                        replayed = bool(replay(event))
                    except Exception as e:
                        logging.warning(f"Could not replay {event_type}: {e}")
                if not replayed:
                    self._replay_tool_event(event)
            elif event_type == "error":
                message = str(event.get("message") or event_text(event) or "Error")
                error_style = self.theme_manager.get_style("ERROR_MESSAGE_STYLE")
                self.ui.add_message(
                    f'<span style="{error_style}">{html.escape(message)}</span>')
        try:
            self.ui.chat_widget.scroll_to_bottom()
        except Exception:
            pass
        logging.info(f"Replayed {len(events)} session events into the UI.")

    def _on_theme_changed(self):
        """Rebuilds the transcript so its baked HTML colors match the new theme."""
        self._theme_replay_pending = True
        self._consume_theme_replay()

    def _theme_replay_safe(self) -> bool:
        """Whether rebuilding the transcript cannot clobber live UI state."""
        if self.session_mode == "chat":
            # A reply streams into a tracked live message; replay would orphan it.
            return not self.thinking_timer.isActive() and not self.chat_queue
        handler = self._handler_for_mode(self.session_mode)
        if handler is None:
            return False
        return str(getattr(handler.state, "name", "")) == "IDLE"

    def _consume_theme_replay(self):
        """Queues a transcript re-render once the active mode is at rest."""
        if not self._theme_replay_pending or not self._theme_replay_safe():
            return
        QTimer.singleShot(0, self._apply_theme_replay)

    def _apply_theme_replay(self):
        # Re-check: the just-finished turn may have queued a new one, or the
        # user switched modes while this callback was waiting.
        if not self._theme_replay_pending or not self._theme_replay_safe():
            return
        self._replay_session()

    # --- MCP servers ------------------------------------------------------

    def _mcp_config(self):
        """Merges user MCP servers from settings with the project's .mcp.json."""
        from src.mcp_client import load_servers
        settings = self.settings_manager.get("mcp", None) or {}
        if not isinstance(settings, dict):
            settings = {}
        servers = settings.get("servers") or {}
        trusted_map = settings.get("trusted_projects") or {}
        trusted: list[str] = []
        if isinstance(trusted_map, dict):
            value = trusted_map.get(self.project_root, [])
            if isinstance(value, (list, tuple)):
                trusted = [str(name) for name in value]
        return load_servers(servers, self.project_root, trusted)

    def _attach_mcp_manager(self):
        manager = self._mcp_manager
        if manager is None:
            return
        computer = getattr(getattr(self, "terminal_handler", None), "computer", None)
        if computer is not None:
            computer.mcp_manager = manager

    @property
    def mcp_manager(self):
        return self._mcp_manager

    def mcp_config(self):
        """Current merged MCP configuration (user settings + project file)."""
        return self._mcp_config()

    def ensure_mcp_manager(self):
        """Creates and starts the MCP manager once, then applies config changes."""
        try:
            config = self._mcp_config()
        except Exception as error:
            logging.error(f"MCP configuration failed: {error}", exc_info=True)
            return self._mcp_manager
        for message in config.errors:
            logging.warning(f"MCP config: {message}")

        manager = self._mcp_manager
        if manager is None:
            if not config.servers:
                return None
            from src.mcp_client import MCPManager
            manager = MCPManager(config.servers)
            try:
                manager.start()
            except Exception as error:
                logging.error(f"MCP manager start failed: {error}", exc_info=True)
                return None
            self._mcp_manager = manager
            logging.info(f"MCP manager started with {len(config.servers)} server(s).")
        else:
            try:
                manager.refresh(config.servers)
            except Exception as error:
                logging.warning(f"MCP manager refresh failed: {error}")
        self._attach_mcp_manager()
        return manager

    def refresh_mcp_servers(self):
        """Re-applies MCP configuration after the active project changes."""
        if self._mcp_manager is None:
            return None
        return self.ensure_mcp_manager()

    def _set_project_context(self, root: str):
        """Re-roots the indexer and the terminal/browser working directories."""
        self.project_root = os.path.abspath(root)
        self.working_directory = self.project_root
        for computer in self._project_computers():
            computer.working_directory = self.project_root
        try:
            if self.indexer is not None:
                self.indexer.stop()
        except Exception as e:
            logging.warning(f"Indexer stop during project switch failed: {e}")
        self.indexer = Indexer(self.project_root)
        self.refresh_mcp_servers()

    def _project_computers(self) -> list:
        """Computers whose working directory follows the active project."""
        computers = [getattr(self, "computer", None)]
        for handler in (getattr(self, "terminal_handler", None),
                        getattr(self, "browser_handler", None)):
            computers.append(getattr(handler, "computer", None))
        return [computer for computer in computers if computer is not None]
        self.indexer.is_active_cb = lambda: self.state == ControllerState.TERMINAL
        self.indexer.index_updated.connect(self._on_index_updated)
        self.last_search_query = None
        self.indexer.start()

    def _stop_active_work(self):
        """Cancels streaming/queued work and shell jobs before switching context."""
        self._clear_chat_queue()
        try:
            if self.thinking_timer.isActive() or self.state != ControllerState.IDLE:
                self.handle_cancel_request()
        except Exception as e:
            logging.warning(f"Could not cancel work on project switch: {e}")
        try:
            computer = getattr(self.terminal_handler, "computer", None)
            manager = getattr(computer, "shell_jobs", None)
            if manager is not None:
                manager.kill_all()
        except Exception as e:
            logging.warning(f"Could not stop shell jobs on project switch: {e}")
        self._forget_last_request()
        try:
            self.thinking_timer.stop()
            self.ui.stop_retry_countdown()
            self.ui.show_thinking_ui(False)
        except Exception:
            pass

    def _report_project_error(self, message: str):
        """Shows project errors on Home (dialog) or in the chat workspace."""
        message = str(message)
        if self.ui.home_page.isVisible():
            QMessageBox.warning(self.ui.main_window, "Projects", message)
        else:
            self.ui.add_message(f"<i>{message}</i>", force_scroll=True)

    def switch_project(self, path: str, session_file: str = "", announce: bool = True):
        """Switches the active project and optionally opens one of its sessions."""
        if self.project_manager is None or not path:
            return
        target = os.path.abspath(path)
        if target == self.project_root and not session_file:
            self.ui.show_workspace()
            return
        if not os.path.isdir(target):
            self._report_project_error("Project folder not found.")
            return
        if self._summary_settings().get("handoff_enabled", True):
            self._capture_handoff_summary()
        try:
            self.project_manager.open_project(target)
        except Exception as e:
            self._report_project_error(f"Could not switch project: {e}")
            return

        self._stop_active_work()
        self._persist_session()
        self.conversation_history.clear()
        self.pending_attachments.clear()
        try:
            self.ui.clear_thumbnails()
            self._update_attachment_menu()
        except Exception as e:
            logging.warning(f"Could not clear attachments on project switch: {e}")

        self.session_file = ""
        self.session_id = ""
        self.session_objective = ""
        self._set_project_context(target)
        if session_file:
            self._load_session_file(session_file)
        else:
            self._load_project_session()
        self._load_handoff_state()
        try:
            self.ui.clear_chat()
        except Exception:
            pass
        name = self.project_manager.get_active_name()
        self.ui.set_project_name(name, self.project_root)
        self._persist_session()
        self.ui.show_workspace()
        self.ui.refresh_project_views()
        self._restore_agent_history()
        self._replay_session()
        if announce:
            self.ui.add_message(f"<i>Project switched to <b>{name}</b>.</i>", force_scroll=True)

    def open_session(self, session_path: str):
        """Opens a saved session from the home screen or sidebar."""
        if self.project_manager is None or not session_path:
            return
        root = self.project_manager.session_project_root(session_path)
        if not root:
            self._report_project_error("Could not locate the project for this session.")
            return
        self.switch_project(root, session_file=session_path, announce=False)
        self.ui.add_message("<i>Opened saved session.</i>", force_scroll=True)

    def rename_project(self, path: str, new_name: str):
        """Stores a custom project name; the folder on disk keeps its name."""
        if self.project_manager is None or not path:
            return
        try:
            name = self.project_manager.rename_project(path, new_name)
        except Exception as e:
            self._report_project_error(f"Could not rename the project: {e}")
            return
        if not name:
            return
        if self.project_root and os.path.abspath(path) == self.project_root:
            self.ui.set_project_name(name, self.project_root)
        self.ui.refresh_project_views()

    def remove_project(self, path: str, delete_data: bool = False):
        """Removes a project from the list; the project folder itself stays."""
        if self.project_manager is None or not path:
            return
        target = os.path.abspath(path)
        is_active = bool(self.project_root) and target == self.project_root
        if is_active:
            self._stop_active_work()
            self._persist_session()
        try:
            self.project_manager.remove_project(target, delete_data=delete_data)
        except Exception as e:
            self._report_project_error(f"Could not remove the project: {e}")
            return
        if is_active:
            self.project_root = ""
            self.working_directory = ""
            self.session_file = ""
            self.session_id = ""
            self.session_objective = ""
            self.session_events = []
            self._session_event_count = 0
            self._persisted_message_count = 0
            self.conversation_history.clear()
            self.pending_attachments.clear()
            for computer in self._project_computers():
                try:
                    computer.working_directory = ""
                except Exception:
                    pass
            try:
                if self.indexer is not None:
                    self.indexer.stop()
            except Exception as e:
                logging.warning(f"Indexer stop after project removal failed: {e}")
            self.indexer = None
            try:
                self.ui.clear_chat()
                self.ui.clear_thumbnails()
                self._update_attachment_menu()
            except Exception as e:
                logging.warning(f"Could not clear the UI after removing the project: {e}")
            self.ui.show_home()
        self.ui.refresh_project_views()

    def rename_session(self, path: str, new_title: str):
        """Stores a custom session title in the session header."""
        if self.project_manager is None or not path:
            return
        try:
            renamed = self.project_manager.set_session_title(path, new_title)
        except Exception as e:
            self._report_project_error(f"Could not rename the session: {e}")
            return
        if not renamed:
            self._report_project_error("The session title could not be saved.")
            return
        self.ui.refresh_project_views()
        self._schedule_context_indicator_update()

    def delete_session(self, path: str):
        """Deletes a session; deleting the open one leaves an empty chat.

        No journal is created until the next message, so opening another
        session or leaving the empty chat leaves nothing behind.
        """
        if self.project_manager is None or not path:
            return
        target = os.path.abspath(path)
        is_open = bool(self.session_file) and target == os.path.abspath(self.session_file)
        if is_open:
            self._stop_active_work()
        try:
            deleted = self.project_manager.delete_session(target)
        except Exception as e:
            self._report_project_error(f"Could not delete the session: {e}")
            return
        if not deleted:
            self._report_project_error("The session file could not be deleted.")
            return
        if is_open:
            self.session_file = ""
            self.session_id = ""
            self.session_objective = ""
            self.session_turn_id = ""
            self.session_events = []
            self._session_event_count = 0
            self._persisted_message_count = 0
            self._handoff_summary = ""
            self._handoff_meta = {}
            self._summary_force = False
            self._manual_compact_pending = False
            self._compaction_trigger = ""
            self._manual_compact_focus = ""
            self.conversation_history.clear()
            self.pending_attachments.clear()
            try:
                self.ui.clear_chat()
                self.ui.clear_thumbnails()
                self._update_attachment_menu()
            except Exception as e:
                logging.warning(f"Could not clear the UI after deleting the session: {e}")
            if self.project_root:
                try:
                    self._reset_agent_histories()
                    self._restore_agent_history()
                except Exception as e:
                    logging.warning(f"Could not reset the agent after deleting the session: {e}")
            self._schedule_context_indicator_update()
        self.ui.refresh_project_views()

    def new_session(self):
        """Starts an empty session in the active project."""
        if self.project_manager is None or not self.project_root:
            self.ui.show_home()
            return
        if self._summary_settings().get("handoff_enabled", True):
            self._capture_handoff_summary()
        self._stop_active_work()
        self._persist_session()
        self.conversation_history.clear()
        self.pending_attachments.clear()
        try:
            self.ui.clear_thumbnails()
            self._update_attachment_menu()
        except Exception as e:
            logging.warning(f"Could not clear attachments for new session: {e}")
        self.session_id = ""
        self.session_objective = ""
        try:
            self._create_new_session(self._current_mode())
        except Exception as e:
            logging.warning(f"Could not create a new session: {e}")
            self.session_file = ""
        self._reset_agent_histories()
        self._restore_agent_history()
        try:
            self.ui.clear_chat()
        except Exception:
            pass
        self.ui.show_workspace()
        self.ui.refresh_project_views()
        self.ui.add_message("<i>Started a new session.</i>", force_scroll=True)

    def _handle_open_project(self):
        start = os.path.expanduser("~")
        active = self.project_manager.get_active_root() if self.project_manager else ""
        if active:
            start = os.path.dirname(active)
        path = QFileDialog.getExistingDirectory(
            self.ui.main_window, "Open Project Folder", start)
        if path:
            self.switch_project(path)

    def _start_vision_initialization(self):
        """Marks Vision ready immediately (agent-loop rebuild).

        The legacy local grounding backend (YOLO/OCR/Florence) was extracted:
        there is nothing to initialize in a background thread anymore. Vision
        grounds through the active provider's native Computer Use (or, later,
        the recognizer service), both created on demand per task.
        """
        self.is_vision_module_initialized = True
        try:
            if self.ui and hasattr(self.ui, "spinner_animation"):
                self.ui.spinner_animation.stop()
            self.ui.set_vision_mode_loading(False)
        except Exception:
            pass
        self.vision_module_ready.emit()

    def _set_state(self, new_state: ControllerState):
        logging.info(f"AppController state changed from {self.state.name} to {new_state.name}")
        self.state = new_state
        self._schedule_context_indicator_update()
        if new_state in (ControllerState.IDLE, ControllerState.TERMINAL,
                         ControllerState.BROWSER):
            self._schedule_auto_compact_check()
        self._consume_theme_replay()

    def handle_user_input(self, user_text):
        if not self.llm_worker.provider:
            active = (self.settings.get("active_provider") or "")
            configured = provider_is_configured(self.settings, active)
            if active and configured:
                # Configured, but the background thread has not finished yet.
                self.ui.add_message(
                    "<i>The AI provider is still initializing, please wait a few seconds and send again.</i>",
                    force_scroll=True
                )
            else:
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

        # While a chat reply is still streaming, a new message is queued (shown
        # dimmed in the thread) instead of cancelling the in-flight request. It
        # is answered, in order, once the current reply finishes.
        if self.state == ControllerState.CHATTING and self.thinking_timer.isActive():
            self._enqueue_chat_message(user_text)
            return

        attachment_records = self._record_pending_attachments()
        self.session_turn_id = uuid.uuid4().hex
        self.ui.add_user_message(user_text, attachments=attachment_records)
        self.conversation_history.append({'role': 'user', 'content': user_text})
        self._persist_session(attachments=attachment_records)
        self._ensure_session_objective(user_text)

        # Process attachments based on the current mode. Agent modes get a
        # reference block and read the file on demand; Chat has no tools, so
        # text contents are inlined once for the current request.
        attachment_context = attachment_store.context_block(attachment_records)
        attachments_for_handler = []
        if self.pending_attachments:
            if self.state == ControllerState.TERMINAL:
                if attachment_context:
                    self.terminal_handler.command_history.append(attachment_context)
                for attachment in self.pending_attachments:
                    try:
                        attachment_type = attachment.get('type')
                        path = attachment.get('path')
                        if attachment_type == 'folder':
                            self.ui.add_message(
                                f"<i>Folder attached as a reference: <code>{path}</code></i>",
                                force_scroll=True)
                        elif path:
                            self.ui.add_message(
                                f"<i>File attached as a reference: <code>{path}</code></i>",
                                force_scroll=True)
                    except Exception as e:
                        logging.error(f"Error processing attachment for terminal: {e}")
                        self.ui.add_error_message(f"Error processing attachment: {e}")
            else:
                attachments_for_handler.extend(self._collect_chat_attachments())

        self.pending_attachments.clear()
        self.ui.clear_thumbnails()
        self._update_attachment_menu()

        paused_agent = self._paused_agent()
        if paused_agent is not None:
            # Task frozen mid-way: chat freely, actions stay frozen until
            # Resume. Recent task history is prepended so answers can
            # reference the work. PAUSED_ON_ERROR is NOT detoured: an
            # explicit model question still auto-continues the task.
            recent = paused_agent.command_history[-8:] if paused_agent.command_history else []
            context = "\n".join(str(x) for x in recent)
            question = (f"[Paused {paused_agent.MODE_NAME} task — actions frozen, "
                        f"answering as chat]\n"
                        + (f"Recent task history:\n{context}\n\n" if context else "")
                        + f"User question while paused: {user_text}")
            self.chat_handler.start_chat(
                self._chat_prompt(question, attachment_records),
                attachments_for_handler)
            return

        if self.state == ControllerState.VISION:
            if self.vision_handler.state == VisionState.PAUSED_ON_ERROR:
                self.vision_handler.resume_with_guidance(user_text, attachments_for_handler)
            else:
                self.vision_handler.start_task(user_text, attachments_for_handler)
        elif self.state == ControllerState.TERMINAL:
            self._refresh_agent_context()
            self.consume_handoff_seed()
            self.terminal_handler.start_task(user_text)
        elif self.state == ControllerState.BROWSER:
            self._refresh_agent_context()
            self.consume_handoff_seed()
            self.browser_handler.start_task(user_text, attachment_context)
        else:
            self.chat_handler.start_chat(
                self._chat_prompt(user_text, attachment_records),
                attachments_for_handler)

    def _chat_prompt(self, user_text: str, records: list) -> str:
        """Chat has no tool loop: inline text files once, keep paths as refs."""
        parts = [user_text] if user_text else []
        inline = attachment_store.inline_text_block(records, self.project_root)
        if inline:
            parts.append(inline)
        refs = attachment_store.context_block(records, can_read=False)
        if refs:
            parts.append(refs)
        return "\n\n".join(parts)

    def _record_pending_attachments(self) -> list:
        """Journal metadata for the pending attachments of the current send."""
        if not self.pending_attachments or self.project_manager is None:
            return []
        if not self.session_file:
            try:
                self._create_new_session(self.session_mode)
            except Exception as e:
                logging.warning(f"Could not create a session for attachments: {e}")
                return []
        root = self.project_root or self.project_manager.get_active_root()
        records = []
        for attachment in self.pending_attachments:
            try:
                records.append(attachment_store.record_attachment(
                    self.project_manager, self.session_file, attachment, root))
            except Exception as e:
                logging.warning(f"Could not record attachment: {e}")
        return records

    def _collect_chat_attachments(self, while_streaming: bool = False) -> list:
        """Reads pending image attachments into LLM-ready payloads.

        Text files are handled by ``inline_text_block`` instead: providers
        convert every attachment as an image, so raw text bytes cannot be
        passed through this path.

        ``while_streaming`` suppresses the (chat-bubble) error notice, which
        would otherwise reset the active stream state when a queued message's
        attachment fails to load.
        """
        payloads = []
        for attachment in self.pending_attachments:
            try:
                if isinstance(attachment, dict):
                    attachment_type = attachment.get('type')
                else:
                    # Raw payload (e.g. screenshot bytes queued directly).
                    payloads.append(attachment)
                    continue
                if attachment_type == 'file':
                    path = attachment.get('path') or ""
                    _, ext = os.path.splitext(path.lower())
                    if ext not in attachment_store.IMAGE_EXTENSIONS:
                        continue
                    with open(path, 'rb') as f:
                        payloads.append(f.read())
                elif attachment_type == 'image':
                    payloads.append(attachment.get('data'))
            except Exception as e:
                logging.error(f"Error processing attachment for chat/vision: {e}")
                if not while_streaming:
                    self.ui.add_error_message(f"Could not process attachment: {e}")
        return payloads

    def _enqueue_chat_message(self, user_text):
        """Queues a chat message typed while a reply is streaming."""
        if len(self.chat_queue) >= self.CHAT_QUEUE_MAX:
            self.ui.add_message(
                f"<i>Queue is full ({self.CHAT_QUEUE_MAX}) — wait for the "
                "current replies to finish, then send again.</i>",
                force_scroll=True,
            )
            try:
                self.ui.user_input.setPlainText(user_text)
                self.ui.user_input.setFocus()
            except Exception:
                pass
            return

        attachment_records = self._record_pending_attachments()
        attachments = self._collect_chat_attachments(while_streaming=True)
        self.pending_attachments.clear()
        self.ui.clear_thumbnails()
        self._update_attachment_menu()

        label = user_text if user_text.strip() else "(attachment)"
        raw_html = self.ui.build_user_message_html(
            label, attachments=attachment_records)
        display_html = self.ui.build_user_message_html(
            label, queued=True, attachments=attachment_records)
        handle = self.ui.add_queued_message(raw_html, display_html)
        self.chat_queue.append({
            'text': user_text,
            'attachments': attachments,
            'attachment_records': attachment_records,
            'ui_handle': handle,
        })
        logging.info(f"Chat message queued (position {len(self.chat_queue)}).")

    def _drain_chat_queue(self):
        """Starts the oldest queued chat message, if any. Returns True if one ran."""
        if not self.chat_queue:
            return False
        item = self.chat_queue.pop(0)
        try:
            self.ui.activate_queued_message(item.get('ui_handle'))
        except Exception as e:
            logging.warning(f"Could not activate queued chat message: {e}")
        self.current_handler = self.chat_handler
        self.session_turn_id = uuid.uuid4().hex
        self.conversation_history.append({'role': 'user', 'content': item['text']})
        self._persist_session(attachments=item.get('attachment_records') or [])
        self._ensure_session_objective(item['text'])
        logging.info(f"Dequeuing chat message ({len(self.chat_queue)} left).")
        prompt = self._chat_prompt(
            item['text'], item.get('attachment_records') or [])
        self.chat_handler.start_chat(prompt, item.get('attachments') or [])
        return True

    def _clear_chat_queue(self):
        """Drops all queued chat messages (used when leaving chat mode)."""
        while self.chat_queue:
            item = self.chat_queue.pop(0)
            try:
                self.ui.remove_queued_message(item.get('ui_handle'))
            except Exception:
                pass

    def handle_response_chunk(self, chunk):
        self.ui.stop_retry_countdown()
        self.current_handler.handle_response_chunk(chunk)

    def _handle_chat_response(self, full_response):
        if self.state == ControllerState.CHATTING:
            self.chat_handler.handle_chat_response(full_response)
        else:
            # The full response is already logged to the llm_communication.log file.
            # This warning is just to indicate that an unexpected response type was received.
            logging.warning("Received a simple chat response while in Vision mode. Ignoring.")
            self.thinking_timer.stop()
            self.ui.show_thinking_ui(False)

    def _handle_structured_response(self, reasoning: str, tool_calls_obj, schema_name: str):
        # The full response is already logged to the llm_communication.log file.
        if self.state == ControllerState.TERMINAL:
            self.terminal_handler.handle_llm_response(reasoning, tool_calls_obj, schema_name)
        elif self.state == ControllerState.BROWSER:
            self.browser_handler.handle_llm_response(reasoning, tool_calls_obj, schema_name)
        else:
            logging.warning("Received a structured response in an unexpected state. Ignoring.")
            self.thinking_timer.stop()
            self.ui.show_thinking_ui(False)

    def _handle_plain_text_response(self, text: str):
        """Handles a plain text response received instead of a structured one."""
        # The full response is already logged to the llm_communication.log file.
        logging.warning("Received unexpected plain text response for a structured request.")
        self.thinking_timer.stop()
        self.ui.show_thinking_ui(False)

    def _handle_retry_attempt(self, attempt_num, max_attempts, error, wait_seconds):
        """Updates the UI to show that a retry is happening."""
        logging.info(f"Handling retry attempt {attempt_num}/{max_attempts} in UI.")
        self.thinking_timer.stop()
        self.ui.show_retry_countdown(attempt_num, max_attempts, wait_seconds)

    def _handle_retry_countdown(self, remaining):
        """Updates the visible countdown to the next automatic retry."""
        self.ui.update_retry_countdown(remaining)

    def handle_llm_error(self, error_message):
        """
        Handles a fatal LLM error after all retries have failed.
        Routes the error to the appropriate handler based on the current state.
        """
        self.thinking_timer.stop()
        self.ui.stop_retry_countdown()
        self.ui.show_thinking_ui(False)
        self._last_llm_error = True
        self.append_session_event(
            "error",
            turn_id=self.session_turn_id,
            scope="llm",
            message=str(error_message),
            retryable=True,
        )

        if self.state == ControllerState.VISION:
            # The vision handler will manage UI changes (like switching to full mode)
            self.vision_handler.handle_fatal_error(error_message)
        elif self.state == ControllerState.CHATTING:
            self.chat_handler.handle_fatal_error(error_message)
        else:
            # Fallback for IDLE state or agent modes without a dedicated
            # fatal-error path (TERMINAL/BROWSER). Show the error but keep
            # the current mode state: dropping to IDLE would orphan the
            # handler loop and break the Retry button (the retried response
            # would arrive "in chat mode" and be ignored).
            self.ui.add_message(
                f"<b>A critical error occurred:</b> {error_message}<br>"
                "Please try your request again.",
                force_scroll=True
            )
            if self.state not in (ControllerState.TERMINAL, ControllerState.BROWSER, ControllerState.VISION):
                self._set_state(ControllerState.IDLE)

    def _send_llm_request(self, user_prompt, attachments, system_prompt, history, schema_name,
                            tag="untagged", turn=0):
        """Store the request for the Retry button, then emit it to the worker.

        tag/turn correlate log entries across the single app.log timeline
        (task tag + turn number, same convention as the CUA journal).
        """
        self._last_llm_request = (user_prompt, attachments, system_prompt, history, schema_name, tag, turn)
        self._last_llm_error = False
        self.llm_request_sent.emit(user_prompt, attachments, system_prompt, history, schema_name, tag, turn)

    def _resend_last_request(self, log_line: str, notice_html: str, thinking_msg: str) -> bool:
        """Re-sends the stored last LLM request (Retry / Continue links)."""
        if not self._last_llm_request:
            logging.warning("Resend requested but no previous LLM request is stored.")
            return False
        if self.thinking_timer.isActive():
            logging.info("Resend requested while a request is in flight; ignoring.")
            return False
        logging.info(log_line)
        self.ui.add_message(notice_html, force_scroll=True)
        user_prompt, attachments, system_prompt, history, schema_name, tag, turn = self._last_llm_request
        if schema_name in ("terminal_tool_code", "browser_tool_code"):
            self._reenter_agent_mode(self.terminal_handler if schema_name == "terminal_tool_code"
                                     else self.browser_handler)
        elif schema_name is None:
            # Chat: route the response back to the chat handler.
            self._set_state(ControllerState.CHATTING)
            self.current_handler = self.chat_handler
        self.ui.show_thinking_ui(True, message=thinking_msg)
        self.thinking_time = 0
        self.thinking_timer.start(1000)
        self._last_llm_error = False
        self.llm_request_sent.emit(user_prompt, attachments, system_prompt, history, schema_name, tag, turn)
        return True

    def _reenter_agent_mode(self, handler):
        """Routes the controller back to a resumed agent (no cleanup, no cancel).

        Paused-chat detours and error recoveries leave controller.state behind
        (IDLE/CHATTING) while the handler runs. Without this, the plan
        response arrives 'in the wrong mode' and is discarded, hanging the
        task on eternal Thinking. Safe no-op when already routed.
        """
        target = {"terminal": ControllerState.TERMINAL,
                  "browser": ControllerState.BROWSER}.get(getattr(handler, "MODE_NAME", ""))
        if target is None:
            return
        if self.state == target and self.current_handler is handler:
            return
        try:
            cancel_wait = getattr(handler, "cancel_job_wait", None)
            if callable(cancel_wait):
                cancel_wait()
        except Exception:
            pass
        logging.info(f"Re-entering {target.name} mode for resumed task.")
        self._set_state(target)
        self.current_handler = handler
        self.ui.set_active_mode('terminal' if target == ControllerState.TERMINAL else 'browser')

    def ask_safety_approval(self, checks: list) -> bool:
        """Modal approval for computer-use safety checks (Vision CUA loop).

        Called on the GUI thread while the worker waits. Deny (or dialog
        failure) is the safe default: the turn is abandoned.
        """
        from PySide6.QtWidgets import QMessageBox
        try:
            lines = "\n".join(
                f"• [{c.get('code', '?')}] {c.get('message', '')}"
                for c in (checks or [])
            )
            answer = QMessageBox.question(
                self.ui,
                "Computer Use — safety check",
                "The model requested an action flagged by a safety check:\n\n"
                f"{lines}\n\nAllow once and continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return answer == QMessageBox.Yes
        except Exception as e:
            logging.error(f"Safety approval dialog failed: {e}")
            return False

    def ask_terminal_approval(self, request: dict) -> bool:
        """Modal permission prompt for a Terminal tool call.

        Runs on the GUI thread before the action executes. Deny is the safe
        default: closing the dialog or a dialog failure rejects the action.
        """
        from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel,
                                       QPlainTextEdit, QPushButton, QVBoxLayout)
        from PySide6.QtGui import QFontDatabase, QIcon
        from src.computer.terminal_policy import ACCESS_MODE_LABELS
        try:
            request = request if isinstance(request, dict) else {}
            try:
                width = int(self.ui.main_window.width())
            except Exception:
                width = 700
            width = max(540, min(width, 900))
            dialog = QDialog(self.ui)
            dialog.setWindowTitle("Logos — permission required")
            try:
                dialog.setWindowIcon(QIcon("assets/ico.ico"))
            except Exception:
                pass
            dialog.setFixedWidth(width)
            layout = QVBoxLayout(dialog)
            layout.setSpacing(10)

            summary = str(request.get("summary") or "run an action").strip()
            title = QLabel(f"Logos wants to {summary[:1].lower()}{summary[1:]}.")
            title.setWordWrap(True)
            title_font = title.font()
            title_font.setPointSizeF(title_font.pointSizeF() + 1)
            title_font.setBold(True)
            title.setFont(title_font)
            layout.addWidget(title)

            detail = str(request.get("detail") or "").strip()
            if detail:
                shown = detail if len(detail) <= 4000 else (
                    detail[:4000] + "\n… (truncated)")
                detail_box = QPlainTextEdit(shown)
                detail_box.setReadOnly(True)
                detail_box.setLineWrapMode(QPlainTextEdit.NoWrap)
                detail_box.setFont(
                    QFontDatabase.systemFont(QFontDatabase.FixedFont))
                lines = min(9, shown.count("\n") + 1)
                line_height = detail_box.fontMetrics().lineSpacing()
                detail_box.setFixedHeight(min(240, line_height * lines + 24))
                layout.addWidget(detail_box)

            reasons = [str(reason).strip()
                       for reason in (request.get("reasons") or [])
                       if str(reason).strip()]
            if reasons:
                why = QLabel("Why this needs your approval:\n" + "\n".join(
                    f"• {reason}" for reason in reasons))
                why.setWordWrap(True)
                layout.addWidget(why)

            mode_label = ACCESS_MODE_LABELS.get(
                str(request.get("mode") or ""), "")
            if mode_label:
                hint = QLabel(
                    f"Access permissions: {mode_label}. "
                    "Change this in Settings → Terminal.")
                hint.setStyleSheet("color: #9E9E9E;")
                layout.addWidget(hint)

            buttons = QHBoxLayout()
            buttons.addStretch()
            deny_button = QPushButton("Deny")
            deny_button.setDefault(True)
            allow_button = QPushButton("Allow")
            allow_button.setAutoDefault(False)
            deny_button.clicked.connect(dialog.reject)
            allow_button.clicked.connect(dialog.accept)
            buttons.addWidget(deny_button)
            buttons.addWidget(allow_button)
            layout.addLayout(buttons)
            deny_button.setFocus()
            return dialog.exec() == QDialog.Accepted
        except Exception as e:
            logging.error(f"Terminal approval dialog failed: {e}")
            return False

    def ask_user_text(self, question: str, sensitive: bool = False):
        """Modal question with optional free text (Vision ask_user tool).

        Width matches the compact window. Yes sends the typed text (or
        "да" when empty — no typing needed for plain confirmations);
        No sends the typed text (or "нет"); closing via X returns None
        (the task pauses and the user can steer via chat). Sensitive
        questions use hidden entry so the value is never echoed.
        GUI thread only.
        """
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                       QLabel, QLineEdit, QPushButton)
        from PySide6.QtGui import QIcon
        try:
            try:
                width = int(self.ui.main_window.width())
            except Exception:
                width = 700
            width = max(480, min(width, 900))
            dlg = QDialog(self.ui)
            dlg.setWindowTitle("Logos")
            try:
                dlg.setWindowIcon(QIcon("assets/ico.ico"))
            except Exception:
                pass
            dlg.setFixedWidth(width)
            layout = QVBoxLayout(dlg)
            label = QLabel(question)
            label.setWordWrap(True)
            layout.addWidget(label)
            edit = QLineEdit(dlg)
            if sensitive:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
                edit.setPlaceholderText("Секрет (скрытый ввод)…")
            else:
                edit.setPlaceholderText("Дополнение (необязательно)…")
            layout.addWidget(edit)
            row = QHBoxLayout()
            row.addStretch()
            btn_yes = QPushButton("Yes")
            btn_no = QPushButton("No")
            row.addWidget(btn_yes)
            row.addWidget(btn_no)
            layout.addLayout(row)
            result: dict = {}
            btn_yes.clicked.connect(
                lambda: (result.setdefault("answer",
                                           edit.text().strip() or "да"),
                         dlg.accept()))
            btn_no.clicked.connect(
                lambda: (result.setdefault("answer",
                                           edit.text().strip() or "нет"),
                         dlg.accept()))
            if dlg.exec():
                return result.get("answer", "да")
            return None
        except Exception as e:
            logging.error(f"ask dialog failed: {e}")
            return None

    def _handle_retry_request(self):
        """Re-send the last LLM request (Retry button on transient-error cards)."""
        self._resend_last_request(
            "Retrying last LLM request on user demand.",
            "<i>Retrying last request...</i>",
            "Retrying...",
        )

    def _handle_continue_request(self):
        """Resume the task on the new model (Continue link after a model switch)."""
        self._resend_last_request(
            "Continuing last task on the new model on user demand.",
            "<i>Continuing on the new model...</i>",
            "Continuing...",
        )

    def _can_continue(self) -> bool:
        """Whether a 'Continue' link after a model switch makes sense.

        A model switch never aborts the in-flight request (it finishes on the
        model that started it, and the new model takes over next turn), so
        nothing needs resuming while a step is running. Continue is therefore
        only offered when the last request actually stalled — it errored and
        the thinking timer is no longer running. A fresh IDLE returns False to
        avoid re-firing a completed request.
        """
        if not self._last_llm_request:
            return False
        if self.thinking_timer.isActive():
            return False
        return bool(self._last_llm_error)

    def handle_cancel_request(self):
        if self.llm_worker:
            self.llm_worker.cancel()
        self.thinking_timer.stop()
        self.ui.stop_retry_countdown()
        self.ui.show_thinking_ui(False)
        self.ui.add_message("<i>Request cancelled.</i>", force_scroll=True)

        # If we are in vision mode, just reset the vision handler to IDLE
        # but keep the controller in ASSISTING state. Otherwise, idle the active
        # agent handler so a cancelled task does not stay EXECUTING-stalled —
        # but keep the mode the user still has selected: Stop must NOT silently
        # drop a Browser/Terminal session into chat (the next message would then
        # run without tools, while the UI still shows e.g. "Browser").
        if self.state == ControllerState.VISION:
            self.vision_handler._set_state(VisionState.IDLE)
        else:
            if self.state == ControllerState.BROWSER:
                try:
                    self.browser_handler._cleanup_task()
                except Exception as e:
                    logging.warning(f"Browser cancel cleanup failed: {e}")
                self.browser_handler._set_state(VisionState.IDLE)
            elif self.state == ControllerState.TERMINAL:
                self.terminal_handler._set_state(VisionState.IDLE)
            self._set_state(self._selected_mode_state())

        # Cancelling only stops the current reply; messages queued during it
        # still run (in order) on the next turn.
        self._drain_chat_queue()

    def _selected_mode_state(self) -> ControllerState:
        """ControllerState matching the mode currently selected in the UI.

        Used after a cancel so the next message keeps going to the selected
        handler (Browser/Terminal/Vision) instead of falling through to chat.
        Plain chat maps to IDLE (chat starts from an idle controller).
        """
        mode = getattr(self.ui, "_active_mode_key", None)
        return {
            "terminal": ControllerState.TERMINAL,
            "browser": ControllerState.BROWSER,
            "vision": ControllerState.VISION,
        }.get(mode, ControllerState.IDLE)

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
        """User-friendly "Provider - model" string (without the build id).

        Used for the toolbar model button and switch notices; the build id is
        appended only for the window/title bar via ``_get_llm_window_title``.
        """
        try:
            active_provider_name = self.settings.get("active_provider")
            providers = self.settings.get("llm_providers", {})
            provider_config = providers.get(active_provider_name)

            if not provider_config:
                title = "Logos AI Assistant"
            else:
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
                    title = f"OpenRouter: {provider_display_name_sub} - {model_display_name}"
                else:
                    title = f"{provider_display_name_main} - {model_name}"

            return title
        except Exception as e:
            logging.error(f"Failed to generate LLM title string: {e}", exc_info=True)
            return "Logos AI Assistant"

    def _get_llm_window_title(self) -> str:
        """Window/title-bar text: the LLM title string plus the build id."""
        return f"{self._get_llm_title_string()} [{get_build_id()}]"

    def _on_settings_changed(self, new_settings):
        self.settings = new_settings
        self.llm_worker.update_settings_and_provider(new_settings)

        self.chat_handler.settings = new_settings
        self.vision_handler.settings = new_settings
        logging.info("Settings have been updated and providers reloaded.")

        # Live ad-blocking toggle (no browser restart needed).
        try:
            computer = getattr(self.browser_handler, "computer", None)
            mgr = getattr(computer, "browser_manager", None) if computer else None
            if mgr is not None and hasattr(mgr, "set_block_ads"):
                browser_settings = (new_settings.get("browser", {}) or {})
                mgr.set_block_ads(bool(browser_settings.get("block_ads", True)))
        except Exception as e:
            logging.warning(f"Could not propagate ad-blocking setting: {e}")

        # MCP servers follow edits saved from the settings page.
        try:
            self.refresh_mcp_servers()
        except Exception as e:
            logging.warning(f"Could not apply MCP settings: {e}")

        title_string = self._get_llm_title_string()
        window_title = self._get_llm_window_title()
        self.ui.set_main_window_title(window_title)
        self.ui.set_title_bar_text(window_title)
        self.ui.refresh_model_button(title_string)
        self.ui.set_assistant_name(self._get_current_model_name())

    def set_reasoning_effort(self, provider_id: str, model_id: str, effort: str):
        """Stores the per-model effort level and applies it to the active model."""
        from llm.reasoning_effort import levels
        if not provider_id or not model_id:
            return
        if effort not in levels(provider_id, model_id):
            logging.warning(
                "Ignoring unsupported reasoning effort '%s' for %s::%s",
                effort, provider_id, model_id)
            return
        all_settings = self.settings_manager.all()
        providers = all_settings.get("llm_providers") or {}
        target = providers.get(provider_id)
        if not isinstance(target, dict):
            return
        saved = dict(target.get("reasoning_efforts") or {})
        if saved.get(model_id) == effort:
            return
        saved[model_id] = effort
        target["reasoning_efforts"] = saved
        is_active = (all_settings.get("active_provider") == provider_id
                     and target.get("model") == model_id)
        # Persist the preference; only the active model needs a provider reload
        # (settings_changed does that for us when emit is True).
        self.settings_manager.set_many(
            {"llm_providers": providers}, save=True, emit=is_active)
        self.settings = self.settings_manager.all()
        logging.info(
            "Reasoning effort for %s::%s set to '%s' (active=%s)",
            provider_id, model_id, effort, is_active)

    def switch_model(self, provider_id: str, model_id: str, custom_notice: str | None = None):
        """Switches the active provider/model for this session only.

        The change is kept in memory (no settings.json write). Any in-flight
        request is left to finish on the model that started it; the new model
        takes over from the next turn (same behaviour as opencode). Histories
        and handler states are preserved.
        Provider tool schemas are per-mode static files loaded fresh by the
        new provider instance — no regeneration needed on switch.

        ``custom_notice`` overrides the default "Switched to ..." message
        (used when the switch is triggered automatically by a mode change).
        """
        all_settings = self.settings_manager.all()
        user_providers = all_settings.get("llm_providers", {})
        target = user_providers.get(provider_id) or {}
        if not provider_is_configured(all_settings, provider_id):
            self.ui.add_message(
                f"<i>Cannot switch to {provider_id}: provider is not configured.</i>",
                force_scroll=True,
            )
            return
        if (provider_id == all_settings.get("active_provider")
                and target.get("model") == model_id):
            return
        logging.info(f"Switching model (session-only): {all_settings.get('active_provider')} -> {provider_id} / {model_id}")
        # Never abort the in-flight request: a running reply/step finishes on
        # the model that started it, and the new model takes over on the next
        # turn (same behaviour as opencode). This avoids stranding the worker
        # on providers whose streaming cannot be cancelled (e.g. Google).
        self.settings_manager.set_many({"active_provider": provider_id}, save=False)
        target["model"] = model_id
        self.settings = self.settings_manager.all()
        self.llm_worker.update_settings_and_provider(self.settings)
        self.chat_handler.settings = self.settings
        self.vision_handler.settings = self.settings
        logging.info("Settings have been updated and providers reloaded (session-only switch).")
        title_string = self._get_llm_title_string()
        window_title = self._get_llm_window_title()
        self.ui.set_main_window_title(window_title)
        self.ui.set_title_bar_text(window_title)
        self.ui.refresh_model_button(title_string)
        self.ui.set_assistant_name(self._get_current_model_name())
        meta = self.theme_manager.get_style("COLLAPSIBLE_META")
        if custom_notice:
            notice = (f'<p style="margin: 0; color: {meta};"><i>'
                      f'{html.escape(custom_notice)}</i>')
            if self._can_continue():
                notice += CONTINUE_LINK
            notice += '</p>'
        elif self._can_continue():
            notice = (f'<p style="margin: 0; color: {meta};"><i>Switched to '
                      f'{html.escape(title_string)} (this session only). '
                      f'Context preserved.</i>{CONTINUE_LINK}</p>')
        else:
            notice = (f'<p style="margin: 0; color: {meta};"><i>Switched to '
                      f'{html.escape(title_string)} (this session only). '
                      f'The new model takes over from your next '
                      f'message.</i></p>')
        self.ui.add_message(notice, force_scroll=True, keep_stream=True)
        self._schedule_context_indicator_update()
        if self._summary_settings().get("handoff_on_model_switch", True):
            self._schedule_summary_refresh(force=True)
        self._schedule_auto_compact_check()
        # Only kick the queue when nothing is in flight; an in-flight reply
        # drains it when it finishes, and start_chat would abort that reply.
        if not self.thinking_timer.isActive():
            self._drain_chat_queue()

    def _apply_mode_default_model(self, subsection: str):
        """Session-only fallback to the per-mode default model.

        If the active model already meets the target modes' requirements it is
        left untouched (a capable model like gpt-6-luna is never swapped out).
        Only when it does not, we switch to the model the user configured for
        this mode (the Vision model), without writing settings.json.
        """
        try:
            modes = SUBSECTIONS.get(subsection, ())
            if not modes:
                return
            all_settings = self.settings_manager.all()
            user_providers = all_settings.get("llm_providers", {}) or {}
            cur_pid = all_settings.get("active_provider")
            cur_model = (user_providers.get(cur_pid) or {}).get("model")
            if cur_pid and cur_model and all(ModeCapabilities.supports(cur_pid, cur_model, m) for m in modes):
                return  # Current model fits this mode — keep it.
            entry = (all_settings.get("mode_models", {}) or {}).get(subsection) or {}
            pid = entry.get("provider")
            mid = entry.get("model")
            if not pid or not mid:
                if cur_pid and cur_model:
                    self.ui.add_message(
                        "<i>Current model does not support computer use. Pick one under "
                        "Settings → General → Vision (Computer Use).</i>",
                        force_scroll=True,
                    )
                return
            if not provider_is_configured(all_settings, pid):
                self.ui.add_message(
                    f"<i>The Vision model ({pid}) is not configured.</i>",
                    force_scroll=True,
                )
                return
            if pid == cur_pid and mid == cur_model:
                return
            notice = (
                "Так как ваша текущая модель не удовлетворяет требованиям для этого "
                "режима работы, мы переключились на модель, выбранную вами по умолчанию "
                "в настройках."
            )
            self.switch_model(pid, mid, custom_notice=notice)
        except Exception as e:
            logging.warning(f"Could not apply per-mode default model for '{subsection}': {e}", exc_info=True)

    def _cleanup_and_switch_handler(self, new_handler, new_state):
        # Leaving chat abandons any queued (not yet answered) chat messages.
        self._clear_chat_queue()

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

        # Same for the browser handler (closes the controlled browser).
        if self.current_handler == getattr(self, 'browser_handler', None):
            self.browser_handler.stop()

        mode = "chat"
        for name, handler in (
            ("terminal", getattr(self, "terminal_handler", None)),
            ("browser", getattr(self, "browser_handler", None)),
            ("vision", getattr(self, "vision_handler", None)),
        ):
            if new_handler is handler:
                mode = name
                break
        self._prepare_session_for_mode(mode)
        self._restore_agent_history()

        self._set_state(new_state)
        self.current_handler = new_handler
        self._forget_last_request()

    def _forget_last_request(self):
        """Drops the stored request on mode switch.

        Re-sending across modes is meaningless (and actively harmful: a
        stored chat request re-sent in terminal mode streams chunks into a
        handler that never asked for them). Model switch keeps it.
        """
        self._last_llm_request = None
        self._last_llm_error = False

    def _activate_chat_mode(self):
        self._cleanup_and_switch_handler(self.chat_handler, ControllerState.CHATTING)
        self.ui.set_active_mode('chat')
        self.ui.add_message("<i>Chat mode enabled.</i>", force_scroll=True)

    def _activate_terminal_mode(self):
        self._cleanup_and_switch_handler(self.terminal_handler, ControllerState.TERMINAL)
        self.ensure_mcp_manager()
        self.ui.set_active_mode('terminal')
        self.ui.add_message("<i>Terminal mode enabled. I will now use terminal commands to accomplish tasks.</i>", force_scroll=True)
        if self.indexer is not None:
            self.indexer.schedule_reindex(force=True)

    def _activate_browser_mode(self):
        self._cleanup_and_switch_handler(self.browser_handler, ControllerState.BROWSER)
        self.ui.set_active_mode('browser')
        self.ui.add_message("<i>Browser mode enabled. Describe the web task you want me to perform.</i>", force_scroll=True)

    def _vision_availability_error(self) -> str | None:
        """Returns why Vision cannot start, or None when a CUA model is usable.

        The current model is enough on its own; otherwise the dedicated Vision
        model from settings must exist, be configured and support computer use.
        """
        all_settings = self.settings_manager.all()
        user_providers = all_settings.get("llm_providers", {}) or {}
        cur_pid = all_settings.get("active_provider")
        cur_model = (user_providers.get(cur_pid) or {}).get("model")
        modes = SUBSECTIONS.get("vision", ())
        if cur_pid and cur_model and all(
                ModeCapabilities.supports(cur_pid, cur_model, m) for m in modes):
            return None
        entry = (all_settings.get("mode_models", {}) or {}).get("vision") or {}
        pid = entry.get("provider")
        mid = entry.get("model")
        if pid and mid:
            if not provider_is_configured(all_settings, pid):
                return (
                    f"The Vision model ({pid}) is not configured. Re-select it "
                    "in Settings → General → Vision (Computer Use)."
                )
            if all(ModeCapabilities.supports(pid, mid, m) for m in modes):
                return None
        return (
            "Vision mode needs a model with Computer Use support. Choose one "
            "in Settings → General → Vision (Computer Use) — OpenAI, Anthropic "
            "or Google — or switch to such a model from the model button."
        )

    def _activate_vision_mode(self):
        error = self._vision_availability_error()
        if error:
            self.ui.set_active_mode(self._current_mode())
            self.ui.add_message(f"<i>{error}</i>", force_scroll=True)
            return
        self._cleanup_and_switch_handler(self.vision_handler, ControllerState.VISION)
        self.ui.set_active_mode('vision')
        self.ui.add_message("<i>Vision mode enabled. Describe the task you want me to perform.</i>", force_scroll=True)
        self._apply_mode_default_model("vision")
        self.vision_handler._set_state(VisionState.IDLE)

    def _deactivate_vision_mode(self):
        """Switches back to chat mode. Maintained for compatibility."""
        logging.info("Deactivating vision mode and switching to chat mode.")
        self._activate_chat_mode()

    def _handle_job_stop(self, job_id: int):
        """User clicked Stop on a running shell job in the transcript."""
        handler = getattr(self, "terminal_handler", None)
        if handler is None:
            return
        try:
            handler.request_stop_job(job_id)
        except Exception as e:
            logging.warning(f"Could not stop shell job #{job_id} from UI: {e}")

    def _handle_stop(self):
        """Stops the running work but keeps the mode the user has selected.

        Stop (title-bar button) is a stronger version of the toolbar Cancel: it
        also stops the mode's agent (a Vision task, or a frozen Browser/Terminal
        session). Like Cancel it must NOT silently drop a Browser/Terminal/
        Vision session into chat — the next message should still run with the
        selected handler and its tools. So we reuse the cancel path, which idles
        the active handler and restores ``_selected_mode_state()``.
        """
        if self.state == ControllerState.VISION:
            self.vision_handler.stop()
        else:
            paused_agent = self._paused_agent()
            if paused_agent is not None:
                paused_agent.stop()
        self.handle_cancel_request()

    def _paused_agent(self):
        """Browser/terminal handler frozen in PAUSED, or None.

        PAUSED_ON_ERROR is deliberately excluded: replies to an explicit
        model question auto-continue the task instead of detouring to chat.
        Prefers the handler matching the current mode, falls back to any.
        """
        handlers = []
        if self.state == ControllerState.BROWSER:
            handlers.append(self.browser_handler)
        elif self.state == ControllerState.TERMINAL:
            handlers.append(self.terminal_handler)
        for handler in (self.browser_handler, self.terminal_handler):
            if handler not in handlers:
                handlers.append(handler)
        for handler in handlers:
            try:
                if getattr(handler, "state", None) == VisionState.PAUSED:
                    return handler
            except Exception:
                continue
        return None

    def _handle_pause_resume(self):
        logging.info(f"Pause/resume button pressed in controller state {self.state.name}.")
        paused_agent = self._paused_agent()
        if paused_agent is not None:
            paused_agent.pause_or_resume()
            return
        if self.state == ControllerState.VISION:
            self.vision_handler.pause_or_resume()
        elif self.state == ControllerState.BROWSER:
            self.browser_handler.pause_or_resume()
        elif self.state == ControllerState.TERMINAL:
            self.terminal_handler.pause_or_resume()

    def _handle_pause_for_feedback(self):
        if self.state == ControllerState.VISION:
            self.vision_handler.pause_for_feedback()

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

    def _model_context_payload(self) -> dict:
        """Components of the next request to the active model."""
        if not self.session_mode:
            return {}
        handler = self._handler_for_mode(self.session_mode)
        build = getattr(handler, "context_payload", None)
        if not callable(build):
            return {}
        try:
            return build() or {}
        except Exception as e:
            logging.debug("Could not build model context payload: %s", e, exc_info=True)
            return {}

    def _context_stats_snapshot(self, summary: str = "") -> dict:
        """Actual provider usage where available, estimates otherwise."""
        if not self.session_events:
            return {}
        provider_id, model_id = self._active_model_ids()
        if not provider_id or not model_id:
            return {}
        ratio = self._summary_settings().get(
            "summary_threshold_ratio", handoff.DEFAULT_THRESHOLD_RATIO)
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            ratio = handoff.DEFAULT_THRESHOLD_RATIO
        stats = context_stats.build_session_stats(
            self.session_events,
            provider_id,
            model_id,
            summary_text=summary,
            threshold_ratio=ratio,
            payload=self._model_context_payload(),
        )
        meta = self._compaction_meta()
        stats["compacted_events"] = handoff.compacted_event_count(
            meta, self.session_events)
        stats["compacted_at"] = str(meta.get("compacted_at") or "")
        return stats

    def _context_stats_widget(self, parent, stats: dict):
        group = QGroupBox("Context window", parent)
        box = QVBoxLayout(group)
        used = int(stats.get("context_tokens") or 0)
        source = str(stats.get("context_source") or "estimated")
        source_label = {
            "actual": "provider-reported",
            "payload": "model payload estimate",
            "estimated": "journal estimate",
        }.get(source, source)
        prefix = "" if source == "actual" else "~"
        window = int(stats.get("window") or 0)
        percent = float(stats.get("percent") or 0.0)
        threshold = float(stats.get("threshold_ratio") or 0.0) * 100.0
        threshold_tokens = int(stats.get("threshold_tokens") or 0)

        bar = QProgressBar(group)
        bar.setRange(0, 1000)
        bar.setValue(min(1000, max(0, round(float(stats.get("ratio") or 0) * 1000))))
        bar.setTextVisible(False)
        box.addWidget(bar)

        headline = QLabel(
            f"<b>{prefix}{used:,} / {window:,} tokens ({percent:.1f}%)</b> · "
            f"{source_label} · refresh threshold ~{threshold_tokens:,} ({threshold:.0f}%)",
            group,
        )
        headline.setWordWrap(True)
        box.addWidget(headline)

        counts = stats.get("counts") or {}
        box.addWidget(QLabel(
            f"Events {int(counts.get('events') or 0):,} · "
            f"messages {int(counts.get('messages') or 0):,} "
            f"(user {int(counts.get('user_messages') or 0):,}, "
            f"assistant {int(counts.get('assistant_messages') or 0):,}) · "
            f"tool calls {int(counts.get('tool_calls') or 0):,} · "
            f"results {int(counts.get('tool_results') or 0) + int(counts.get('job_results') or 0):,} · "
            f"reasoning {int(counts.get('reasoning') or 0):,} · "
            f"errors {int(counts.get('errors') or 0):,}",
            group,
        ))
        compacted_events = int(stats.get("compacted_events") or 0)
        if compacted_events:
            compacted_label = (
                f"Compacted: {compacted_events:,} of "
                f"{int(counts.get('events') or 0):,} events replaced by the "
                "summary")
            compacted_at = str(stats.get("compacted_at") or "")[:19]
            if compacted_at:
                compacted_label += f" (last {compacted_at})"
            box.addWidget(QLabel(compacted_label, group))
        auto_label = self._auto_compaction_diagnostic(window)
        if auto_label:
            box.addWidget(QLabel(auto_label, group))
        payload_tokens = int(stats.get("payload_tokens") or 0)
        if payload_tokens:
            components = stats.get("payload_components") or {}
            labels = (
                ("system", "system"),
                ("history", "history"),
                ("user", "user"),
                ("tool_schema", "tool schema"),
                ("images", "images"),
            )
            parts = [
                f"{label} ~{int(components.get(key) or 0):,}"
                for key, label in labels if int(components.get(key) or 0)
            ]
            box.addWidget(QLabel(
                f"Model payload estimate: ~{payload_tokens:,} tokens "
                f"({'; '.join(parts)})",
                group,
            ))
            delta = stats.get("payload_delta")
            if delta is not None:
                delta = int(delta)
                sign = "+" if delta >= 0 else ""
                delta_percent = (delta / payload_tokens * 100.0) if payload_tokens else 0.0
                box.addWidget(QLabel(
                    f"Provider input minus payload estimate: {sign}{delta:,} tokens "
                    f"({sign}{delta_percent:.0f}%)",
                    group,
                ))
            compact = stats.get("payload_compact") or {}
            omitted = int(compact.get("compacted_entries") or 0)
            if omitted:
                box.addWidget(QLabel(
                    f"Compaction replaced {omitted:,} earlier history "
                    "entries with the session summary.",
                    group,
                ))
            before = int(compact.get("chars_before") or 0)
            after = int(compact.get("chars_after") or 0)
            if before and after < before:
                box.addWidget(QLabel(
                    f"Agent history sent: {after:,} of {before:,} chars "
                    f"({before - after:,} saved by microcompaction; "
                    f"{int(compact.get('pruned_results') or 0)} old tool result(s) pruned)",
                    group,
                ))
        breakdown = context_stats.format_breakdown(stats)
        if breakdown:
            box.addWidget(QLabel(f"Estimated journal tokens: {breakdown}", group))
        box.addWidget(QLabel(
            f"Estimated journal text: input ~{int(stats.get('estimated_input') or 0):,} · "
            f"output ~{int(stats.get('estimated_output') or 0):,} · "
            f"summary ~{int(stats.get('summary_tokens') or 0):,}",
            group,
        ))
        usage_events = int(stats.get("usage_events") or 0)
        if usage_events:
            latest_total = int(stats.get("latest_usage_tokens") or 0)
            if latest_total:
                cache_read = int(stats.get("latest_cache_read") or 0)
                cache_write = int(stats.get("latest_cache_write") or 0)
                prompt = f"input {int(stats.get('latest_input_tokens') or 0):,}"
                cache_parts = []
                if cache_read:
                    share = stats.get("latest_cache_share")
                    share_text = (
                        f", {float(share) * 100:.0f}% of prompt" if share else "")
                    cache_parts.append(f"cache read {cache_read:,}{share_text}")
                if cache_write:
                    cache_parts.append(f"cache write {cache_write:,}")
                if cache_parts:
                    prompt += f" ({'; '.join(cache_parts)})"
                box.addWidget(QLabel(
                    f"Latest request: {prompt} · "
                    f"output {int(stats.get('latest_output') or 0):,} · "
                    f"reasoning {int(stats.get('latest_reasoning') or 0):,} · "
                    f"total {latest_total:,}",
                    group,
                ))
                latest_provider = str(stats.get("latest_provider") or "")
                latest_model = str(stats.get("latest_model") or "")
                if latest_provider and (
                        latest_provider != str(stats.get("provider_id") or "")
                        or latest_model != str(stats.get("model_id") or "")):
                    box.addWidget(QLabel(
                        f"Latest usage from: {latest_provider}::{latest_model}",
                        group,
                    ))
            session_parts = [
                f"input {int(stats.get('actual_input') or 0):,}",
                f"output {int(stats.get('actual_output') or 0):,}",
                f"reasoning {int(stats.get('actual_reasoning') or 0):,}",
            ]
            session_cache_read = int(stats.get("actual_cache_read") or 0)
            session_cache_write = int(stats.get("actual_cache_write") or 0)
            if session_cache_read:
                session_parts.append(f"cache read {session_cache_read:,}")
            if session_cache_write:
                session_parts.append(f"cache write {session_cache_write:,}")
            box.addWidget(QLabel(
                f"Session totals ({usage_events:,} provider calls): "
                + " · ".join(session_parts)
                + f" · total {int(stats.get('actual_tokens') or 0):,}",
                group,
            ))
            summary_calls = int(stats.get("summary_usage_calls") or 0)
            if summary_calls:
                box.addWidget(QLabel(
                    f"Summary calls: {summary_calls:,} · "
                    f"{int(stats.get('summary_usage_tokens') or 0):,} tokens "
                    "(not part of the current context)",
                    group,
                ))
        if stats.get("first_ts"):
            box.addWidget(QLabel(
                f"Session: {str(stats.get('first_ts'))[:19]} → "
                f"{str(stats.get('last_ts'))[:19]}",
                group,
            ))
        provider_id = str(stats.get("provider_id") or "")
        model_id = str(stats.get("model_id") or "")
        box.addWidget(QLabel(
            f"Context source: {source_label} · "
            f"{provider_id or '?'}::{model_id or '?'}",
            group,
        ))
        summary_provider, summary_model = self._summary_model_ids()
        if summary_model and (summary_provider, summary_model) != (
                provider_id, model_id):
            box.addWidget(QLabel(
                f"Summary model: {summary_provider}::{summary_model}", group))
        return group

    def _auto_compaction_diagnostic(self, window: int) -> str:
        context_cfg = self._summary_settings()
        if self.session_mode not in ("chat", "terminal", "browser"):
            return "Auto-compaction: not used in this mode"
        if not context_cfg.get("handoff_enabled", True):
            return "Auto-compaction: off (handoff is disabled in Settings → Context)"
        if not context_cfg.get("auto_compact", True):
            return "Auto-compaction: off (Settings → Context)"
        if not window:
            return "Auto-compaction: on"
        headroom = max(
            context_stats.DEFAULT_MIN_HEADROOM_TOKENS,
            int(window * context_stats.DEFAULT_HEADROOM_RATIO))
        return (f"Auto-compaction: on · starts at ≤{headroom:,} free tokens "
                "once the step settles with ≥4 new events")

    def _show_context_summary(self):
        summary = handoff.read_summary(self.session_file) if self.session_file else ""
        if not summary and self.session_events:
            summary = self._capture_handoff_summary()
        stats = self._context_stats_snapshot(summary)
        if not summary and not stats:
            QMessageBox.information(
                self.ui.main_window, "Session context",
                "No handoff summary has been recorded for this session yet.")
            return
        dialog = QDialog(self.ui.main_window)
        dialog.setWindowTitle("Session context")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        if stats:
            layout.addWidget(self._context_stats_widget(dialog, stats))
        if summary:
            browser = QTextBrowser(dialog)
            browser.setOpenExternalLinks(False)
            browser.setMarkdown(summary)
            layout.addWidget(browser)
        else:
            layout.addWidget(QLabel(
                "No handoff summary yet. It will be generated from the journal.",
                dialog,
            ))
        focus_label = QLabel("Compaction focus (optional):", dialog)
        layout.addWidget(focus_label)
        focus_input = QLineEdit(dialog)
        focus_input.setMaxLength(handoff.FOCUS_MAX_CHARS)
        focus_input.setPlaceholderText(
            "What the summary should keep in detail, e.g. the API changes "
            "and open test failures")
        layout.addWidget(focus_input)
        refresh_button = QPushButton("Compact now", dialog)

        def _request_from_dialog():
            focus = focus_input.text()
            self._request_manual_summary_refresh(focus=focus)
            dialog.accept()

        refresh_button.clicked.connect(_request_from_dialog)
        layout.addWidget(refresh_button)
        close_button = QPushButton("Close", dialog)
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        dialog.exec()

    def _request_manual_summary_refresh(self, focus: str = ""):
        focus = (focus or "").strip()[:handoff.FOCUS_MAX_CHARS]
        if not self.session_events or not self.session_file:
            QMessageBox.information(
                self.ui.main_window, "Session context",
                "There are no session events to summarize yet.")
            return
        busy = self._compaction_busy_reason()
        if busy:
            QMessageBox.information(
                self.ui.main_window, "Session context", busy)
            return
        if self._summary_pending:
            # Compaction will follow once the in-flight summary lands.
            self._manual_compact_pending = True
            self._compaction_trigger = "manual"
            self._manual_compact_focus = focus
            self.ui.add_message("<i>Compacting the session context…</i>")
            return
        meta = handoff.read_meta(self.session_file)
        covered = handoff.summary_coverage(meta, self.session_events)
        if (not focus and covered >= len(self.session_events)
                and handoff.read_summary(self.session_file)):
            self._compaction_trigger = "manual"
            if not self._apply_compaction(
                    covered, self._history_cut_key()):
                QMessageBox.warning(
                    self.ui.main_window, "Session context",
                    "Could not record the compaction boundary. "
                    "The session is unchanged.")
            return
        self._manual_compact_pending = True
        self._compaction_trigger = "manual"
        self._manual_compact_focus = focus
        self._request_summary_update(
            force=True, advance_boundary=True, trigger="manual", focus=focus)
        self.ui.add_message("<i>Compacting the session context…</i>")

    def _show_session_journal(self):
        if not self.session_events and not self.session_file:
            QMessageBox.information(
                self.ui.main_window, "Session journal",
                "This session has no recorded events yet.")
            return
        text = self._session_journal_text()
        dialog = QDialog(self.ui.main_window)
        dialog.setWindowTitle("Session journal")
        dialog.resize(760, 600)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setOpenExternalLinks(False)
        browser.setPlainText(text)
        layout.addWidget(browser)
        close_button = QPushButton("Close", dialog)
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        dialog.exec()

    def _session_journal_text(self) -> str:
        """Plain-text journal of every recorded session event."""
        lines = [
            f"Session: {os.path.basename(self.session_file or '(new)')}",
            f"Mode: {self.session_mode}",
            f"Objective: {self.session_objective or '(not set)'}",
            "",
        ]
        for event in self.session_events:
            event_type = str(event.get("type") or "event")
            stamp = str(event.get("ts") or "")
            if event_type == "message":
                detail = f"{event.get('role') or 'message'}: {event_text(event)}"
                attachments = self._journal_attachment_lines(event)
                if attachments:
                    detail += "\n" + attachments
            elif event_type == "tool_call":
                args = event.get("input") or {}
                detail = (f"{event.get('name') or 'action'} "
                          f"{json.dumps(args, ensure_ascii=False)}")
            elif event_type == "tool_result":
                detail = (f"{event.get('name') or 'tool'} "
                          f"[{event.get('status') or 'ok'}]\n{event_text(event)}")
            elif event_type == "job_result":
                detail = (f"job #{event.get('job_id', '?')} "
                          f"[{event.get('status') or 'finished'}]\n{event_text(event)}")
            elif event_type == "error":
                detail = str(event.get("message") or event_text(event))
            elif event_type == "usage":
                detail = self._usage_journal_detail(event)
            else:
                detail = event_text(event)
            lines.append(f"[{stamp}] {event_type}\n{detail}\n")
        text = "\n".join(lines).strip()
        limit = 200_000
        if len(text) > limit:
            text = text[:limit] + "\n\n… [journal truncated for display]"
        return text

    @staticmethod
    def _usage_journal_detail(event: dict) -> str:
        """Token counts of one provider call, for the Session journal."""
        parts = []
        for key, label in (
            ("input", "input"),
            ("output", "output"),
            ("reasoning", "reasoning"),
            ("cache_read", "cache read"),
            ("cache_write", "cache write"),
            ("total", "total"),
        ):
            value = event.get(key)
            if value is None:
                continue
            try:
                parts.append(f"{label} {int(value):,}")
            except (TypeError, ValueError):
                continue
        purpose = str(event.get("purpose") or "completion")
        detail = f"{purpose}: " + (" · ".join(parts) or "no token counts")
        source = "::".join(
            str(value) for value in (event.get("provider"), event.get("model"))
            if value)
        if source:
            detail += f"\nProvider: {source}"
        return detail

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
            safe_states = [VisionState.IDLE, VisionState.PAUSED_ON_ERROR]
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
