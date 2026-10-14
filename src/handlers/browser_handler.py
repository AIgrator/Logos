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

from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, Signal, Slot

from src.computer.computer import Computer
from src.executor.executor import Executor

from .base_agent import BaseAgentHandler
from .tool_exec_worker import ToolExecRelay


class BrowserExecWorker(QObject):
    """Runs browser tools on the handler's dedicated thread (off the GUI).

    Playwright objects are created and used ONLY here (affinity): the
    BrowserManager object is built lazily on first tool call, launched and
    closed on this same thread. Results travel back via the GUI-thread relay.
    """

    dispatch = Signal(int, str, dict)  # ticket, action_name, action_args

    def __init__(self, computer, executor, ensure_manager, relay: ToolExecRelay):
        super().__init__()
        self.computer = computer
        self.executor = executor
        self._ensure_manager = ensure_manager
        self._relay = relay
        self._cancelled = False
        self.dispatch.connect(self.run_tool)

    @Slot(str, dict, int)
    def run_tool(self, ticket: int, action_name: str, action_args: dict):
        if self._cancelled:
            logging.info(f"BrowserExecWorker: ticket {ticket} ({action_name}) dropped, cancelled.")
            return
        logging.info(f"BrowserExecWorker: ticket {ticket} running {action_name}.")
        try:
            self._ensure_manager()
            result = self.executor.execute(
                {"name": action_name, "arguments": action_args},
                {"shell_allowed": False},
            )
        except Exception as e:  # noqa: BLE001 - report back to the loop
            if not self._cancelled:
                logging.error(f"Browser tool execution error in worker: {e}", exc_info=True)
                self._relay.browser_error.emit(ticket, str(e))
            return
        if self._cancelled:
            logging.info(f"BrowserExecWorker: ticket {ticket} ({action_name}) done but cancelled.")
            return
        if result.ok:
            self._relay.browser_finished.emit(
                ticket, True, result.output_text or "(ok, no output)")
        else:
            self._relay.browser_finished.emit(ticket, False, result.error or "unknown error")

    @Slot()
    def close_browser(self):
        """Closes the controlled browser. Runs on the worker thread via
        blocking invoke (Playwright affinity); safe no-op when never started."""
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is not None:
                logging.info("Closing controlled browser (best effort).")
                mgr.close()
        except Exception as e:
            logging.warning(f"Browser close failed: {e}")

    def cancel(self):
        self._cancelled = True


class BrowserHandler(BaseAgentHandler):
    MODE_NAME = "browser"
    SCHEMA_NAME = "browser_tool_code"

    def __init__(self, controller, working_directory=None):
        super().__init__(controller)
        self.computer = Computer(mode='browser', working_directory=working_directory)
        self.os_info = f"{platform.system()} {platform.release()}"
        self.executor = Executor(self.computer, ui=None)

        self._exec_thread = QThread()
        # Relay first: it must live on the GUI thread so results are queued
        # back here (direct connects would run slots on the worker thread).
        self._exec_relay = ToolExecRelay(self)
        self._exec_worker = BrowserExecWorker(
            self.computer, self.executor,
            self._ensure_browser_manager, self._exec_relay)
        self._exec_worker.moveToThread(self._exec_thread)
        self._exec_thread.start()
        self._exec_ticket = 0
        self._pending_action_name = None
        self._pending_action_args = None

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
            headless=bool(browser_settings.get("headless", False)),
            profile_dir=browser_settings.get("profile_dir", "cache/browser_profile"),
            timeout_ms=int(browser_settings.get("default_timeout_ms", 30000) or 30000),
            block_ads=bool(browser_settings.get("block_ads", True)),
        )
        self.computer.browser_manager = mgr
        # The browser_* tools also look for a factory on the Computer.
        self.computer.browser_factory = lambda: mgr
        return mgr

    def _ensure_exec_thread(self):
        if not self._exec_thread.isRunning():
            self._exec_thread.start()

    def _cleanup_task(self):
        # The controlled browser persists across tasks (relaunching Chromium
        # per task is slow); only in-flight work is dropped here. The browser
        # itself closes at app shutdown (see shutdown()).
        self._exec_ticket += 1
        self._tool_in_flight = False
        if self._exec_thread and self._exec_thread.isRunning():
            try:
                self._exec_worker.cancel()
            except Exception:
                pass
            self._exec_thread.quit()
            if not self._exec_thread.wait(2000):
                logging.warning("Browser exec thread did not stop in 2000ms.")

    def shutdown(self):
        """Stops the exec thread and closes the browser (bounded, for app exit)."""
        self._exec_ticket += 1
        self._tool_in_flight = False
        thread = getattr(self, "_exec_thread", None)
        worker = getattr(self, "_exec_worker", None)
        if thread is not None and thread.isRunning() and worker is not None:
            try:
                QMetaObject.invokeMethod(worker, "close_browser", Qt.BlockingQueuedConnection)
            except Exception as e:
                logging.warning(f"Browser close invoke failed: {e}")
            try:
                thread.quit()
            except Exception:
                pass
            try:
                if not thread.wait(3000):
                    logging.warning("Browser exec thread did not stop in 3000ms.")
            except Exception as e:
                logging.warning(f"Browser exec thread wait failed: {e}")

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
        """Attaches the latest browser_screenshot (once) to the next request."""
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is None or not hasattr(mgr, "consume_screenshot_path"):
                return []
            path = mgr.consume_screenshot_path()
            if not path:
                return []
            with open(path, "rb") as f:
                logging.info(f"Attaching browser screenshot to next request: {path}")
                return [f.read()]
        except Exception as e:
            logging.warning(f"Could not attach browser screenshot: {e}")
            return []

    def execute_action(self, action: dict) -> str | None:
        """Shows the UI on the GUI thread; the tool itself runs on the exec
        thread. Returns None (async): slots continue the loop on completion."""
        action_name = action.get('name')
        action_args = action.get('arguments') or {}
        if not isinstance(action_args, dict):
            action_args = {}

        self.ui.add_message(f"<b>Executing:</b> {action_name}")
        self.ui.add_message({
            'type': 'collapsible',
            'title': "Arguments",
            'content': json.dumps(action_args, indent=2, ensure_ascii=False),
        })

        self._ensure_exec_thread()
        self._pending_action_name = action_name
        self._pending_action_args = action_args
        self._exec_ticket += 1
        logging.info(f"BrowserHandler: dispatching ticket {self._exec_ticket} ({action_name}) to exec thread.")
        self._tool_in_flight = True
        self._exec_worker.dispatch.emit(self._exec_ticket, action_name, action_args)
        return None

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
            self._continue_loop(error_msg)
            return
        output = text or "(ok, no output)"
        self.ui.add_message({
            'type': 'collapsible',
            'title': f"Browser Result: {action_name}",
            'content': output[-4000:],
        })
        self._continue_loop(f"**Tool Result:**\n```\n{output}\n```")

    @Slot(int, str)
    def _on_browser_error(self, ticket: int, error_message: str):
        if ticket != self._exec_ticket:
            logging.info(f"Stale browser error ignored (ticket {ticket} != {self._exec_ticket}).")
            return
        self._tool_in_flight = False
        logging.info(f"BrowserHandler: ticket {ticket} ({self._pending_action_name}) error received, continuing loop.")
        error_msg = f"**Tool Result:** Error: {error_message}"
        self.ui.add_message(f"<i>{error_msg}</i>")
        self._continue_loop(error_msg)
