"""Background tool executor shared by agent modes.

Runs a single tool call (shell, file op, browser action) in the worker's own
thread so the chat window never blocks. The caller dispatches with a ticket;
results carry it back so stale (cancelled/stopped) executions are ignored.
"""
import logging
import threading

from PySide6.QtCore import QObject, Signal, Slot


class ToolExecWorker(QObject):
    dispatch = Signal(int, str, dict)  # ticket, action_name, action_args
    finished = Signal(int, object)     # ticket, result
    error = Signal(int, str)           # ticket, error_message

    def __init__(self, computer):
        super().__init__()
        self.computer = computer
        self._cancelled = False
        self.dispatch.connect(self.run_tool)

    @Slot(int, str, dict)
    def run_tool(self, ticket: int, action_name: str, action_args: dict):
        if self._cancelled:
            logging.info(f"ToolExecWorker: ticket {ticket} ({action_name}) dropped, cancelled.")
            return
        logging.info(f"ToolExecWorker: ticket {ticket} running {action_name} "
                     f"on thread '{threading.current_thread().name}'.")
        try:
            if not hasattr(self.computer, action_name):
                raise AttributeError(f"Computer has no tool named '{action_name}'")
            action_func = getattr(self.computer, action_name)
            result = action_func(**action_args)
        except Exception as e:  # noqa: BLE001 - report back to the loop
            if not self._cancelled:
                logging.error(f"Tool execution error in worker: {e}", exc_info=True)
                self.error.emit(ticket, str(e))
            return
        if not self._cancelled:
            logging.info(f"ToolExecWorker: ticket {ticket} ({action_name}) done, "
                         f"result={_summarize(result)}.")
            self.finished.emit(ticket, result)
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
