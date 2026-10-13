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
from PySide6.QtCore import Slot, QThread, QTimer

from states import VisionState
from ui.terminal_html_formatter import TerminalHtmlFormatter
from src.computer.computer import Computer

from .base_agent import BaseAgentHandler
from .tool_exec_worker import ToolExecRelay, ToolExecWorker

# Background-job await: after a launch the loop waits handler-side instead
# of asking the model immediately (it would act NOW, e.g. relaunch). Cheap
# status polls run on this timer; the model is called back only when the
# job finishes or after MAX rounds with fresh state.
JOB_WAIT_INTERVAL_MS = 15000
JOB_WAIT_MAX_ROUNDS = 20


def _running_job_id(result) -> int | None:
    """Job id when a tool result is a live background handle, else None."""
    if isinstance(result, dict) and result.get('status') == 'running' and 'job_id' in result:
        try:
            return int(result['job_id'])
        except Exception:
            return None
    return None

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

                # --- PLAN A: Native Provider Search ---
                try:
                    logging.info(f"Plan A: Attempting to use native web search from provider '{self.provider.get_provider_id()}'.")
                    final_response = self.provider.web_search(query=query)
                except Exception as e:
                    logging.warning(f"Plan A (native search) from '{self.provider.get_display_name()}' failed: {e}. Proceeding to Plan B.")
                    self._relay.web_plan_a_failed.emit(self.provider.get_display_name())
                    final_response = None

                if self._is_cancelled:
                    return

                # --- PLAN B: Local Execution (if Plan A failed) ---
                if final_response is None:
                    logging.info("Plan B: Using local web search fallback.")
                    final_response = self.computer.web_search(query=query)

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
        self._pending_action_name = None
        self._pending_action_args = None

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
        history = self.command_history
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

        if self.active_edit_path and (not is_file_op or path != self.active_edit_path):
            self.ui.add_message(self.html_formatter.format_container_end())
            self.active_edit_path = None

        if is_file_op:
            if not self.active_edit_path:
                self.active_edit_path = path
                self.ui.add_message(self.html_formatter.format_container_start(path))

            if action_name == 'write_file':
                content = action_args.get('content', '')
                self.ui.add_message(self.html_formatter.format_write_chunk(content))

            elif action_name in ['replace', 'replace_many']:
                replacements = [action_args] if action_name == 'replace' else action_args.get('replacements', [])
                try:
                    with open(path, encoding='utf-8') as f:
                        full_content = f.read()
                except Exception:
                    full_content = ""

                for replacement in replacements:
                    old_string = replacement.get('old_string', '')
                    new_string = replacement.get('new_string', '')
                    old_lines, new_lines = old_string.split('\n'), new_string.split('\n')
                    dedented_all = self._dedent_lines(old_lines + new_lines)
                    dedented_old, dedented_new = dedented_all[:len(old_lines)], dedented_all[len(old_lines):]
                    index = full_content.find(old_string)
                    start_line = full_content.count('\n', 0, index) + 1 if index != -1 else -1
                    self.ui.add_message(self.html_formatter.format_replace_chunk(dedented_old, dedented_new, start_line))
        else:
            # Handle extensive arguments for UI for non-file-op commands
            if len(json.dumps(action_args)) > 100: # Arbitrary threshold for 'extensive'
                self.ui.add_message(f"<b>Executing:</b> {action_name}")
                self.ui.add_message({
                    'type': 'collapsible',
                    'title': "Arguments",
                    'content': json.dumps(action_args, indent=2)
                })
            else:
                command_str_for_ui = f"`{action_name}({json.dumps(action_args)})`"
                self.ui.add_message(f"<b>Executing:</b> {command_str_for_ui}")
        # ---------------------------------------------

        self._ensure_exec_thread()
        self._pending_action_name = action_name
        self._pending_action_args = action_args
        try:
            self.computer.cancel_event.clear()
        except Exception:
            pass
        self._exec_ticket += 1
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
            output_for_llm = (
                f"**Tool Result:** Background shell job #{job_id} started and is running.\n"
                f"Poll it with poll_shell_job, pacing polls with wait_seconds.\n"
                f"```\n{result}\n```"
            )
            output_for_ui = f"<i>Background shell job #{job_id} started.</i>"
            return output_for_llm, output_for_ui

        if tool_name == 'execute_shell':
            stdout = result.get('stdout', '')
            stderr = result.get('stderr', '')
            exit_code = result.get('exit_code', -1)
            output_for_llm = (
                f"**Command Result:** (Exit Code: {exit_code})\n"
                f"**STDOUT:**\n```\n{stdout or '(empty)'}\n```\n"
                f"**STDERR:**\n```\n{stderr or '(empty)'}\n```"
            )
            title = f"Shell Command Output (Exit Code: {exit_code})"
            content = f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            output_for_ui = {'type': 'collapsible', 'title': title, 'content': content}

        elif tool_name in ['write_file', 'replace', 'replace_many']:
            output_for_llm = f"**Tool Result:**\n```\n{result}\n```"
            output_for_ui = f"<i>{html.escape(str(result))}</i>"

        elif tool_name == 'read_files':
            paths = args.get('paths', [])
            title = f"Content of {len(paths)} file(s)"
            output_for_llm = f"**Tool Result:**\n```\n{result}\n```"
            output_for_ui = {
                'type': 'collapsible',
                'title': title,
                'content': str(result)
            }

        elif tool_name in ['delete_files', 'move_files', 'copy_files']:
            output_for_llm = f"**Tool Result:**\n```\n{result}\n```"
            output_for_ui = {
                'type': 'collapsible',
                'title': f"Operation Result: {tool_name.replace('_', ' ').title()}",
                'content': str(result)
            }

        elif tool_name == 'search_file_content':
            query = args.get('pattern', 'N/A')
            path = args.get('path', '.')
            title = f"Search Results for '{query}' in '{path}'"
            content = "\n".join(result) if isinstance(result, list) else str(result)
            output_for_llm = f"**Tool Result:**\n```\n{content}\n```"
            if result:
                 output_for_ui = {'type': 'collapsible', 'title': title, 'content': content}
            else:
                 output_for_ui = "<i>(No results found)</i>"

        elif tool_name == 'list_directory':
            path = args.get('path', '.')
            title = f"Directory Listing for: {path}"
            content = "\n".join(result) if isinstance(result, list) else str(result)
            output_for_llm = f"**Tool Result:**\n```\n{content}\n```"
            if result:
                output_for_ui = {'type': 'collapsible', 'title': title, 'content': content}
            else:
                output_for_ui = f"<i>(Directory '{path}' is empty or not found)</i>"

        else:
            # Generic fallback for any other tool
            str_result = str(result)
            output_for_llm = f"**Tool Result:**\n```\n{str_result}\n```"
            output_for_ui = {'type': 'collapsible', 'title': tool_name, 'content': str_result}

        return output_for_llm, output_for_ui

    def _ensure_exec_thread(self):
        if not self._exec_thread.isRunning():
            self._exec_thread.start()

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
        self.ui.show_tool_progress(
            ticket,
            f"⏳ <b>{html.escape(str(self._pending_action_name))}</b>{pct} · {elapsed:.0f}s{tail_html}"
        )

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
        if output_for_ui:
            self.ui.add_message(output_for_ui)
        job_id = _running_job_id(result)
        if job_id is not None:
            # Background launch: do NOT ask the model immediately (it acts
            # NOW — e.g. relaunches). Record the launch, wait handler-side.
            self.command_history.append(output_for_llm)
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
        self._continue_loop(error_msg)

    def cancel_job_wait(self):
        try:
            self._job_wait_timer.stop()
        except Exception:
            pass
        self._awaiting_job_id = None
        self._job_wait_rounds = 0

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
                self.ui.add_message(output_for_ui)
            self._continue_loop(output_for_llm, from_job_wait=True)
            return
        self._job_wait_rounds += 1
        if self._job_wait_rounds >= JOB_WAIT_MAX_ROUNDS:
            self._awaiting_job_id = None
            logging.info(f"TerminalHandler: job #{job_id} still running after "
                         f"{JOB_WAIT_MAX_ROUNDS} waits; handing control back.")
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
        if self.active_edit_path:
            self.ui.add_message(self.html_formatter.format_container_end())
            self.active_edit_path = None

        if self.web_thread and self.web_thread.isRunning():
            self.web_thread.cancel()
