"""
Handles the logic for the terminal mode based on a stateful agent architecture.
The agent operates in a continuous loop, analyzing the state and executing commands
until the user's goal is achieved.
"""
import logging
import platform
import html
import re
from PySide6.QtCore import QObject, Slot, QThread, QTimer, Signal

from states import VisionState
from ui.terminal_html_formatter import TerminalHtmlFormatter
from src.computer.computer import Computer
from src.computer.terminal_policy import evaluate as evaluate_terminal_access
from src.computer.terminal_policy import normalize_mode as normalize_access_mode
from src.computer.tools.terminal.mcp_tools import servers_prompt_block

from .base_agent import BaseAgentHandler, _is_error_output
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
    if name == 'use_skill':
        skill = _shorten_text(args.get('name'))
        return f"Loading skill · {skill}" if skill else "Loading skill"
    if name in {'mcp_list', 'mcp_get', 'mcp_call'}:
        server = _shorten_text(args.get('server'))
        tool = _shorten_text(args.get('tool'))
        target = "/".join(part for part in (server, tool) if part)
        if name == 'mcp_list':
            return f"Listing MCP tools · {target}" if target else "Listing MCP servers"
        if name == 'mcp_get':
            return f"Inspecting MCP tool · {target}" if target else "Inspecting MCP tool"
        return f"MCP call · {target}" if target else "MCP call"
    return f"Running {name.replace('_', ' ')}"


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
        self.html_formatter = TerminalHtmlFormatter(
            self.theme_manager,
            base_dir=lambda: self.computer.working_directory)
        self.os_info = f"{platform.system()} {platform.release()}"
        self.web_thread = None

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
        self.html_formatter = TerminalHtmlFormatter(
            self.theme_manager,
            base_dir=lambda: self.computer.working_directory)

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
            history=history,
            available_skills=self.skills_block(),
            available_mcp_servers=self.mcp_servers_block(),
        )

    def mcp_servers_block(self) -> str:
        """Configured MCP servers (state and one-line description) for the prompt."""
        manager = getattr(getattr(self, "computer", None), "mcp_manager", None)
        try:
            return servers_prompt_block(manager)
        except Exception as error:
            logging.warning(f"{type(self).__name__} MCP servers lookup failed: {error}")
            return "No MCP servers are configured."

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

    def _theme(self, key: str) -> str:
        return self.theme_manager.get_style(key)

    def _step_title(self, action_name: str, action_args: dict) -> str:
        return _action_summary(action_name, action_args)

    _BULKY_ARG_KEYS = (
        'content', 'old_string', 'new_string', 'old_text', 'new_text',
        'replacements',
    )

    def _step_args_text(self, action_name: str, action_args: dict) -> str:
        """Arguments stay short: file bodies live in the step detail
        (write_file) or in the result (execute_shell), and the live step and
        its replay must show the same call."""
        args = dict(action_args or {})
        if action_name in {'write_file', 'replace', 'replace_many'}:
            for key in self._BULKY_ARG_KEYS:
                if key in args:
                    size = len(str(args[key]))
                    args[key] = f"<{size} chars>"
        if action_name == 'execute_shell':
            args.pop('command', None)
        return super()._step_args_text(action_name, args)

    def _replay_step_detail(self, action_name: str, action_args: dict):
        """Written file bodies and edit diffs render as they did live."""
        if action_name == 'write_file':
            content = action_args.get('content')
            path = action_args.get('path') or 'file.txt'
            if content:
                return self.html_formatter.format_code_content(
                    str(content), str(path))
        if action_name in ('replace', 'replace_many'):
            return self._edit_detail_html(action_name, action_args)
        return None

    def _edit_target_path(self, action_name: str, action_args: dict) -> str:
        key = 'file_path' if action_name == 'replace' else 'path'
        return str(action_args.get(key) or '')

    def _read_edit_target(self, action_name: str, action_args: dict) -> str:
        path = self._edit_target_path(action_name, action_args)
        if not path:
            return ""
        resolver = getattr(self.computer, '_resolve_path', None)
        absolute_path = resolver(path) if callable(resolver) else path
        try:
            with open(absolute_path, encoding='utf-8') as handle:
                return handle.read()
        except (OSError, ValueError):
            return ""

    def _edit_detail_html(self, action_name: str, action_args: dict):
        """Diff block of a replace/replace_many step, live and replayed.

        Live runs before the edit, so the original text anchors the line
        number. A replay runs after it, when old_string is gone from disk;
        new_string sits on the same line there because the text before the
        edit is unchanged.
        """
        if action_name == 'replace':
            replacements = [action_args]
        elif action_name == 'replace_many':
            replacements = action_args.get('replacements') or []
        else:
            return None
        if not isinstance(replacements, list) or not replacements:
            return None
        full_content = self._read_edit_target(action_name, action_args)
        chunks = []
        for replacement in replacements:
            if not isinstance(replacement, dict):
                continue
            old_string = str(replacement.get('old_string') or '')
            new_string = str(replacement.get('new_string') or '')
            old_lines, new_lines = old_string.split('\n'), new_string.split('\n')
            dedented_all = self._dedent_lines(old_lines + new_lines)
            dedented_old = dedented_all[:len(old_lines)]
            dedented_new = dedented_all[len(old_lines):]
            index = full_content.find(old_string)
            if index == -1 and new_string:
                index = full_content.find(new_string)
            start_line = full_content.count('\n', 0, index) + 1 if index != -1 else -1
            chunks.append(self.html_formatter.format_diff_content(
                dedented_old, dedented_new, start_line))
        if not chunks:
            return None
        return (f'<div style="margin-bottom: 4px;">Changed '
                f'{len(chunks)} replacement(s)</div>' + "".join(chunks))

    def _access_mode(self) -> str:
        settings = getattr(self.controller, "settings", None) or {}
        terminal = settings.get("terminal") or {}
        return normalize_access_mode(terminal.get("access_mode"))

    def _mcp_call_context(self, action_args) -> dict | None:
        """Describes the MCP tool a call targets, for the access policy."""
        manager = getattr(getattr(self, "computer", None), "mcp_manager", None)
        if manager is None or not getattr(manager, "is_running", False):
            return None
        server = str(action_args.get("server") or "").strip()
        tool = str(action_args.get("tool") or "").strip()
        if not server or not tool:
            return None
        try:
            status = manager.status(server)
            info = manager.get_tool(server, tool)
        except Exception:
            return None
        return {
            "server": server,
            "tool": tool,
            "trusted": bool(status.trusted),
            "state": status.state,
            "read_only": bool(info.read_only),
            "destructive": info.annotations.get("destructive_hint") is True,
        }

    def _authorize_action(self, action_name, action_args) -> bool:
        """Ask the user before a tool call the access mode does not allow."""
        project_root = getattr(self.computer, "working_directory", "") or ""
        mcp_tool = self._mcp_call_context(action_args) if action_name == "mcp_call" else None
        request = evaluate_terminal_access(
            action_name, action_args, project_root, self._access_mode(),
            mcp_tool=mcp_tool)
        if request is None:
            return True
        asker = getattr(self.controller, "ask_terminal_approval", None)
        if not callable(asker):
            return False
        try:
            return bool(asker(request))
        except Exception as error:
            logging.error(f"Terminal approval failed: {error}")
            return False

    def _report_denied(self, action_name, action_args):
        label = _action_summary(action_name, action_args)
        text = (f"**Tool Result:** Denied: the user rejected '{label}'. "
                "Do not retry this action; ask the user how to proceed or stop.")
        if self._active_step is not None:
            self._finish_active_step(
                status="denied", output=self._strip_result_prefix(text), expanded=True)
        else:
            self._add_result_card(text, f"{action_name} · denied", "denied")
        self.record_tool_result(text, status="denied", name=action_name)
        self._after_action_result(text, abort=True)

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

        if not self._authorize_action(action_name, action_args):
            self._report_denied(action_name, action_args)
            return None

        # --- Hybrid Web Request Interception ---
        if action_name in ['web_fetchs', 'web_search']:
            self._handle_web_request(action_name, action_args)
            return None
        # -----------------------------------------

        path = action_args.get('path')
        is_file_op = action_name in ['write_file', 'replace', 'replace_many']
        action_label = _action_summary(action_name, action_args)
        self._pending_action_label = action_label
        self._update_activity_step(self._active_step, title=action_label)

        if is_file_op:
            # The step row is the file-edit container now: its expanded body
            # carries the written content or the diff instead of a second card.
            if action_name == 'write_file':
                content = action_args.get('content', '')
                detail = self.html_formatter.format_code_content(
                    content or '', str(path or ''))
                self._update_activity_step(self._active_step, detail_html=detail)
            elif action_name in ['replace', 'replace_many']:
                detail = self._edit_detail_html(action_name, action_args)
                if detail:
                    self._update_activity_step(
                        self._active_step, detail_html=detail)

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
        if self._active_step is not None:
            self._finish_active_step(
                status="error", output=self._strip_result_prefix(error_msg_for_llm),
                expanded=True)
        else:
            self._add_result_card(
                error_msg_for_llm, f"{action_name} · error", "error")
        self.record_tool_result(error_msg_for_llm, status="error", name=action_name)
        self._after_action_result(error_msg_for_llm, abort=True)
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

        # The step row carries the fetched content in its expanded body; a
        # separate "Web Content" card only repeated what the step already says.
        if self._active_step is not None:
            self._finish_active_step(
                title=title, output=response_text, expanded=False)
        else:
            self.ui.add_message({
                'type': 'collapsible',
                'title': title,
                'content': response_text
            })

        if hasattr(self.controller.llm_worker, 'log_tool_response'):
            self.controller.llm_worker.log_tool_response(action_name, response_text)

        output_for_llm = f"**Tool Result:**\n```\n{response_text}\n```"
        self.record_tool_result(output_for_llm, name=action_name)
        self._after_action_result(output_for_llm)

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

        elif tool_name in ('mcp_list', 'mcp_get', 'mcp_call'):
            text = str(result)
            server = str(args.get('server') or '')
            tool = str(args.get('tool') or '')
            target = f"{server}/{tool}" if server and tool else (server or tool)
            if tool_name == 'mcp_list':
                title = f"MCP tools · {target}" if target else "MCP servers"
            elif tool_name == 'mcp_get':
                title = f"MCP tool definition · {target}" if target else "MCP tool definition"
            else:
                title = f"MCP result · {target}" if target else "MCP result"
            output_for_llm = f"**Tool Result:**\n```\n{text}\n```"
            output_for_ui = {
                'type': 'collapsible',
                'title': title,
                'content': text,
                'plain_content': text,
                'copy_payloads': {'output': text},
            }

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
        job_id = _running_job_id(result)
        title, _detail, plain, log_path = self._ui_result_fields(output_for_ui)
        self.record_tool_result(
            output_for_llm, name=self._pending_action_name,
            display=plain or '', display_title=title or '',
            job_id=(job_id if self._pending_action_name == 'execute_shell'
                    else None),
            log_path=log_path or '')
        self._show_activity_result(output_for_llm, output_for_ui, launch_job_id=job_id)
        if job_id is not None and self._pending_action_name == 'execute_shell':
            if self._active_step is not None and self._activity_msg is not None:
                self._job_steps[int(job_id)] = (self._activity_msg, self._active_step)
                if len(self._job_steps) > 200:
                    self._job_steps.pop(next(iter(self._job_steps)))
            # Background launch: do NOT ask the model immediately (it acts
            # NOW — e.g. relaunches). Record the launch, wait handler-side.
            # A readiness probe already spent the startup window, so its
            # verdict is final and the model gets control immediately.
            # An explicit poll is different: its result is already current,
            # so waiting again here only stalls the model by 10 seconds.
            self.command_history.append(output_for_llm)
            if isinstance(result, dict) and result.get('readiness_checked'):
                self._after_action_result(output_for_llm)
                return
            self._arm_job_wait(job_id)
            return
        self._after_action_result(output_for_llm)

    @staticmethod
    def _ui_result_fields(output_for_ui):
        """Step-row fields of a formatted UI result.

        Returns (title, detail_html, plain_text, log_path). plain_text is what
        the journal keeps for replay: it is human-facing and never contains
        the model-facing wrapper markers.
        """
        if not isinstance(output_for_ui, dict):
            return None, None, None, None
        title = output_for_ui.get('title') or None
        detail_html = None
        if output_for_ui.get('content_is_html'):
            detail_html = output_for_ui.get('content')
        plain = output_for_ui.get('plain_content')
        if plain is None and detail_html is None:
            content = output_for_ui.get('content')
            plain = None if content is None else str(content)
        return title, detail_html, plain, output_for_ui.get('open_log_path')

    def _show_activity_result(self, output_for_llm, output_for_ui, launch_job_id=None):
        """Folds a finished tool result into the active step row."""
        step = self._active_step
        if step is None:
            if output_for_ui:
                self.ui.add_message(output_for_ui)
            return
        title, detail_html, _plain, log_path = self._ui_result_fields(output_for_ui)
        failed = _is_error_output(output_for_llm)
        # Only a launching execute_shell keeps its step live until the job
        # ends (stop link + elapsed); polls and foreground steps finish now.
        running_job = launch_job_id is not None and not failed
        status = 'running' if running_job else ('error' if failed else 'ok')
        output_text = None
        if detail_html is None:
            output_text = self._strip_result_prefix(output_for_llm)
        self._update_activity_step(
            step, status=status, output=output_text, detail_html=detail_html,
            title=title, job_id=launch_job_id, log_path=log_path,
            expanded=True if failed else None)

    @staticmethod
    def _job_outcome(final: dict, stopped: bool = False) -> str:
        if stopped or final.get('cancelled'):
            return 'stopped'
        if final.get('timed_out') or final.get('exit_code') not in (0, None):
            return 'error'
        return 'ok'

    def _apply_job_ui(self, job_id, output_for_ui, status: str = "ok"):
        """Updates the job's launch step; falls back to a result card when
        the step is unknown (e.g. a job launched before a restart)."""
        try:
            entry = self._job_steps.get(int(job_id))
        except (TypeError, ValueError):
            entry = None
        if entry is None:
            self._show_job_result(job_id, output_for_ui)
            return
        msg, row = entry
        title, detail_html, _plain, log_path = self._ui_result_fields(output_for_ui)
        try:
            self.ui.activity_update_step(
                msg, row, status=status, title=title,
                detail_html=detail_html, log_path=log_path,
                expanded=True if status == 'error' else None)
        except Exception as e:
            logging.warning(f"Job step update failed for #{job_id}: {e}")
            self._show_job_result(job_id, output_for_ui)

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
        if self._active_step is not None:
            self._finish_active_step(
                status="error", output=self._strip_result_prefix(error_msg),
                expanded=True)
        else:
            self._add_result_card(
                error_msg, f"{self._pending_action_name} · error", "error")
        self.record_tool_result(
            error_msg, status="error", name=self._pending_action_name)
        self._after_action_result(error_msg, abort=True)

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
        if job_id not in self._job_steps and job_id not in self._job_commands:
            # Foreground execute_shell calls also run as ShellJobManager jobs.
            # Their result already folds into the step row in _on_tool_finished;
            # treating this event as a background job would add a duplicate
            # "Background job #N" card and a stale history note.
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

        job_status = self._job_outcome(
            final, stopped=(event.get("type") == "stopped"))

        if output_for_ui:
            self._apply_job_ui(job_id, output_for_ui, job_status)

        if job_id in self._handled_job_ids:
            return
        self._mark_job_handled(job_id)
        title, _detail, plain, log_path = self._ui_result_fields(output_for_ui)
        self._append_session_event(
            "job_result",
            turn_id=getattr(self.controller, "session_turn_id", ""),
            job_id=job_id,
            name=tool_name,
            status=job_status,
            exit_code=final.get("exit_code"),
            output={"text": str(output_for_llm or "")},
            display=plain or "",
            display_title=title or "",
            log_path=log_path or "",
        )

        if (self._awaiting_job_id == job_id
                and self.state == VisionState.EXECUTING
                and not self._tool_in_flight):
            self.cancel_job_wait()
            logging.info(f"TerminalHandler: job #{job_id} finished; waking loop.")
            self._after_action_result(output_for_llm, from_job_wait=True)
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
                self._apply_job_ui(
                    job_id_arg, output_for_ui, self._job_outcome(final))
            self._after_action_result(output_for_llm, from_job_wait=True)
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
            output_for_llm, _ = self._format_result_for_llm_and_ui(
                "poll_shell_job", current, {"job_id": job_id}
            )
            # The launch step already shows the job and its Stop link; a
            # second snapshot block would just repeat it in chat.
            self._after_action_result(output_for_llm, from_job_wait=True)
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

        if self.web_thread and self.web_thread.isRunning():
            self.web_thread.cancel()
