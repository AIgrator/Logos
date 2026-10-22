"""
Handles the browser mode: an iterative agent loop over Playwright tools.

Same shape as TerminalHandler (thought -> single action -> observe),
but the tools drive a real Chromium window instead of a shell.
No desktop screenshots are involved: grounding comes from the DOM snapshot.

The loop mechanics (states, single-step plan -> execute -> observe cycle,
pause/resume/stop, user communication) live in BaseAgentHandler; here only
browser specifics remain: prompts, Playwright manager, result rendering.
"""
import json
import logging
import platform
import queue
import threading
import time

from PySide6.QtCore import Slot

from src.browser.policy import BrowserCancelledError, classify_action, is_sensitive_text, redact_url
from src.computer.computer import Computer
from src.executor.executor import Executor
from states import VisionState

from .base_agent import BaseAgentHandler
from .tool_exec_worker import ToolExecRelay

_CLOSE_SENTINEL = object()
_STOP_SENTINEL = object()

# Tool results that contain page-authored text (untrusted data, not orders).
_UNTRUSTED_RESULT_TOOLS = frozenset({
    "browser_read", "browser_snapshot", "browser_find",
})


def _setting_bool(settings: dict, key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "")
    return bool(value)


def _setting_float(settings: dict, key: str, default: float) -> float:
    try:
        return float(settings.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _setting_int(settings: dict, key: str, default: int) -> int:
    try:
        return int(settings.get(key, default) or default)
    except (TypeError, ValueError):
        return default


class BrowserExecWorker:
    """Runs browser tools on a dedicated plain thread (off the GUI).

    Playwright objects are created and used ONLY here (affinity): the
    BrowserManager object is built lazily on first tool call, launched and
    closed on this same thread. Results travel back via the GUI-thread relay.

    Deliberately NOT a QThread with queued slots: Playwright's sync API
    (greenlets) crashes the process when driven from under a Qt event-loop
    dispatch on Windows — proven by repro (plain thread and QThread-subclass
    run() are clean, queued slot delivery is fatal). Work arrives through a
    plain queue; the GUI side never blocks.
    """

    def __init__(self, computer, executor, ensure_manager, relay: ToolExecRelay,
                 approval_enabled: bool = True, approval_timeout: float = 300.0):
        self.computer = computer
        self.executor = executor
        self._ensure_manager = ensure_manager
        self._relay = relay
        self.approval_enabled = bool(approval_enabled)
        self.approval_timeout = max(1.0, float(approval_timeout))
        self._cancel_event = threading.Event()
        self._generation = 0
        self._active_generation = None
        self._gen_lock = threading.Lock()
        self._approval_event = threading.Event()
        self._approval_result = False
        self._approval_ticket = None
        self._queue: queue.Queue = queue.Queue()
        self._close_done = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self):
        # (Re)creates the thread when needed: a dead threading.Thread can
        # never restart, but the queue survives, so pending work is kept.
        if self._thread is not None and self._thread.is_alive():
            return
        if self._started and (self._thread is None or not self._thread.is_alive()):
            logging.warning("Browser exec thread died; starting a fresh one.")
        self._thread = threading.Thread(
            target=self._run_loop, name="browser-exec", daemon=True)
        self._started = True
        self._thread.start()

    def dispatch(self, ticket: int, action_name: str, action_args: dict):
        self._cancel_event.clear()
        with self._gen_lock:
            generation = self._generation
        self.start()
        self._queue.put((generation, ticket, action_name, action_args))

    def cancel(self):
        self._cancel_event.set()
        with self._gen_lock:
            self._generation += 1

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def is_active_current(self) -> bool:
        """True while the running tool still belongs to the newest dispatch."""
        with self._gen_lock:
            return (self._active_generation is not None
                    and self._active_generation == self._generation)

    def _is_current(self, generation: int) -> bool:
        with self._gen_lock:
            return generation == self._generation

    def resolve_approval(self, ticket: int, approved: bool) -> None:
        """Called by the GUI thread when the approval dialog closes."""
        if ticket != self._approval_ticket:
            return
        self._approval_result = bool(approved)
        self._approval_event.set()

    def _request_approval(self, generation: int, ticket: int,
                          checks: list) -> bool:
        if not checks or not self.approval_enabled:
            return True
        if not self._is_current(generation):
            return False
        self._approval_ticket = ticket
        self._approval_result = False
        self._approval_event.clear()
        logging.info(f"BrowserExecWorker: ticket {ticket} awaiting user approval "
                     f"for {[c.get('code') for c in checks]}.")
        self._relay.browser_approval_needed.emit(ticket, checks)
        deadline = time.monotonic() + self.approval_timeout
        while time.monotonic() < deadline:
            if self._approval_event.wait(0.1):
                return self._approval_result and self._is_current(generation)
            if not self._is_current(generation):
                return False
        logging.warning(f"BrowserExecWorker: approval timed out for ticket {ticket}.")
        return False

    def _run_loop(self):
        while True:
            item = self._queue.get()
            try:
                if item is _STOP_SENTINEL:
                    return
                if item is _CLOSE_SENTINEL:
                    self._close_browser()
                    self._close_done.set()
                    continue
                generation, ticket, action_name, action_args = item
                self._run_tool(generation, ticket, action_name, action_args)
            finally:
                self._queue.task_done()

    def _run_tool(self, generation: int, ticket: int, action_name: str,
                  action_args: dict):
        if not self._is_current(generation):
            logging.info(f"BrowserExecWorker: stale ticket {ticket} ({action_name}) dropped.")
            return
        with self._gen_lock:
            self._active_generation = generation
        logging.info(f"BrowserExecWorker: ticket {ticket} running {action_name}.")
        try:
            self._ensure_manager()
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is not None:
                mgr.set_cancel_check(lambda: not self.is_active_current())
            element = {}
            try:
                if mgr is not None:
                    element = mgr.describe_ref((action_args or {}).get("ref"))
            except Exception:
                element = {}
            checks = classify_action(action_name, action_args or {}, element,
                                     getattr(mgr, "last_known_url", ""))
            if not self._request_approval(generation, ticket, checks):
                logging.info(f"BrowserExecWorker: ticket {ticket} ({action_name}) denied.")
                self._relay.browser_finished.emit(
                    ticket, False,
                    "Action denied by the user (safety check). Do not retry it. "
                    "Ask the user what they want to do instead via communicate_to_user.")
                return
            result = self.executor.execute(
                {"name": action_name, "arguments": action_args},
                {"shell_allowed": False},
            )
        except BrowserCancelledError:
            logging.info(f"BrowserExecWorker: ticket {ticket} ({action_name}) cancelled mid-call.")
            return
        except Exception as e:  # noqa: BLE001 - report back to the loop
            if self.is_active_current():
                logging.error(f"Browser tool execution error in worker: {e}", exc_info=True)
                self._relay.browser_error.emit(ticket, str(e))
            return
        finally:
            with self._gen_lock:
                if self._active_generation == generation:
                    self._active_generation = None
        if not self._is_current(generation) or self._cancel_event.is_set():
            logging.info(f"BrowserExecWorker: ticket {ticket} ({action_name}) done but cancelled.")
            return
        if result.ok:
            self._relay.browser_finished.emit(
                ticket, True, result.output_text or "(ok, no output)")
        else:
            self._relay.browser_finished.emit(ticket, False, result.error or "unknown error")

    def _close_browser(self):
        """Closes the controlled browser on the worker thread (affinity)."""
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is not None:
                logging.info("Closing controlled browser (best effort).")
                mgr.close()
        except Exception as e:
            logging.warning(f"Browser close failed: {e}")

    def shutdown(self, close_timeout: float = 3.0, join_timeout: float = 3.0) -> None:
        """Best-effort browser close + thread stop, both bounded (for app exit)."""
        self._cancel_event.set()
        self._close_done.clear()
        try:
            self._queue.put(_CLOSE_SENTINEL)
        except Exception:
            pass
        self._close_done.wait(timeout=close_timeout)
        try:
            self._queue.put(_STOP_SENTINEL)
        except Exception:
            pass
        self._thread.join(timeout=join_timeout)
        if self._thread.is_alive():
            logging.warning("Browser exec thread did not stop in time; continuing shutdown.")


class BrowserHandler(BaseAgentHandler):
    MODE_NAME = "browser"
    SCHEMA_NAME = "browser_tool_code"

    def __init__(self, controller, working_directory=None):
        super().__init__(controller)
        self.computer = Computer(mode='browser', working_directory=working_directory)
        self.os_info = f"{platform.system()} {platform.release()}"
        self.executor = Executor(self.computer, ui=None)

        # Relay first: it must live on the GUI thread so results are queued
        # back here (direct connects would run slots on the worker thread).
        browser_settings = (self.settings or {}).get("browser", {}) or {}
        self._exec_relay = ToolExecRelay(self)
        self._exec_worker = BrowserExecWorker(
            self.computer, self.executor,
            self._ensure_browser_manager, self._exec_relay,
            approval_enabled=_setting_bool(browser_settings, "require_approval", True),
            approval_timeout=_setting_float(
                browser_settings, "approval_timeout_seconds", 300.0),
        )
        self._exec_worker.start()
        self._exec_ticket = 0
        self._pending_action_name = None
        self._pending_action_args = None
        self._history_dirty = False
        self._task_started = 0.0
        self._step_count = 0

    # -- mode specifics -------------------------------------------------

    def handle_response_chunk(self, chunk):
        # Browser mode is non-streaming; chunks are ignored.
        pass

    def current_url(self) -> str:
        # Plain-string cache: live mgr.url() calls are forbidden off the
        # worker thread (Playwright affinity), so prompts read the cache.
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is not None:
                return getattr(mgr, "last_known_url", "") or "not open yet"
        except Exception:
            pass
        return "not open yet"

    def _ensure_browser_manager(self):
        """Create and attach the shared BrowserManager on first use.

        Runs ONLY on the exec thread (called from the worker): Playwright
        objects must be created where they are used.
        """
        from src.browser.manager import BrowserManager
        mgr = getattr(self.computer, "browser_manager", None)
        if mgr is not None:
            return mgr
        settings = self.settings if isinstance(self.settings, dict) else {}
        browser_settings = settings.get("browser", {}) or {}
        mgr = BrowserManager(
            headless=_setting_bool(browser_settings, "headless", False),
            profile_dir=browser_settings.get("profile_dir", "cache/browser_profile"),
            timeout_ms=_setting_int(browser_settings, "default_timeout_ms", 30000),
            block_ads=_setting_bool(browser_settings, "block_ads", True),
            allowed_domains=browser_settings.get("allowed_domains") or [],
            block_private_networks=_setting_bool(
                browser_settings, "block_private_networks", True),
        )
        self.computer.browser_manager = mgr
        # The browser_* tools also look for a factory on the Computer.
        self.computer.browser_factory = lambda: mgr
        return mgr

    def _cleanup_task(self):
        # The controlled browser (and its thread) persist across tasks;
        # only in-flight work is dropped here. The browser itself closes
        # at app shutdown (see shutdown()).
        self._exec_ticket += 1
        self._tool_in_flight = False
        self._history_dirty = True
        try:
            self._exec_worker.cancel()
        except Exception:
            pass

    def start_task(self, user_text: str):
        if self.state == VisionState.IDLE:
            # Stop/Cancel idles the handler without BaseAgentHandler.stop()
            # in the cancel path; drop that stale task's history on the next
            # fresh start instead of feeding it to an unrelated task.
            if self._history_dirty:
                self.command_history = []
                self._history_dirty = False
            self._task_started = time.monotonic()
            self._step_count = 0
            self._consecutive_errors = 0
            self._assessment_sent = False
            self._last_action_sig = None
        super().start_task(user_text)

    def _continue_loop(self, last_command_output=None, from_job_wait=False):
        if self.state == VisionState.EXECUTING and self._task_started:
            settings = (self.settings or {}).get("browser", {}) or {}
            max_steps = _setting_int(settings, "max_steps", 50)
            max_seconds = _setting_float(settings, "max_task_seconds", 1800.0)
            reason = None
            if max_steps > 0 and self._step_count >= max_steps:
                reason = (f"step budget exhausted ({self._step_count}/{max_steps} tool "
                          f"calls). Ask the user how to continue.")
            elif (max_seconds > 0
                  and time.monotonic() - self._task_started >= max_seconds):
                reason = (f"time budget exhausted ({max_seconds:.0f}s). "
                          f"Ask the user how to continue.")
            if reason:
                logging.warning(f"BrowserHandler stopping task: {reason}")
                self.ui.add_message(
                    f"<i>Browser task stopped: {reason}</i>", force_scroll=True)
                self.stop()
                return
        super()._continue_loop(last_command_output, from_job_wait)

    def shutdown(self):
        """Stops the exec thread and closes the browser (bounded, for app exit)."""
        self._exec_ticket += 1
        self._tool_in_flight = False
        try:
            worker = getattr(self, "_exec_worker", None)
            if worker is not None:
                worker.shutdown()
        except Exception as e:
            logging.warning(f"Browser shutdown failed: {e}")

    def build_prompts(self):
        system_prompt, user_prompt = self.brain.get_browser_prompts(
            user_request=self.original_user_request,
            os_info=self.os_info,
            history=self.command_history,
            current_url=self.current_url(),
        )
        return system_prompt, user_prompt

    def _request_attachments(self) -> list:
        return self._consume_pending_screenshot()

    def _consume_pending_screenshot(self) -> list:
        """Attaches the latest browser_screenshot to the request (persistent:
        peek, don't consume — navigation/tab-change already invalidate it)."""
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            peek = getattr(mgr, "peek_screenshot_path", None)
            if mgr is None or not callable(peek):
                return []
            path = peek()
            if not path:
                return []
            with open(path, "rb") as f:
                logging.info(f"Attaching browser screenshot to next request: {path}")
                return [f.read()]
        except Exception as e:
            logging.warning(f"Could not attach browser screenshot: {e}")
            return []

    def _element_for_args(self, action_args: dict) -> dict:
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is None:
                return {}
            return mgr.describe_ref((action_args or {}).get("ref"))
        except Exception:
            return {}

    def _redact_action_args(self, action_name: str, action_args: dict) -> dict:
        """Masks secrets/URL tokens before history, UI and logs see them."""
        args = dict(action_args or {})
        element = self._element_for_args(args)
        if "url" in args and isinstance(args.get("url"), str):
            args["url"] = redact_url(args["url"])
        if action_name == "browser_fill":
            text = args.get("text")
            if text is not None and is_sensitive_text(
                    args.get("description"), element.get("name"),
                    element.get("role"), element.get("type")):
                args["text"] = f"<redacted: {len(str(text))} chars>"
        if action_name == "browser_handle_dialog" and args.get("prompt_text"):
            if is_sensitive_text(args.get("description")):
                args["prompt_text"] = "<redacted>"
        return args

    def _history_action(self, action: dict) -> dict:
        redacted = self._redact_action_args(
            action.get("name"), action.get("arguments") or {})
        return {**action, "arguments": redacted}

    def execute_action(self, action: dict) -> str | None:
        """Shows the UI on the GUI thread; the tool itself runs on the exec
        thread. Returns None (async): slots continue the loop on completion."""
        action_name = action.get('name')
        action_args = action.get('arguments') or {}
        if not isinstance(action_args, dict):
            action_args = {}
        display_args = self._redact_action_args(action_name, action_args)

        self.ui.add_message(f"<b>Executing:</b> {action_name}")
        self.ui.add_message({
            'type': 'collapsible',
            'title': "Arguments",
            'content': json.dumps(display_args, indent=2, ensure_ascii=False),
        })

        self._pending_action_name = action_name
        self._pending_action_args = action_args
        self._exec_ticket += 1
        self._step_count += 1
        logging.info(f"BrowserHandler: dispatching ticket {self._exec_ticket} ({action_name}) to exec thread.")
        self._tool_in_flight = True
        self._exec_worker.dispatch(self._exec_ticket, action_name, action_args)
        return None

    @Slot(int, object)
    def _on_browser_approval(self, ticket: int, checks):
        """GUI-thread modal for worker-requested risky actions (fail closed)."""
        if ticket != self._exec_ticket:
            self._exec_worker.resolve_approval(ticket, False)
            return
        approved = False
        try:
            approved = bool(self.controller.ask_safety_approval(checks))
        except Exception as e:
            logging.error(f"Browser approval dialog failed: {e}")
        self._exec_worker.resolve_approval(ticket, approved)

    @Slot(int, bool, str)
    def _on_browser_finished(self, ticket: int, ok: bool, text: str):
        if ticket != self._exec_ticket:
            logging.info(f"Stale browser result ignored (ticket {ticket} != {self._exec_ticket}).")
            return
        self._tool_in_flight = False
        action_name = self._pending_action_name
        logging.info(f"BrowserHandler: ticket {ticket} ({action_name}) result received, continuing loop.")
        if not ok:
            error_msg = f"**Tool Result:** Error: {text}"
            self.ui.add_message(f"<i>{error_msg}</i>")
            self.record_tool_result(error_msg, status="error", name=action_name)
            self._continue_loop(error_msg)
            return
        output = text or "(ok, no output)"
        self.ui.add_message({
            'type': 'collapsible',
            'title': f"Browser Result: {action_name}",
            'content': output[-4000:],
        })
        if action_name in _UNTRUSTED_RESULT_TOOLS:
            result_text = (
                "**Tool Result:**\n<untrusted_page_content>\n"
                f"{output}\n</untrusted_page_content>")
            self.record_tool_result(result_text, name=action_name)
            self._continue_loop(result_text)
            return
        result_text = f"**Tool Result:**\n```\n{output}\n```"
        self.record_tool_result(result_text, name=action_name)
        self._continue_loop(result_text)

    @Slot(int, str)
    def _on_browser_error(self, ticket: int, error_message: str):
        if ticket != self._exec_ticket:
            logging.info(f"Stale browser error ignored (ticket {ticket} != {self._exec_ticket}).")
            return
        self._tool_in_flight = False
        logging.info(f"BrowserHandler: ticket {ticket} ({self._pending_action_name}) error received, continuing loop.")
        error_msg = f"**Tool Result:** Error: {error_message}"
        self.ui.add_message(f"<i>{error_msg}</i>")
        self.record_tool_result(
            error_msg, status="error", name=self._pending_action_name)
        self._continue_loop(error_msg)
