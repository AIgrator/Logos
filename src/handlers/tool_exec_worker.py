"""Background tool executor shared by agent modes.

Runs a single tool call (shell, file op, browser action) in the worker's own
thread so the chat window never blocks. The caller dispatches with a ticket;
results carry it back so stale (cancelled/stopped) executions are ignored.
"""
import inspect
import json
import logging
import threading
import time
import types
import typing

from PySide6.QtCore import QObject, Signal, Slot


def _declared_containers(annotation) -> set:
    """Container types a parameter annotation could accept (list/dict/tuple)."""
    if annotation is inspect.Parameter.empty:
        return set()
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        kinds = set()
        for member in typing.get_args(annotation):
            kinds |= _declared_containers(member)
        return kinds
    if origin in (list, dict, tuple):
        return {origin}
    return set()


def _decode_container(text: str, kinds: set):
    """JSON list/object written as text for a container parameter, else None."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        value = json.loads(stripped)
    except ValueError:
        return None
    if list in kinds and isinstance(value, list):
        return value
    if tuple in kinds and isinstance(value, list):
        return value
    if dict in kinds and isinstance(value, dict):
        return value
    return None


def coerce_arguments(func, args: dict) -> dict:
    """Matches stringified scalar arguments to the types the tool declares.

    Weak models sometimes send every value as a string ("false", "null", "5").
    Passing those straight through breaks the tool: a non-empty "false" is
    truthy, so a foreground command runs in the background, and "null" becomes
    a literal folder name. Only a value whose declared parameter type disagrees
    with the string is converted, so genuine string arguments (a command, a
    path, file content) are never touched.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return args
    coerced = dict(args)
    for key, value in args.items():
        parameter = parameters.get(key)
        if parameter is None or not isinstance(value, str):
            continue
        annotation = parameter.annotation
        if annotation is inspect.Parameter.empty:
            continue
        origin = typing.get_origin(annotation)
        allowed = typing.get_args(annotation) if origin in (typing.Union, types.UnionType) else (annotation,)
        text = value.strip()
        lowered = text.lower()
        containers = _declared_containers(annotation)
        decoded = _decode_container(text, containers) if containers else None
        if decoded is not None:
            # "[\"a\", \"b\"]" sent as one string reached the tool as an
            # iterable of characters (read_files tried to open '[' and '"').
            coerced[key] = decoded
            logging.info(
                f"Decoded JSON-encoded tool argument '{key}' "
                f"({type(decoded).__name__} of {len(decoded)} item(s)).")
            continue
        if containers & {list, tuple} and text and lowered not in (
                "null", "none"):
            # A bare value for a list parameter ("index.html") means one item;
            # iterating the string would read one character per path.
            coerced[key] = [value]
            logging.info(
                f"Wrapped scalar tool argument '{key}' into a one-item list.")
            continue
        if lowered in ("null", "none", ""):
            # "null" written as text means "not set". A non-optional parameter
            # falls back to its declared default instead of receiving the word.
            if type(None) in allowed:
                coerced[key] = None
            elif parameter.default is not inspect.Parameter.empty:
                del coerced[key]
            else:
                continue
        elif bool in allowed and lowered in ("true", "false"):
            coerced[key] = lowered == "true"
        elif int in allowed and text.lstrip("-").isdigit():
            coerced[key] = int(text)
        elif float in allowed and int not in allowed:
            try:
                coerced[key] = float(text)
            except ValueError:
                continue
        else:
            continue
        if key in coerced:
            logging.info(f"Coerced tool argument '{key}' from string {value!r} to {coerced[key]!r}.")
        else:
            logging.info(f"Dropped tool argument '{key}' ({value!r}); the tool's default applies.")
    return coerced


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
    browser_finished = Signal(int, bool, str)  # ticket, ok, output-or-error text
    browser_error = Signal(int, str)           # ticket, error_message
    browser_approval_needed = Signal(int, object)  # ticket, checks (list of dicts)

    def __init__(self, handler):
        super().__init__()
        self._handler = handler
        self.finished.connect(self._forward_finished)
        self.error.connect(self._forward_error)
        self.progress.connect(self._forward_progress)
        self.web_finished.connect(self._forward_web_finished)
        self.web_error.connect(self._forward_web_error)
        self.web_plan_a_failed.connect(self._forward_plan_a_failed)
        self.browser_finished.connect(self._forward_browser_finished)
        self.browser_error.connect(self._forward_browser_error)
        self.browser_approval_needed.connect(self._forward_browser_approval)

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

    @Slot(int, bool, str)
    def _forward_browser_finished(self, ticket, ok, text):
        self._handler._on_browser_finished(ticket, ok, text)

    @Slot(int, str)
    def _forward_browser_error(self, ticket, error_message):
        self._handler._on_browser_error(ticket, error_message)

    @Slot(int, object)
    def _forward_browser_approval(self, ticket, checks):
        self._handler._on_browser_approval(ticket, checks)


class ToolExecWorker(QObject):
    dispatch = Signal(int, str, dict)  # ticket, action_name, action_args

    def __init__(self, computer, relay: ToolExecRelay):
        super().__init__()
        self.computer = computer
        self._relay = relay
        self._cancelled = False
        self._min_ticket = 0
        self.dispatch.connect(self.run_tool)

    @Slot(int, str, dict)
    def run_tool(self, ticket: int, action_name: str, action_args: dict):
        if self._cancelled or ticket < self._min_ticket:
            logging.info(f"ToolExecWorker: ticket {ticket} ({action_name}) dropped, cancelled.")
            if self._cancelled and ticket >= self._min_ticket:
                # Never leave the handler with _tool_in_flight stuck: report
                # the drop back so the loop can move on instead of hanging.
                self._relay.error.emit(
                    ticket, "Tool execution was cancelled before it started.")
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
            result = action_func(**coerce_arguments(action_func, action_args))
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

    def reset(self, min_ticket: int = 0):
        """Re-arms the worker for a new task and drops older queued tickets."""
        self._min_ticket = max(self._min_ticket, min_ticket)
        self._cancelled = False


def _summarize(result, limit: int = 200) -> str:
    try:
        text = str(result)
    except Exception:
        return f"<unprintable {type(result).__name__}>"
    text = text.replace("\n", " ")
    if len(text) > limit:
        return f"{text[:limit]}... (+{len(text) - limit} chars)"
    return text
