"""Background tool executor shared by agent modes.

Runs a single tool call (shell, file op, browser action) in the worker's own
thread so the chat window never blocks. The caller dispatches with a ticket;
results carry it back so stale (cancelled/stopped) executions are ignored.
"""
import logging
import threading
import time

from PySide6.QtCore import QObject, Signal, Slot


class ToolExecRelay(QObject):
    """GUI-thread relay for worker results.

    Workers emit THESE signals (never signals connected straight to the
    handler): a signal connected to a non-QObject callable is always
    delivered DIRECTLY in the emitter's thread, so handler slots ran on
    the worker thread and touched Qt from there — an access violation
    inside _calculate_content_height killed the app. The relay's own
    slots are real QObject slots, so delivery from worker threads is
    queued to the GUI thread, where they forward to the handler.
    Must be created on the GUI thread.
    """
    finished = Signal(int, object)     # ticket, result
    error = Signal(int, str)           # ticket, error_message
    progress = Signal(int, float, str)  # ticket, elapsed_seconds, last_output_line
    web_finished = Signal(str, str, dict)  # action_name, result, original_args
    web_error = Signal(str, str, dict)     # action_name, error_message, original_args
    web_plan_a_failed = Signal(str)        # provider_display_name

    def __init__(self, handler):
        super().__init__()
        self._handler = handler
        self.finished.connect(self._forward_finished)
        self.error.connect(self._forward_error)
        self.progress.connect(self._forward_progress)
        self.web_finished.connect(self._forward_web_finished)
        self.web_error.connect(self._forward_web_error)
        self.web_plan_a_failed.connect(self._forward_plan_a_failed)

    @Slot(int, object)
    def _forward_finished(self, ticket, result):
        self._handler._on_tool_finished(ticket, result)

    @Slot(int, str)
    def _forward_error(self, ticket, error_message):
        self._handler._on_tool_error(ticket, error_message)

    @Slot(int, float, str)
    def _forward_progress(self, ticket, elapsed, last_line):
        self._handler._on_tool_progress(ticket, elapsed, last_line)

    @Slot(str, str, dict)
    def _forward_web_finished(self, action_name, result, original_args):
        self._handler._on_web_request_complete(action_name, result, original_args)

    @Slot(str, str, dict)
    def _forward_web_error(self, action_name, error_message, original_args):
        self._handler._on_web_request_error(action_name, error_message, original_args)

    @Slot(str)
    def _forward_plan_a_failed(self, provider_name):
        self._handler._on_plan_a_failed(provider_name)


class ToolExecWorker(QObject):
    dispatch = Signal(int, str, dict)  # ticket, action_name, action_args

    def __init__(self, computer, relay: ToolExecRelay):
        super().__init__()
        self.computer = computer
        self._relay = relay
        self._cancelled = False
        self.dispatch.connect(self.run_tool)

    @Slot(int, str, dict)
    def run_tool(self, ticket: int, action_name: str, action_args: dict):
        if self._cancelled:
            logging.info(f"ToolExecWorker: ticket {ticket} ({action_name}) dropped, cancelled.")
            return
        logging.info(f"ToolExecWorker: ticket {ticket} running {action_name} "
                     f"on thread '{threading.current_thread().name}'.")
        # Live progress from streaming tools (throttled here so a chatty
        # tool cannot flood the GUI thread).
        last_emit = [0.0]

        def _progress(elapsed: float, line: str):
            now = time.monotonic()
            if now - last_emit[0] >= 0.5:
                last_emit[0] = now
                try:
                    self._relay.progress.emit(ticket, elapsed, (line or "")[:300])
                except Exception:
                    pass

        prev_cb = getattr(self.computer, "progress_callback", None)
        try:
            self.computer.progress_callback = _progress
        except Exception:
            pass
        try:
            if not hasattr(self.computer, action_name):
                raise AttributeError(f"Computer has no tool named '{action_name}'")
            action_func = getattr(self.computer, action_name)
            result = action_func(**action_args)
        except Exception as e:  # noqa: BLE001 - report back to the loop
            if not self._cancelled:
                logging.error(f"Tool execution error in worker: {e}", exc_info=True)
                self._relay.error.emit(ticket, str(e))
            return
        finally:
            try:
                if getattr(self.computer, "progress_callback", None) is _progress:
                    self.computer.progress_callback = prev_cb
            except Exception:
                pass
        if not self._cancelled:
            logging.info(f"ToolExecWorker: ticket {ticket} ({action_name}) done, "
                         f"result={_summarize(result)}.")
            self._relay.finished.emit(ticket, result)
        else:
            logging.info(f"ToolExecWorker: ticket {ticket} ({action_name}) done but cancelled.")

    def cancel(self):
        self._cancelled = True


def _summarize(result, limit: int = 200) -> str:
    try:
        text = str(result)
    except Exception:
        return f"<unprintable {type(result).__name__}>"
    text = text.replace("\n", " ")
    if len(text) > limit:
        return f"{text[:limit]}... (+{len(text) - limit} chars)"
    return text
