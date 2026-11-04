"""
The app's single controlled browser: `BrowserService` and its exec thread.

`BrowserService` owns the one Chromium instance (one profile, one login state)
on a dedicated plain thread, and both agent capabilities dispatch into it: a
terminal turn that also holds the browser capability, and the browser
presentation of the same loop (``TerminalHandler``). The loop itself, the
browser prompts and the result rendering live in ``terminal_handler.py``; this
module is only the engine and its Playwright affinity.
"""
import logging
import os
import queue
import threading
import time

from PySide6.QtCore import QObject

from src.browser.policy import (BrowserCancelledError, classify_action,
                                resolve_network_access)
from src.computer.computer import Computer
from src.executor.executor import Executor

from .tool_exec_worker import ToolExecRelay

_CLOSE_SENTINEL = object()
_STOP_SENTINEL = object()


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
    closed on this same thread. Results travel back via the GUI-thread relay
    of the handler that dispatched the call.

    Deliberately NOT a QThread with queued slots: Playwright's sync API
    (greenlets) crashes the process when driven from under a Qt event-loop
    dispatch on Windows — proven by repro (plain thread and QThread-subclass
    run() are clean, queued slot delivery is fatal). Work arrives through a
    plain queue; the GUI side never blocks.
    """

    def __init__(self, computer, executor, ensure_manager):
        self.computer = computer
        self.executor = executor
        self._ensure_manager = ensure_manager
        self.approval_enabled = True
        self.approval_timeout = 300.0
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

    def dispatch(self, ticket: int, action_name: str, action_args: dict,
                 relay: ToolExecRelay):
        self._cancel_event.clear()
        with self._gen_lock:
            generation = self._generation
        self.start()
        self._queue.put((generation, ticket, action_name, action_args, relay))

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
                          checks: list, relay: ToolExecRelay) -> bool:
        if not checks or not self.approval_enabled:
            return True
        if not self._is_current(generation):
            return False
        self._approval_ticket = ticket
        self._approval_result = False
        self._approval_event.clear()
        logging.info(f"BrowserExecWorker: ticket {ticket} awaiting user approval "
                     f"for {[c.get('code') for c in checks]}.")
        relay.browser_approval_needed.emit(ticket, checks)
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
                generation, ticket, action_name, action_args, relay = item
                self._run_tool(generation, ticket, action_name, action_args, relay)
            finally:
                self._queue.task_done()

    def _run_tool(self, generation: int, ticket: int, action_name: str,
                  action_args: dict, relay: ToolExecRelay):
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
            if not self._request_approval(generation, ticket, checks, relay):
                logging.info(f"BrowserExecWorker: ticket {ticket} ({action_name}) denied.")
                relay.browser_finished.emit(
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
                relay.browser_error.emit(ticket, str(e))
            return
        finally:
            with self._gen_lock:
                if self._active_generation == generation:
                    self._active_generation = None
        if not self._is_current(generation) or self._cancel_event.is_set():
            logging.info(f"BrowserExecWorker: ticket {ticket} ({action_name}) done but cancelled.")
            return
        if result.ok:
            relay.browser_finished.emit(
                ticket, True, result.output_text or "(ok, no output)")
        else:
            relay.browser_finished.emit(ticket, False, result.error or "unknown error")

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


class BrowserService(QObject):
    """The app's single controlled browser, shared by every capability.

    One Chromium, one profile, one thread: `BrowserExecWorker` and the
    `Computer` it drives live here, not in a handler, so switching panels or
    replacing the Terminal exec thread after a hard Stop never spawns a second
    browser. Handlers dispatch with their own GUI-thread relay, so a browser
    step from a terminal turn renders in that turn, not in the browser panel.
    """

    def __init__(self, settings_provider, working_directory=None,
                 settings_manager=None, parent=None):
        super().__init__(parent)
        self._settings_provider = settings_provider
        self._settings_manager = settings_manager
        self.computer = Computer(mode="browser",
                                 working_directory=working_directory,
                                 settings_manager=settings_manager)
        self.executor = Executor(self.computer, ui=None)
        self._worker = BrowserExecWorker(
            self.computer, self.executor, self._ensure_manager)
        self._worker.start()

    # -- ownership ------------------------------------------------------

    def manager(self):
        """The live BrowserManager, or None when no browser is open.

        GUI-thread readers (prompts, screenshot attachment, redaction) use
        this cache; Playwright objects are only touched on the worker thread.
        """
        return getattr(self.computer, "browser_manager", None)

    def dispatch(self, ticket: int, action_name: str, action_args: dict,
                 relay: ToolExecRelay):
        self._apply_approval_settings()
        self._worker.dispatch(ticket, action_name, action_args, relay)

    def cancel(self):
        """Drops queued and in-flight browser work (Stop, task cleanup)."""
        self._worker.cancel()

    def resolve_approval(self, ticket: int, approved: bool) -> None:
        self._worker.resolve_approval(ticket, approved)

    def shutdown(self, close_timeout: float = 3.0, join_timeout: float = 3.0) -> None:
        self._worker.shutdown(close_timeout, join_timeout)

    # -- worker-thread setup --------------------------------------------

    def _apply_approval_settings(self):
        settings = (self._settings_provider() or {}).get("browser", {}) or {}
        self._worker.approval_enabled = _setting_bool(
            settings, "require_approval", True)
        self._worker.approval_timeout = max(
            1.0, _setting_float(settings, "approval_timeout_seconds", 300.0))

    def _ensure_manager(self):
        """Creates and attaches the BrowserManager on first use.

        Runs ONLY on the browser thread (called from the worker): Playwright
        objects must be created where they are used.
        """
        from src.browser.manager import BrowserManager
        from src.browser.logs import resolve_log_dir
        mgr = getattr(self.computer, "browser_manager", None)
        if mgr is not None:
            return mgr
        browser_settings = (self._settings_provider() or {}).get("browser", {}) or {}
        mgr = BrowserManager(
            headless=_setting_bool(browser_settings, "headless", False),
            profile_dir=browser_settings.get("profile_dir", "cache/browser_profile"),
            timeout_ms=_setting_int(browser_settings, "default_timeout_ms", 30000),
            block_ads=_setting_bool(browser_settings, "block_ads", True),
            allowed_domains=browser_settings.get("allowed_domains") or [],
            network_access=resolve_network_access(browser_settings),
            log_dir=resolve_log_dir(
                str(getattr(self.computer, "working_directory", "") or "")),
            screenshot_dir=self._screenshot_dir(),
        )
        self.computer.browser_manager = mgr
        # The browser_* tools also look for a factory on the Computer.
        self.computer.browser_factory = lambda: mgr
        return mgr

    def _screenshot_dir(self) -> str:
        """``<project>/.logos/screenshots``, else the app's screenshot folder.

        A bare relative folder would land in the install directory, and a
        browser step inside a terminal turn shares the project's shot history.
        """
        root = str(getattr(self.computer, "working_directory", "") or "")
        if root and os.path.isdir(root):
            try:
                from src.project_manager import PROJECT_DIRNAME
            except Exception:
                PROJECT_DIRNAME = ".logos"
            return os.path.join(root, PROJECT_DIRNAME, "screenshots")
        try:
            configured = (self._settings_manager.get("APP_SCREENSHOTS_DIR")
                          if self._settings_manager is not None else "")
        except Exception:
            configured = ""
        if configured:
            return str(configured)
        return os.path.join(os.getcwd(), "screenshots")
