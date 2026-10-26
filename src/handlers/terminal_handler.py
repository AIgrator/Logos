"""
Handles the logic for the terminal mode based on a stateful agent architecture.
The agent operates in a continuous loop, analyzing the state and executing commands
until the user's goal is achieved.
"""
import logging
import platform
import html
import json
import re
from PySide6.QtCore import QObject, Slot, QThread, QTimer, Signal

from states import VisionState
from ui.terminal_html_formatter import TerminalHtmlFormatter
from src.computer.computer import Computer

from .base_agent import BaseAgentHandler
from .tool_exec_worker import ToolExecRelay, ToolExecWorker

# Background-job await: after a launch, give the job a short startup window
# so quick failures are reported without an extra model turn. If it is still
# running after that (a server/daemon or a long build), hand control back:
# waiting for a server to exit would block the task for minutes. The model
# can then poll, or verify a server with an HTTP request.
JOB_WAIT_INTERVAL_MS = 5000
JOB_WAIT_MAX_ROUNDS = 2

_RUNNING_JOB_HINT = (
    "If this is a server/daemon, verify it with an HTTP request "
    "(e.g. `curl -I http://127.0.0.1:PORT/`) instead of waiting for it to exit. "
    "Use `stop_shell_job` when done."
)


def _running_job_id(result) -> int | None:
    """Job id when a tool result is a live background handle, else None."""
    if isinstance(result, dict) and result.get('status') == 'running' and 'job_id' in result:
        try:
            return int(result['job_id'])
        except Exception:
            return None
    return None


class JobEventRelay(QObject):
    """Queues ShellJobManager callbacks from worker threads onto the GUI thread."""

    event_received = Signal(dict)

    def publish(self, event: dict):
        self.event_received.emit(event)


def _shorten_text(value, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _shorten_command(value, limit: int = 120) -> str:
    """Keep both the executable and the trailing arguments visible."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    tail = min(50, limit // 3)
    head = max(40, limit - tail - 3)
    return text[:head].rstrip() + " … " + text[-tail:].lstrip()


def _action_summary(action_name: str, action_args: dict) -> str:
    args = action_args if isinstance(action_args, dict) else {}
    name = action_name or "action"

    if name == 'execute_shell':
        command = _shorten_command(args.get('command'), 120)
        return f"Command · {command}" if command else "Command"
    if name == 'read_files':
        paths = args.get('paths')
        count = len(paths) if isinstance(paths, list) else 0
        return f"Reading {count} file(s)" if count else "Reading files"
    if name == 'search_file_content':
        pattern = _shorten_text(args.get('pattern'))
        return f"Searching for '{pattern}'" if pattern else "Searching files"
    if name == 'list_directory':
        return "Listing directory"
    if name == 'write_file':
        return "Writing file"
    if name == 'replace':
        return "Editing file"
    if name == 'replace_many':
        replacements = args.get('replacements')
        count = len(replacements) if isinstance(replacements, list) else 0
        return f"Editing file ({count} replacements)" if count else "Editing file"
    if name == 'delete_files':
        paths = args.get('paths')
        count = len(paths) if isinstance(paths, list) else 0
        return f"Deleting {count} file(s)" if count else "Deleting files"
    if name == 'move_files':
        return "Moving files"
    if name == 'copy_files':
        return "Copying files"
    if name == 'web_search':
        query = _shorten_text(args.get('query'))
        return f"Searching the web for '{query}'" if query else "Searching the web"
    if name == 'web_fetchs':
        urls = args.get('urls')
        count = len(urls) if isinstance(urls, list) else 0
        return f"Opening {count} page(s)" if count else "Opening pages"
    if name == 'wait_seconds':
        seconds = args.get('seconds', '?')
        return f"Waiting {seconds}s"
    if name in {'poll_shell_job', 'stop_shell_job'}:
        job_id = args.get('job_id', '?')
        verb = "Check" if name == 'poll_shell_job' else "Stop"
        return f"{verb} shell job · #{job_id}"
    return f"Running {name.replace('_', ' ')}"


def _details_title(action_name: str) -> str:
    if action_name == 'execute_shell':
        return "Command details"
    if action_name in {'write_file', 'replace', 'replace_many'}:
        return "File operation details"
    if action_name == 'read_files':
        return "Files to read"
    if action_name == 'list_directory':
        return "Directory details"
    if action_name == 'search_file_content':
        return "Search details"
    return f"Details · {action_name}"


class WebTaskThread(QThread):
    """ Thread to handle blocking web requests."""

    def __init__(self, computer, provider, action_name, action_args, relay: ToolExecRelay, parent=None):
        super().__init__(parent)
        self.computer = computer
        self.provider = provider
        self.action_name = action_name
        self.action_args = action_args
        self._relay = relay
        self._is_cancelled = False

    def run(self):
        """Executes the web request logic in a background thread."""
        try:
            if self._is_cancelled:
                return

            final_response = None

            if self.action_name == 'web_fetchs':
                urls = self.action_args.get('urls')
                if not urls:
                    raise ValueError("Action 'web_fetchs' is missing the 'urls' argument.")
                logging.info("Executing local web_fetchs.")
                final_response = self.computer.web_fetchs(urls=urls)

            elif self.action_name == 'web_search':
                query = self.action_args.get('query')
                if not query:
                    raise ValueError("Action 'web_search' is missing the 'query' argument.")

                provider_id = self.provider.get_provider_id()
                provider_name = self.provider.get_display_name()

                # --- PLAN A: Native Provider Search ---
                try:
                    logging.info(f"Plan A: attempting native web search via provider '{provider_id}' for query: {query!r}")
                    final_response = self.provider.web_search(query=query)
                    if final_response:
                        logging.info(f"Plan A: native search via '{provider_id}' succeeded ({len(final_response)} chars).")
                    else:
                        logging.info(f"Plan A: provider '{provider_id}' returned no native result; trying local fallback.")
                except Exception as e:
                    logging.warning(f"Plan A (native search) from '{provider_name}' failed: {e}. Proceeding to Plan B.", exc_info=True)
                    self._relay.web_plan_a_failed.emit(provider_name)
                    final_response = None

                if self._is_cancelled:
                    return

                # --- PLAN B: Local Execution (if Plan A failed) ---
                if final_response is None:
                    logging.info(f"Plan B: using local web search for query: {query!r}")
                    final_response = self.computer.web_search(query=query)
                    logging.info(f"Plan B: local search returned {len(final_response) if final_response else 0} chars.")

            if not self._is_cancelled:
                self._relay.web_finished.emit(self.action_name, final_response, self.action_args)

        except Exception as e:
            if not self._is_cancelled:
                logging.error(f"WebTaskThread error for {self.action_name}: {e}", exc_info=True)
                self._relay.web_error.emit(self.action_name, str(e), self.action_args)

    def cancel(self):
        """Flags the thread to be cancelled."""
        self._is_cancelled = True


class TerminalHandler(BaseAgentHandler):
    MODE_NAME = "terminal"
    SCHEMA_NAME = "terminal_tool_code"
    PROMPTS_SUBDIR = "terminal"

    def __init__(self, controller, theme_manager, working_directory=None):
        super().__init__(controller)
        self.computer = Computer(mode='terminal', working_directory=working_directory,
                                 settings_manager=getattr(controller, 'settings_manager', None))
        self.theme_manager = theme_manager
        self.html_formatter = TerminalHtmlFormatter(self.theme_manager)
        self.os_info = f"{platform.system()} {platform.release()}"
        self.web_thread = None
        self.active_edit_path = None

        self._exec_thread = QThread()
        # Relay first: it must live on the GUI thread so worker results
        # are queued back here (direct connects would run slots on the
        # worker thread and crash Qt — proven by faulthandler).
        self._exec_relay = ToolExecRelay(self)
        self._exec_worker = ToolExecWorker(self.computer, self._exec_relay)
        self._exec_worker.moveToThread(self._exec_thread)
        self._exec_thread.start()
        self._exec_ticket = 0
        self._exec_thread_restart_pending = False
        self._abandoned_exec_workers = []
        self._pending_action_name = None
        self._pending_action_args = None
        self._pending_action_label = None
        self._job_commands = {}
        self._job_cwds = {}
        self._handled_job_ids = set()

        self._job_event_relay = JobEventRelay()
        self._job_event_relay.event_received.connect(self._on_job_event)
        manager = getattr(self.computer, "shell_jobs", None)
        if manager is not None:
            manager.add_listener(self._job_event_relay.publish)

        self._job_wait_timer = QTimer()
        self._job_wait_timer.setSingleShot(True)
        self._job_wait_timer.timeout.connect(self._on_job_wait_tick)
        self._awaiting_job_id = None
        self._job_wait_rounds = 0

        # Connect to theme changes to update the formatter
        self.theme_manager.theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self):
        """Re-initializes the HTML formatter with the new theme."""
        self.html_formatter = TerminalHtmlFormatter(self.theme_manager)

    def build_prompts(self):
        # Live jobs are appended transiently (never stored): every planning
        # turn — including guidance-steered ones — sees what is already
        # running, so the model polls instead of relaunching it.
        history = self.model_history()
        try:
            manager = getattr(self.computer, "shell_jobs", None)
            note = manager.describe_live() if manager is not None else ""
            if note:
                history = history + [note]
        except Exception:
            pass
        return self.brain.get_terminal_prompts(
            user_request=self.original_user_request,
            os_info=self.os_info,
            history=history
        )

    def _dedent_lines(self, lines: list[str]) -> list[str]:
        """
        Removes the common leading whitespace from a list of strings.
        """
        non_empty_lines = [line for line in lines if line.strip()]
        if not non_empty_lines:
            return lines

        # Find the shortest leading whitespace string
        indentations = []
        for line in non_empty_lines:
            i = 0
            while i < len(line) and line[i].isspace():
                i += 1
            indentations.append(line[:i])

        if not indentations:
            return lines

        # Find the common prefix of all indentation strings
        common_prefix = indentations[0]
        for indent in indentations[1:]:
            max_len = min(len(common_prefix), len(indent))
            i = 0
            while i < max_len and common_prefix[i] == indent[i]:
                i += 1
            common_prefix = common_prefix[:i]
            if not common_prefix:
                break

        if not common_prefix:
            return lines

        prefix_len = len(common_prefix)
        return [line[prefix_len:] if line.startswith(common_prefix) else line for line in lines]

    def _format_diff_text(self, old_lines: list[str], new_lines: list[str], start_line: int) -> str:
        rows = []
        for i, line in enumerate(old_lines):
            prefix = f"{start_line + i:>4}" if start_line != -1 else "    "
            rows.append(f"{prefix} - {line}")
        for i, line in enumerate(new_lines):
            prefix = f"{start_line + i:>4}" if start_line != -1 else "    "
            rows.append(f"{prefix} + {line}")
        return "\n".join(rows)

    def _theme(self, key: str) -> str:
        return self.theme_manager.get_style(key)

    def _action_summary_html(self, label: str) -> str:
        return (
            f'<div style="background-color: {self._theme("ACTION_SUMMARY_BG")}; '
            f'border-left: 3px solid {self._theme("ACTION_SUMMARY_BORDER")}; '
            'padding: 6px 9px; margin: 6px 0 2px 0;">'
            f'<span style="color: {self._theme("ACTION_SUMMARY_COLOR")}; '
            f'font-weight: 600;">{html.escape(label)}</span></div>'
        )

    def _details_block(self, action_name: str, action_args: dict) -> dict:
        if action_name == 'execute_shell' and action_args.get('command'):
            command = str(action_args.get('command'))
            extras = {k: v for k, v in action_args.items() if k != 'command'}
            html_parts = [self.html_formatter.format_code_content(command, 'command.sh')]
            plain_parts = [f"$ {command}"]
            if extras:
                labels = {
                    'cwd': 'Working directory',
                    'timeout': 'Timeout',
                    'run_in_background': 'Run in background',
                }
                meta_lines = []
                for key, value in extras.items():
                    label = labels.get(key, key.replace('_', ' ').title())
                    if isinstance(value, bool):
                        value = 'yes' if value else 'no'
                    meta_lines.append(f"{label}: {value}")
                meta_text = "\n".join(meta_lines)
                html_parts.append(
                    f'<div style="color: {self._theme("COLLAPSIBLE_META")}; '
                    f'margin-top: 6px; white-space: pre-wrap;">'
                    f'{html.escape(meta_text)}</div>'
                )
                plain_parts.append(meta_text)
            return {
                'type': 'collapsible',
                'title': _details_title(action_name),
                'content': "\n".join(html_parts),
                'plain_content': "\n\n".join(plain_parts),
                'content_is_html': True,
            }

        if action_name == 'read_files':
            paths = action_args.get('paths') or []
            if isinstance(paths, str):
                paths = [paths]
            content = "\n".join(str(path) for path in paths) or "(no paths)"
            return {'type': 'collapsible', 'title': _details_title(action_name), 'content': content}

        if action_name == 'list_directory':
            path = action_args.get('path', '.')
            recursive = action_args.get('recursive')
            content = f"Path: {path}"
            if recursive is not None:
                content += f"\nRecursive: {recursive}"
            return {'type': 'collapsible', 'title': _details_title(action_name), 'content': content}

        if action_name == 'search_file_content':
            pattern = action_args.get('pattern', '')
            path = action_args.get('path') or action_args.get('directory') or '.'
            content = f"Pattern: {pattern}\nPath: {path}"
            return {'type': 'collapsible', 'title': _details_title(action_name), 'content': content}

        return {
            'type': 'collapsible',
            'title': _details_title(action_name),
            'content': json.dumps(action_args, ensure_ascii=False, indent=2),
        }

    def replay_event(self, event: dict) -> bool:
        """Rebuild terminal action summaries/details from the session log."""
        if event.get("type") == "tool_call":
            name = str(event.get("name") or "action")
            args = event.get("input") or {}
            if not isinstance(args, dict):
                args = {}
            label = _action_summary(name, args)
            self.ui.add_message(self._action_summary_html(label))
            if args and name not in {
                    'execute_shell', 'poll_shell_job', 'stop_shell_job',
                    'read_files', 'list_directory', 'search_file_content'}:
                self.ui.add_message(self._details_block(name, args))
            return True
        return super().replay_event(event)

    def execute_action(self, action: dict) -> str | None:
        """
        Executes the action (tool call) requested by the LLM.
        Intercepts web requests to handle them with the hybrid model.
        Returns history text, or None when completion is asynchronous
        (web requests continue the loop from their thread slots).
        """
        action_name = action.get('name')
        action_args = action.get('arguments', {})
        if not isinstance(action_args, dict):
            action_args = {}

        # --- Hybrid Web Request Interception ---
        if action_name in ['web_fetchs', 'web_search']:
            self._handle_web_request(action_name, action_args)
            return None
        # -----------------------------------------

        path = action_args.get('path')
        is_file_op = action_name in ['write_file', 'replace', 'replace_many']
        action_label = _action_summary(action_name, action_args)
        self._pending_action_label = action_label

        if self.active_edit_path and (not is_file_op or path != self.active_edit_path):
            self.ui.add_message(self.html_formatter.format_container_end())
            self.active_edit_path = None

        if is_file_op:
            if not self.active_edit_path:
                self.active_edit_path = path
                self.ui.add_message(self.html_formatter.format_container_start(path))

            if action_name == 'write_file':
                content = action_args.get('content', '')
                line_count = len(content.splitlines())
                self.ui.add_text(f"Writing {line_count} line(s)")
                self.ui.add_message({
                    'type': 'collapsible',
                    'title': f'File content · {path}',
                    'content': self.html_formatter.format_code_content(content or '', str(path or '')),
                    'plain_content': content or '(empty)',
                    'content_is_html': True,
                })

            elif action_name in ['replace', 'replace_many']:
                replacements = [action_args] if action_name == 'replace' else action_args.get('replacements', [])
                try:
                    with open(path, encoding='utf-8') as f:
                        full_content = f.read()
                except Exception:
                    full_content = ""

                diff_chunks = []
                diff_html_chunks = []
                for replacement in replacements:
                    old_string = replacement.get('old_string', '')
                    new_string = replacement.get('new_string', '')
                    old_lines, new_lines = old_string.split('\n'), new_string.split('\n')
                    dedented_all = self._dedent_lines(old_lines + new_lines)
                    dedented_old, dedented_new = dedented_all[:len(old_lines)], dedented_all[len(old_lines):]
                    index = full_content.find(old_string)
                    start_line = full_content.count('\n', 0, index) + 1 if index != -1 else -1
                    diff_chunks.append(self._format_diff_text(dedented_old, dedented_new, start_line))
                    diff_html_chunks.append(self.html_formatter.format_diff_content(dedented_old, dedented_new, start_line))

                if diff_chunks:
                    self.ui.add_text(f"Changed {len(diff_chunks)} replacement(s)")
                    self.ui.add_message({
                        'type': 'collapsible',
                        'title': f'Diff · {action_name} · {path}',
                        'content': "\n".join(diff_html_chunks),
                        'plain_content': "\n\n".join(diff_chunks),
                        'content_is_html': True,
                    })
        else:
            self.ui.add_message(self._action_summary_html(action_label))
            # Shell commands and self-describing file results get one combined
            # block when their result arrives. A separate pre-execution details
            # block only duplicated the command/path/pattern.
            if action_args and action_name not in {
                    'execute_shell', 'poll_shell_job', 'stop_shell_job',
                    'read_files', 'list_directory', 'search_file_content'}:
                self.ui.add_message(self._details_block(action_name, action_args))
        # ---------------------------------------------

        self._ensure_exec_thread()
        self._pending_action_name = action_name
        self._pending_action_args = action_args
        try:
            self.computer.cancel_event.clear()
        except Exception:
            pass
        self._exec_ticket += 1
        self._exec_worker.reset(self._exec_ticket)
        logging.info(f"TerminalHandler: dispatching ticket {self._exec_ticket} ({action_name}) to exec thread.")
        self._tool_in_flight = True
        self._exec_worker.dispatch.emit(self._exec_ticket, action_name, action_args)
        return None

    def _handle_web_request(self, action_name: str, action_args: dict):
        """ Handles web_fetchs and web_search asynchronously using a WebTaskThread."""

        self._start_thinking_timer("Accessing web...")
        self._tool_in_flight = True
        provider = self.controller.llm_worker.provider

        if self.web_thread and self.web_thread.isRunning():
            self.web_thread.cancel()
            self.web_thread.quit()
            self.web_thread.wait()

        self.web_thread = WebTaskThread(self.computer, provider, action_name, action_args,
                                         self._exec_relay)
        self.web_thread.finished.connect(self.web_thread.deleteLater)
        self.web_thread.start()

    @Slot(str)
    def _on_plan_a_failed(self, provider_name: str):
        self.ui.add_message(f"<i>Native search with {provider_name} failed. Trying local fallback...</i>")

    @Slot(str, str, dict)
    def _on_web_request_complete(self, action_name: str, result: str, original_args: dict):
        logging.info(f"Web request '{action_name}' completed successfully.")
        self._tool_in_flight = False
        self._stop_thinking_timer()
        self._handle_final_response(action_name, result, original_args)
        self.web_thread = None

    @Slot(str, str, dict)
    def _on_web_request_error(self, action_name: str, error_message: str, original_args: dict):
        logging.error(f"Web request for {action_name} failed: {error_message}")
        self._tool_in_flight = False
        self._stop_thinking_timer()
        error_msg_for_llm = f"**Error:** Local fallback for {action_name} also failed: {error_message}"
        self.ui.add_message(f"<i>{error_msg_for_llm}</i>")
        self.record_tool_result(error_msg_for_llm, status="error", name=action_name)
        self._continue_loop(error_msg_for_llm)
        self.web_thread = None

    def _handle_final_response(self, action_name: str, response_text: str, original_args: dict):
        """Displays the final web-related response and continues the main agent loop."""
        title = "Web Content" # Default title
        if action_name == 'web_search':
            query = original_args.get('query', '...')
            title = f"Web Search Results for: '{query}'"
        elif action_name == 'web_fetchs':
            urls = original_args.get('urls', [])
            if len(urls) == 1:
                title = f"Content from: {urls[0]}"
            elif len(urls) > 1:
                title = f"Content from {len(urls)} URLs"

        # Use the new collapsible block for the UI
        output_for_ui = {
            'type': 'collapsible',
            'title': title,
            'content': response_text
        }
        self.ui.add_message(output_for_ui)

        if hasattr(self.controller.llm_worker, 'log_tool_response'):
            self.controller.llm_worker.log_tool_response(action_name, response_text)

        output_for_llm = f"**Tool Result:**\n```\n{response_text}\n```"
        self.record_tool_result(output_for_llm, name=action_name)
        self._continue_loop(output_for_llm)

    def _format_result_for_llm_and_ui(self, tool_name: str, result: any, args: dict) -> (str, any):
        """
        Formats the result of a tool execution for the LLM and the UI.
        For the UI, it can return a string (for simple messages) or a dict (for complex widgets).
        """
        output_for_llm = ""
        output_for_ui = None # Can be str or dict

        job_id = _running_job_id(result)
        if tool_name == 'execute_shell' and job_id is not None:
            command = str(args.get('command') or '')
            cwd = str(args.get('cwd') or '')
            if not cwd:
                computer = getattr(self, 'computer', None)
                cwd = str(getattr(computer, 'working_directory', '') or '')
            job_commands = getattr(self, '_job_commands', None)
            if job_commands is not None:
                job_commands[job_id] = command
                if len(job_commands) > 100:
                    job_commands.pop(next(iter(job_commands)))
            job_cwds = getattr(self, '_job_cwds', None)
            if job_cwds is not None and cwd:
                job_cwds[job_id] = cwd
                if len(job_cwds) > 100:
                    job_cwds.pop(next(iter(job_cwds)))
            readiness_checked = bool(result.get('readiness_checked'))
            ready = bool(result.get('ready'))
            ready_url = str(result.get('ready_url') or '')
            readiness_elapsed = result.get('readiness_elapsed')
            readiness_error = result.get('readiness_error')
            status_code = result.get('status_code')
            job_state = str(result.get('state') or (
                'ready' if readiness_checked and ready else 'starting'))
            elapsed_text = (f"{readiness_elapsed:g}s"
                            if isinstance(readiness_elapsed, (int, float))
                            else "?s")
            if readiness_checked and ready:
                note = (f"Started as job #{job_id}. State: {job_state}. "
                        f"Ready at {ready_url} (HTTP {status_code}) after "
                        f"{elapsed_text}. Stop with stop_shell_job when done.")
                output_for_llm = (
                    f"**Tool Result:** Background job #{job_id} started; "
                    f"state: {job_state}. It is ready at {ready_url} "
                    f"(HTTP {status_code}) after {elapsed_text}. Stop it with "
                    f"stop_shell_job when done.\n```\n{result}\n```"
                )
                title = f"Shell · job #{job_id} · {job_state} · {ready_url}"
                title_color = self._theme("SUCCESS_COLOR")
            elif readiness_checked:
                note = (f"Started as job #{job_id}. State: {job_state}. "
                        f"{ready_url} did not answer within the readiness "
                        f"timeout ({readiness_error or 'no response'}). Check "
                        "poll_shell_job/output_file before deciding; do not "
                        "relaunch blindly.")
                output_for_llm = (
                    f"**Tool Result:** Background job #{job_id} started; "
                    f"state: {job_state}. The readiness probe for {ready_url} "
                    f"failed after {elapsed_text}. Last error: "
                    f"{readiness_error or 'none'}.\n"
                    "Check poll_shell_job and output_file before deciding; do "
                    "not relaunch blindly.\n"
                    f"```\n{result}\n```"
                )
                title = f"Shell · job #{job_id} · {job_state} · {ready_url}"
                title_color = self._theme("WARNING_COLOR")
            else:
                note = (f"Started in background as job #{job_id}. "
                        f"State: {job_state}. Use poll_shell_job to check "
                        "progress, stop_shell_job to abort.")
                output_for_llm = (
                    f"**Tool Result:** Background shell job #{job_id} started; "
                    f"state: {job_state}.\n"
                    f"Poll it with poll_shell_job, pacing polls with wait_seconds.\n"
                    f"```\n{result}\n```"
                )
                title = f"Shell · job #{job_id} · {job_state}"
                title_color = self._theme("WARNING_COLOR")
            output_file = result.get('output_file')
            actions = {f"Stop job #{job_id}": "stop_job"}
            if output_file:
                actions["Open log"] = "open_log"
            output_for_ui = {
                'type': 'collapsible',
                'title': title,
                'content': self.html_formatter.format_shell_block(
                    command, exit_note=note, show_output=False, cwd=cwd,
                    output_file=output_file),
                'plain_content': f"$ {command}\n\n{note}",
                'content_is_html': True,
                'title_color': title_color,
                'copy_payloads': {'command': command},
                'job_id': job_id,
                'job_role': 'launch',
                'job_state': job_state,
                'actions': actions,
                'open_log_path': output_file,
                'state': 'expanded',
            }
            return output_for_llm, output_for_ui

        if tool_name in ('poll_shell_job', 'stop_shell_job') and isinstance(result, dict) \
                and 'status' in result:
            # Human-readable job reports (never raw dict reprs: their literal
            # \n sequences render as an unreadable wall in HTML).
            job_id = result.get('job_id', '?')
            status = result.get('status', '?')
            elapsed = result.get('elapsed_seconds', '?')
            output = result.get('output', '')
            job_commands = getattr(self, '_job_commands', {}) or {}
            try:
                command = job_commands.get(int(job_id), '')
            except (TypeError, ValueError):
                command = job_commands.get(job_id, '')
            job_cwds = getattr(self, '_job_cwds', {}) or {}
            try:
                cwd = job_cwds.get(int(job_id), '')
            except (TypeError, ValueError):
                cwd = job_cwds.get(job_id, '')
            cwd = str(cwd or args.get('cwd') or '')
            if not cwd:
                computer = getattr(self, 'computer', None)
                cwd = str(getattr(computer, 'working_directory', '') or '')

            if status == 'done' and command:
                shell_result = {
                    'stdout': output,
                    'stderr': '',
                    'exit_code': result.get('exit_code', -1),
                    'wall_time': result.get('wall_time', elapsed),
                    'total_lines': result.get('total_lines'),
                    'total_chars': result.get('total_chars'),
                    'timed_out': result.get('timed_out', False),
                    'cancelled': result.get('cancelled', False),
                    'inactivity_timeout': result.get('inactivity_timeout', False),
                    'exit_note': result.get('exit_note'),
                    'output_file': result.get('output_file'),
                    'state': result.get('state'),
                }
                llm, ui = self._format_result_for_llm_and_ui(
                    'execute_shell', shell_result,
                    {'command': command, 'job_id': job_id, 'cwd': cwd})
                verb = str(result.get('state') or 'finished')
                return f"**Background job #{job_id} {verb}.**\n{llm}", ui

            if status == 'running' and command:
                content = self.html_formatter.format_shell_block(
                    command, stdout=output, cwd=cwd)
                job_state = str(result.get('state') or 'running')
                output_for_llm = (
                    f"**Tool Result:** Background job #{job_id} state: "
                    f"{job_state} after {elapsed}s.\n"
                    f"```\n{output or '(no new output)'}\n```\n"
                    f"**System note:** {_RUNNING_JOB_HINT}"
                )
                output_file = result.get('output_file')
                actions = {f"Stop job #{job_id}": "stop_job"}
                if output_file:
                    actions["Open log"] = "open_log"
                output_for_ui = {
                    'type': 'collapsible',
                    'title': f"Shell · job #{job_id} · {job_state} · {elapsed}s",
                    'content': content,
                    'plain_content': f"$ {command}\n\n{output or '(no new output)'}",
                    'content_is_html': True,
                    'title_color': self._theme(
                        "SUCCESS_COLOR" if job_state == "ready"
                        else "WARNING_COLOR"),
                    'copy_payloads': {'command': command, 'output': output or '(no new output)'},
                    'job_id': job_id,
                    'job_role': 'poll',
                    'job_state': job_state,
                    'actions': actions,
                    'open_log_path': output_file,
                    'state': 'expanded',
                }
                return output_for_llm, output_for_ui

            if status == 'not_found' and command:
                output_for_llm = (
                    f"**Tool Result:** Background job #{job_id} has already finished "
                    "and was reaped; no action is needed."
                )
                output_for_ui = {
                    'type': 'collapsible',
                    'title': f"Background job #{job_id} · already finished",
                    'content': self.html_formatter.format_plain_content(
                        "This job has already finished. No action is needed."),
                    'plain_content': "This job has already finished. No action is needed.",
                    'content_is_html': True,
                }
                return output_for_llm, output_for_ui

            if status == 'done':
                exit_code = result.get('exit_code', '?')
                job_state = str(result.get('state') or 'finished')
                title = (f"Background job #{job_id} · {job_state} · "
                         f"exit {exit_code}")
                title_color = self._theme(
                    "SUCCESS_COLOR" if job_state == "finished"
                    else "WARNING_COLOR" if job_state == "stopped"
                    else "ERROR_COLOR")
                output_for_llm = (
                    f"**Tool Result:** Background job #{job_id} {job_state} "
                    f"(exit {exit_code}) after {elapsed}s.\n"
                    f"```\n{output or '(no output)'}\n```"
                )
                output_for_ui = {
                    'type': 'collapsible',
                    'title': title,
                    'content': self.html_formatter.format_plain_content(
                        output or '(no output)'),
                    'plain_content': output or '(no output)',
                    'content_is_html': True,
                    'title_color': title_color,
                    'copy_payloads': {'output': output or '(no output)'},
                    'job_id': job_id,
                    'job_state': job_state,
                    'state': 'expanded' if job_state != "finished" else 'collapsed',
                }
                return output_for_llm, output_for_ui

            if status == 'running':
                job_state = str(result.get('state') or 'running')
                output_for_llm = (
                    f"**Tool Result:** Background job #{job_id} state: "
                    f"{job_state} after {elapsed}s.\n"
                    f"```\n{output or '(no new output)'}\n```\n"
                    f"**System note:** {_RUNNING_JOB_HINT}"
                )
                output_file = result.get('output_file')
                actions = {f"Stop job #{job_id}": "stop_job"}
                if output_file:
                    actions["Open log"] = "open_log"
                output_for_ui = {
                    'type': 'collapsible',
                    'title': f"Background job #{job_id} · {job_state}",
                    'content': self.html_formatter.format_plain_content(
                        output or '(no new output)'),
                    'plain_content': output or '(no new output)',
                    'content_is_html': True,
                    'title_color': self._theme(
                        "SUCCESS_COLOR" if job_state == "ready"
                        else "WARNING_COLOR"),
                    'copy_payloads': {'output': output or '(no new output)'},
                    'job_id': job_id,
                    'job_role': 'poll',
                    'job_state': job_state,
                    'actions': actions,
                    'open_log_path': output_file,
                    'state': 'expanded',
                }
                return output_for_llm, output_for_ui

            output_for_llm = (
                f"**Tool Result:** Background job #{job_id} not found "
                f"(already reaped after finishing, or wrong id)."
            )
            output_for_ui = {
                'type': 'collapsible',
                'title': f"Background job #{job_id} · not found",
                'content': self.html_formatter.format_plain_content(
                    "Unknown job id. It may have been reaped, or the id is wrong."),
                'plain_content': "Unknown job id. It may have been reaped, or the id is wrong.",
                'content_is_html': True,
                'title_color': self._theme("ERROR_COLOR"),
            }
            return output_for_llm, output_for_ui

        if tool_name == 'wait_seconds' and isinstance(result, dict):
            waited = result.get('waited_seconds', '?')
            state_word = 'completed' if result.get('completed') else 'interrupted'
            output_for_llm = f"**Tool Result:** Waited {waited}s ({state_word})."
            output_for_ui = f"<i>Waited {waited}s.</i>"
            return output_for_llm, output_for_ui

        if tool_name == 'execute_shell':
            command = str(args.get('command') or '')
            stdout = result.get('stdout', '')
            stderr = result.get('stderr', '')
            exit_code = result.get('exit_code', -1)
            wall_time = result.get('wall_time')
            total_lines = result.get('total_lines')
            total_chars = result.get('total_chars')
            exit_note = result.get('exit_note')
            output_file = result.get('output_file')
            timed_out = bool(result.get('timed_out'))
            cancelled = bool(result.get('cancelled'))
            inactive = bool(result.get('inactivity_timeout'))
            job_state = str(result.get('state') or '')
            meta = []
            if wall_time is not None:
                meta.append(f"{wall_time}s")
            if total_lines is not None:
                meta.append(f"{total_lines} line(s)")
            if total_chars is not None:
                meta.append(f"{total_chars} chars")
            meta_text = (", " + ", ".join(meta)) if meta else ""
            ui_meta = (" · " + " · ".join(meta)) if meta else ""
            state_text = f"State: {job_state}, " if job_state else ""
            header = (f"**Command Result:** ({state_text}Exit Code: "
                      f"{exit_code}{meta_text})\n")
            if exit_note:
                header += f"**Note:** {exit_note}\n"
            output_for_llm = (
                header
                + f"**OUTPUT:**\n```\n{stdout or '(no output)'}\n```"
            )
            if stderr:
                output_for_llm += f"\n**SYSTEM:**\n```\n{stderr}\n```"
            job_id_arg = args.get('job_id')
            prefix = f"Shell · job #{job_id_arg}" if job_id_arg else "Shell"
            if job_state == "finished":
                title = f"{prefix} · finished{ui_meta}"
            elif job_state == "failed":
                title = f"{prefix} · failed · exit {exit_code}{ui_meta}"
            elif job_state == "stopped":
                title = f"{prefix} · stopped{ui_meta}"
            elif job_state in ("ready", "running", "starting"):
                title = f"{prefix} · {job_state}{ui_meta}"
            elif timed_out:
                title = f"{prefix} · timed out{ui_meta}"
            elif cancelled:
                title = f"{prefix} · stopped{ui_meta}"
            elif inactive:
                title = f"{prefix} · stopped (inactive){ui_meta}"
            elif exit_code == 0:
                title = f"{prefix} · exit 0{ui_meta}"
            else:
                title = f"{prefix} · exit {exit_code}{ui_meta}"
            cwd = str(args.get('cwd') or '')
            if not cwd:
                computer = getattr(self, 'computer', None)
                cwd = str(getattr(computer, 'working_directory', '') or '')
            display_stdout = str(stdout or '')
            if output_file:
                display_stdout = re.sub(
                    r"\n*\[Full output saved to: .*?\]", "", display_stdout).rstrip()
            content_html = self.html_formatter.format_shell_block(
                command, stdout=display_stdout, stderr=stderr,
                exit_note=exit_note or '', output_file=output_file or '',
                cwd=cwd)
            plain_parts = [f"$ {command}", "", display_stdout or '(no output)']
            if stderr:
                plain_parts += ["", f"System note: {stderr}"]
            if exit_note:
                plain_parts += ["", f"Note: {exit_note}"]
            if output_file:
                plain_parts += ["", f"Full output: {output_file}"]
            if job_state in ("finished", "ready"):
                title_color = self._theme("SUCCESS_COLOR")
            elif job_state in ("failed",):
                title_color = self._theme("ERROR_COLOR")
            elif job_state in ("stopped", "running", "starting"):
                title_color = self._theme("WARNING_COLOR")
            elif timed_out:
                title_color = self._theme("ERROR_COLOR")
            elif cancelled or inactive:
                # A user stop / idle kill is an intentional outcome, not a
                # failed command. Red made a clean stop look like a crash.
                title_color = self._theme("WARNING_COLOR")
            elif exit_code == 0:
                title_color = self._theme("SUCCESS_COLOR")
            elif exit_note:
                title_color = self._theme("WARNING_COLOR")
            else:
                title_color = self._theme("ERROR_COLOR")
            output_for_ui = {
                'type': 'collapsible',
                'title': title,
                'content': content_html,
                'plain_content': "\n".join(plain_parts),
                'content_is_html': True,
                'title_color': title_color,
                'copy_payloads': {
                    'command': command,
                    'output': display_stdout or '(no output)',
                },
                'job_id': job_id_arg,
                'job_state': job_state,
                # Failures open by default so the error is not hidden behind
                # another click; successful calls stay compact.
                'state': ('expanded' if (job_state == "failed"
                                         or (not job_state and timed_out)
                                         or (not job_state and exit_code != 0
                                             and not cancelled and not inactive))
                          else 'collapsed'),
            }

        elif tool_name in ['write_file', 'replace', 'replace_many']:
            output_for_llm = f"**Tool Result:**\n```\n{result}\n```"
            output_for_ui = f"<i>{html.escape(str(result))}</i>"

        elif tool_name == 'read_files':
            paths = args.get('paths', [])
            title = f"Read {len(paths)} file(s)"
            output_for_llm = f"**Tool Result:**\n```\n{result}\n```"
            output_for_ui = {
                'type': 'collapsible',
                'title': title,
                'content': self.html_formatter.format_read_files_content(str(result)),
                'plain_content': str(result),
                'content_is_html': True,
                'copy_payloads': {'output': str(result)},
            }

        elif tool_name in ['delete_files', 'move_files', 'copy_files']:
            output_for_llm = f"**Tool Result:**\n```\n{result}\n```"
            output_for_ui = {
                'type': 'collapsible',
                'title': "File operation result",
                'content': str(result)
            }

        elif tool_name == 'search_file_content':
            query = args.get('pattern', 'N/A')
            path = args.get('path') or args.get('directory') or '.'
            title = f"Search results · '{query}' in '{path}'"
            items = result if isinstance(result, list) else str(result).splitlines()
            content = "\n".join(str(item) for item in items)
            output_for_llm = f"**Tool Result:**\n```\n{content}\n```"
            if result:
                output_for_ui = {
                    'type': 'collapsible',
                    'title': title,
                    'content': self.html_formatter.format_list_content(
                        items, empty_text="(No results found)"),
                    'plain_content': content,
                    'content_is_html': True,
                    'copy_payloads': {'output': content},
                }
            else:
                output_for_ui = "<i>(No results found)</i>"

        elif tool_name == 'list_directory':
            path = args.get('path', '.')
            title = f"Directory contents · {path}"
            items = result if isinstance(result, list) else str(result).splitlines()
            items = sorted((str(item) for item in items), key=str.casefold)
            content = "\n".join(items)
            output_for_llm = f"**Tool Result:**\n```\n{content}\n```"
            if result:
                output_for_ui = {
                    'type': 'collapsible',
                    'title': title,
                    'content': self.html_formatter.format_list_content(
                        items, empty_text=f"(Directory '{path}' is empty or not found)"),
                    'plain_content': content,
                    'content_is_html': True,
                    'copy_payloads': {'output': content},
                }
            else:
                output_for_ui = f"<i>(Directory '{path}' is empty or not found)</i>"

        else:
            # Generic fallback for any other tool
            str_result = str(result)
            output_for_llm = f"**Tool Result:**\n```\n{str_result}\n```"
            output_for_ui = {'type': 'collapsible', 'title': f"Result · {tool_name}", 'content': str_result}

        return output_for_llm, output_for_ui

    def _ensure_exec_thread(self):
        if self._exec_thread_restart_pending:
            # The previous task was stopped while a tool was still running;
            # quit() only ends the loop after that slot returns. Give it a
            # bounded chance, then move to a fresh thread so a new task can
            # never be queued into a loop that is about to exit.
            if self._exec_thread.isRunning():
                if not self._exec_thread.wait(3000):
                    self._spawn_exec_thread()
                    return
            self._exec_thread_restart_pending = False
        if not self._exec_thread.isRunning():
            self._exec_thread.start()

    def _spawn_exec_thread(self):
        old_thread, old_worker = self._exec_thread, self._exec_worker
        logging.warning("Terminal exec thread did not stop in time; replacing it.")
        self._exec_thread = QThread()
        self._exec_worker = ToolExecWorker(self.computer, self._exec_relay)
        self._exec_worker.moveToThread(self._exec_thread)
        self._exec_thread.start()
        self._exec_thread_restart_pending = False
        self._abandoned_exec_workers.append((old_thread, old_worker))
        old_thread.finished.connect(
            lambda t=old_thread, w=old_worker: self._retire_exec_worker(t, w))

    def _retire_exec_worker(self, thread, worker):
        try:
            worker.deleteLater()
        except Exception:
            pass
        try:
            thread.deleteLater()
        except Exception:
            pass
        self._abandoned_exec_workers = [
            pair for pair in self._abandoned_exec_workers if pair[0] is not thread
        ]

    def _running_jobs_note(self) -> str:
        try:
            manager = getattr(self.computer, "shell_jobs", None)
            if manager is not None:
                return manager.describe_running()
        except Exception:
            pass
        return ""

    @Slot(int, float, str)
    def _on_tool_progress(self, ticket: int, elapsed: float, last_line: str):
        if ticket != self._exec_ticket:
            return
        pct = ""
        try:
            m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", last_line or "")
            if m:
                pct = f" · {m.group(1)}%"
        except Exception:
            pass
        tail = html.escape((last_line or "")[-200:]).strip()
        tail_html = f"<br><code>{tail}</code>" if tail else ""
        label = self._pending_action_label or self._pending_action_name
        self.ui.show_tool_progress(
            ticket,
            f"⏳ <b>{html.escape(str(label))}</b>{pct} · {elapsed:.0f}s{tail_html}"
        )

    def _show_job_result(self, job_id, output_for_ui):
        """Replace the job's launch block with its final state.

        Poll snapshots for the same job are stale once it finishes, so the
        widget removes them when the update succeeds. Fall back to a new block
        when no existing block is available (e.g. after a restart).
        """
        update = getattr(self.ui, 'update_job_block', None)
        if update is not None:
            try:
                if update(int(job_id), output_for_ui):
                    return
            except (TypeError, ValueError):
                pass
            except Exception as e:
                logging.warning(f"Job block update failed for #{job_id}: {e}")
        self.ui.add_message(output_for_ui)

    @Slot(int, object)
    def _on_tool_finished(self, ticket: int, result):
        if ticket != self._exec_ticket:
            logging.info(f"Stale tool result ignored (ticket {ticket} != {self._exec_ticket}).")
            return
        self._tool_in_flight = False
        try:
            self.ui.hide_tool_progress(ticket)
        except Exception:
            pass
        logging.info(f"TerminalHandler: ticket {ticket} ({self._pending_action_name}) result received, continuing loop.")
        output_for_llm, output_for_ui = self._format_result_for_llm_and_ui(
            self._pending_action_name, result, self._pending_action_args
        )
        self.record_tool_result(output_for_llm, name=self._pending_action_name)
        job_id = _running_job_id(result)
        if output_for_ui:
            if (isinstance(result, dict) and result.get('status') == 'done'
                    and result.get('job_id') is not None):
                self._show_job_result(result.get('job_id'), output_for_ui)
            else:
                self.ui.add_message(output_for_ui)
        if job_id is not None and self._pending_action_name == 'execute_shell':
            # Background launch: do NOT ask the model immediately (it acts
            # NOW — e.g. relaunches). Record the launch, wait handler-side.
            # A readiness probe already spent the startup window, so its
            # verdict is final and the model gets control immediately.
            # An explicit poll is different: its result is already current,
            # so waiting again here only stalls the model by 10 seconds.
            self.command_history.append(output_for_llm)
            if isinstance(result, dict) and result.get('readiness_checked'):
                self._continue_loop(output_for_llm)
                return
            self._arm_job_wait(job_id)
            return
        self._continue_loop(output_for_llm)

    @Slot(int, str)
    def _on_tool_error(self, ticket: int, error_message: str):
        if ticket != self._exec_ticket:
            logging.info(f"Stale tool error ignored (ticket {ticket} != {self._exec_ticket}).")
            return
        self._tool_in_flight = False
        try:
            self.ui.hide_tool_progress(ticket)
        except Exception:
            pass
        logging.info(f"TerminalHandler: ticket {ticket} ({self._pending_action_name}) error received, continuing loop.")
        error_msg = f"**Error:** {error_message}"
        self.ui.add_message(f"<i>{error_msg}</i>")
        self.record_tool_result(
            error_msg, status="error", name=self._pending_action_name)
        self._continue_loop(error_msg)

    def cancel_job_wait(self):
        try:
            self._job_wait_timer.stop()
        except Exception:
            pass
        self._awaiting_job_id = None
        self._job_wait_rounds = 0

    def _mark_job_handled(self, job_id: int):
        self._handled_job_ids.add(job_id)
        if len(self._handled_job_ids) > 200:
            self._handled_job_ids = set(list(self._handled_job_ids)[-100:])

    @Slot(dict)
    def _on_job_event(self, event):
        """Wake the loop (or leave a note) when a background job ends."""
        if not isinstance(event, dict):
            return
        if event.get('type') not in ('finished', 'stopped'):
            return
        try:
            job_id = int(event.get('job_id'))
        except (TypeError, ValueError):
            return

        res = dict(event.get('result') or {})
        elapsed = event.get('elapsed_seconds', 0)
        final = {
            'job_id': job_id,
            'status': 'done',
            'state': event.get('state') or 'finished',
            'exit_code': res.get('exit_code', -1),
            'elapsed_seconds': elapsed,
            'wall_time': res.get('wall_time', elapsed),
            'total_lines': res.get('total_lines'),
            'total_chars': res.get('total_chars'),
            'timed_out': bool(res.get('timed_out')),
            'cancelled': bool(res.get('cancelled')),
            'inactivity_timeout': bool(res.get('inactivity_timeout')),
            'exit_note': res.get('stderr') or res.get('exit_note'),
            'output_file': res.get('output_file') or event.get('output_file'),
            'output': str(res.get('stdout') or ''),
        }
        tool_name = ('stop_shell_job' if event.get('type') == 'stopped'
                     else 'poll_shell_job')
        try:
            output_for_llm, output_for_ui = self._format_result_for_llm_and_ui(
                tool_name, final, {'job_id': job_id})
        except Exception as e:
            logging.warning(f"Could not format job event #{job_id}: {e}")
            output_for_llm, output_for_ui = "", None
        if not output_for_llm:
            output_for_llm = (
                f"**Tool Result:** Background job #{job_id} finished "
                f"(exit {final['exit_code']}).")

        if output_for_ui:
            self._show_job_result(job_id, output_for_ui)

        if job_id in self._handled_job_ids:
            return
        self._mark_job_handled(job_id)
        if final.get("cancelled") or event.get("type") == "stopped":
            job_status = "stopped"
        elif final.get("timed_out") or final.get("exit_code") not in (0, None):
            job_status = "error"
        else:
            job_status = "ok"
        self._append_session_event(
            "job_result",
            turn_id=getattr(self.controller, "session_turn_id", ""),
            job_id=job_id,
            name=tool_name,
            status=job_status,
            exit_code=final.get("exit_code"),
            output={"text": str(output_for_llm or "")},
        )

        if (self._awaiting_job_id == job_id
                and self.state == VisionState.EXECUTING
                and not self._tool_in_flight):
            self.cancel_job_wait()
            logging.info(f"TerminalHandler: job #{job_id} finished; waking loop.")
            self._continue_loop(output_for_llm, from_job_wait=True)
            return

        if output_for_llm:
            self.command_history.append(
                f"**Background job #{job_id} finished while you were not "
                f"waiting for it.**\n{output_for_llm}")

    def request_stop_job(self, job_id: int):
        """Non-blocking stop for the transcript's Stop action."""
        manager = getattr(self.computer, "shell_jobs", None)
        if manager is None:
            return
        result = manager.request_stop(job_id)
        if result.get('status') == 'not_found':
            self.ui.add_message(
                f"<i>Background job #{job_id} has already finished.</i>")

    def _arm_job_wait(self, job_id: int):
        self.cancel_job_wait()
        self._awaiting_job_id = job_id
        self._job_wait_rounds = 0
        logging.info(f"TerminalHandler: awaiting background job #{job_id} handler-side.")
        self._start_thinking_timer(f"Waiting for job #{job_id}...")
        try:
            self._job_wait_timer.start(JOB_WAIT_INTERVAL_MS)
        except Exception as e:
            logging.warning(f"Job wait timer failed to start: {e}")
            self._awaiting_job_id = None

    def _on_job_wait_tick(self):
        job_id = self._awaiting_job_id
        if job_id is None or self.state != VisionState.EXECUTING or self._tool_in_flight:
            # Stopped, paused, superseded, or a tool took over (its finish
            # continues the loop and cancels this wait via _continue_loop).
            self._awaiting_job_id = None
            return
        try:
            manager = getattr(self.computer, "shell_jobs", None)
            if manager is None:
                raise RuntimeError("no shell job manager")
            status = manager.status(job_id)
        except Exception as e:
            logging.warning(f"Job wait poll failed: {e}")
            self._awaiting_job_id = None
            return
        if status.get("status") == "done":
            self._awaiting_job_id = None
            self._mark_job_handled(job_id)
            job_id_arg = job_id
            try:
                final = manager.poll(job_id)
            except Exception as e:
                logging.warning(f"Final job poll failed: {e}")
                return
            output_for_llm, output_for_ui = self._format_result_for_llm_and_ui(
                "poll_shell_job", final, {"job_id": job_id_arg}
            )
            if output_for_ui:
                self._show_job_result(job_id_arg, output_for_ui)
            self._continue_loop(output_for_llm, from_job_wait=True)
            return
        self._job_wait_rounds += 1
        if self._job_wait_rounds >= JOB_WAIT_MAX_ROUNDS:
            self._awaiting_job_id = None
            logging.info(f"TerminalHandler: job #{job_id} still running after "
                         f"{JOB_WAIT_MAX_ROUNDS} startup checks; handing control back.")
            try:
                current = manager.poll(job_id)
            except Exception as e:
                logging.warning(f"Job poll failed: {e}")
                return
            output_for_llm, output_for_ui = self._format_result_for_llm_and_ui(
                "poll_shell_job", current, {"job_id": job_id}
            )
            if output_for_ui:
                self.ui.add_message(output_for_ui)
            self._continue_loop(output_for_llm, from_job_wait=True)
            return
        try:
            self._job_wait_timer.start(JOB_WAIT_INTERVAL_MS)
        except Exception:
            self._awaiting_job_id = None

    def _cleanup_task(self):
        self._exec_ticket += 1
        self._tool_in_flight = False
        self.cancel_job_wait()
        try:
            self.computer.cancel_event.set()
        except Exception:
            pass
        try:
            manager = getattr(self.computer, "shell_jobs", None)
            if manager is not None:
                manager.kill_all()
        except Exception as e:
            logging.warning(f"Background shell jobs cleanup failed: {e}")
        if self._exec_thread and self._exec_thread.isRunning():
            self._exec_worker.cancel()
            self._exec_thread.quit()
            if not self._exec_thread.wait(2000):
                logging.warning("Terminal exec thread did not stop in 2000ms.")
                self._exec_thread_restart_pending = True
        if self.active_edit_path:
            self.ui.add_message(self.html_formatter.format_container_end())
            self.active_edit_path = None

        if self.web_thread and self.web_thread.isRunning():
            self.web_thread.cancel()
