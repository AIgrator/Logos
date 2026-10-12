"""
Handles the browser mode: an iterative agent loop over Playwright tools.

Same shape as TerminalHandler (thought -> single action -> observe),
but the tools drive a real Chromium window instead of a shell.
No desktop screenshots are involved: grounding comes from the DOM snapshot.
"""
import json
import logging
import platform

from states import VisionState
from src.computer.computer import Computer
from src.executor.executor import Executor


class BrowserHandler:
    def __init__(self, controller, working_directory=None):
        self.controller = controller
        self.computer = Computer(mode='browser', working_directory=working_directory)
        self.ui = controller.ui
        self.settings = controller.settings
        self.brain = controller.brain
        self.state = VisionState.IDLE
        self.pre_pause_state = None
        self.original_user_request = None
        self.os_info = f"{platform.system()} {platform.release()}"
        self.command_history = []
        self.executor = Executor(self.computer, ui=None)

    # -- lifecycle ------------------------------------------------------

    def _set_state(self, new_state: VisionState):
        if self.state != new_state:
            logging.info(f"BrowserHandler state changed from {self.state.name} to {new_state.name}")
            self.state = new_state

    def _start_thinking_timer(self, message: str):
        self.ui.show_thinking_ui(True, message=message, actor=self.controller._get_current_model_name())
        self.controller.thinking_time = 0
        self.controller.thinking_timer.start(1000)

    def _stop_thinking_timer(self):
        self.ui.show_thinking_ui(False)
        self.controller.thinking_timer.stop()

    def handle_response_chunk(self, chunk):
        # Browser mode is non-streaming; chunks are ignored.
        pass

    def stop(self):
        """Stops the current task, closes the browser, resets state."""
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is not None:
                mgr.close()
                self.computer.browser_manager = None
        except Exception:
            pass
        if self.state != VisionState.IDLE:
            logging.info("BrowserHandler stopped.")
            self._set_state(VisionState.IDLE)
            self._stop_thinking_timer()
            self.original_user_request = None
            self.command_history = []

    # -- main loop --------------------------------------------------------

    def pause_or_resume(self):
        if self.state == VisionState.PAUSED:
            self._set_state(VisionState.EXECUTING)
            self.ui.add_message("<i>Execution resumed.</i>")
            self.ui.set_paused(False)
            self._continue_loop()
        elif self.state == VisionState.EXECUTING:
            self.pre_pause_state = self.state
            self._set_state(VisionState.PAUSED)
            if self.controller.llm_worker:
                self.controller.llm_worker.cancel()
            self.controller.thinking_timer.stop()
            self.ui.show_thinking_ui(False)
            self.ui.add_message("<i>Execution paused. Inspect the browser, then press Resume or type guidance to continue.</i>")
            self.ui.set_paused(True)

    def start_task(self, user_text: str):
        """Starts a new task in browser mode."""
        if self.state == VisionState.PAUSED_ON_ERROR:
            self._set_state(VisionState.EXECUTING)
            output_for_llm = f"**User Response:** {user_text}"
            self._continue_loop(last_command_output=output_for_llm)
            return

        if self.state in (VisionState.EXECUTING, VisionState.PAUSED):
            # Free-text hint mid-task: keep history, steer the next step.
            # (A fresh task starts only from IDLE, e.g. after Stop.)
            if self.controller.llm_worker:
                self.controller.llm_worker.cancel()
            self.controller.thinking_timer.stop()
            self.ui.show_thinking_ui(False)
            self._set_state(VisionState.EXECUTING)
            self.ui.set_paused(False)
            self._continue_loop(last_command_output=f"**User guidance:** {user_text}")
            return

        logging.info(f"BrowserHandler starting task: {user_text}")
        self.original_user_request = user_text
        self.command_history = []
        self._set_state(VisionState.EXECUTING)
        self._continue_loop()

    def current_url(self) -> str:
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is not None and getattr(mgr, "_page", None) is not None:
                return mgr.url()
        except Exception:
            pass
        return "not open yet"

    def _browser_ctx(self) -> dict:
        """Execution context for the Executor (kept for parity)."""
        return {"shell_allowed": False}

    def _ensure_browser_manager(self):
        """Create and attach the shared BrowserManager on first use."""
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

    def _continue_loop(self, last_command_output=None):
        if self.state in (VisionState.IDLE, VisionState.PAUSED):
            return

        self._start_thinking_timer("Thinking...")

        if last_command_output:
            self.command_history.append(last_command_output)

        system_prompt, user_prompt = self.brain.get_browser_prompts(
            user_request=self.original_user_request,
            os_info=self.os_info,
            history=self.command_history,
            current_url=self.current_url(),
        )

        if not system_prompt or not user_prompt:
            self.ui.add_message("<i>Error: Could not load browser prompts.</i>")
            self.stop()
            return

        self.controller._send_llm_request(
            user_prompt,
            self._consume_pending_screenshot(),
            system_prompt,
            [],
            "browser_tool_code",
        )

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

    def handle_llm_response(self, reasoning: str, payload: any, schema_name: str):
        """Handles the response from the LLM, which should be a tool call."""
        if self.state == VisionState.PAUSED:
            logging.info("Browser response arrived while paused; ignoring.")
            return
        self._stop_thinking_timer()

        if reasoning:
            self.ui.add_message(f"<b>{self.controller._get_current_model_name()}:</b> {reasoning}")

        if schema_name != 'browser_tool_code' or not payload or 'action' not in payload:
            self.ui.add_message("<i>Received an unexpected response from the model. Cannot proceed.</i>")
            self.stop()
            return

        action = payload.get('action')
        if not action or 'name' not in action:
            self.ui.add_message("<i>The model's plan was invalid. Cannot proceed.</i>")
            self.stop()
            return

        self._execute_action(action)

    def _execute_action(self, action: dict):
        action_name = action.get('name')
        action_args = action.get('arguments', {})
        if not isinstance(action_args, dict):
            action_args = {}

        if action_name == 'finish':
            message = action_args.get('message', 'Task completed successfully.')
            self.ui.add_formatted_message(message, prefix_html="<b>Task Finished:</b> ")
            self.stop()
            return

        if action_name == 'display_message':
            message = action_args.get('message', '...')
            prefix = f"<b>{self.controller._get_current_model_name()}:</b> "
            self.ui.add_formatted_message(message, prefix_html=prefix)
            self._continue_loop(f"**Tool Result:** Message displayed to user: {message}")
            return

        if action_name == 'communicate_to_user':
            message = action_args.get('message', 'The assistant needs your input.')
            prefix = f"<b>{self.controller._get_current_model_name()}:</b> "
            self.ui.add_formatted_message(message, prefix_html=prefix)
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return

        command_str_for_llm = f"`{action_name}({json.dumps(action_args, ensure_ascii=False)})`"
        self.command_history.append(f"**Agent Command:** {command_str_for_llm}")
        self.ui.add_message(f"<b>Executing:</b> {action_name}")
        self.ui.add_message({
            'type': 'collapsible',
            'title': "Arguments",
            'content': json.dumps(action_args, indent=2, ensure_ascii=False),
        })

        # Lazy browser start: tools expect computer.browser_manager/factory.
        if action_name.startswith("browser_") and action_name != "browser_close":
            try:
                self._ensure_browser_manager()
            except Exception as e:
                logging.error(f"Failed to start browser: {e}", exc_info=True)
                self._continue_loop(f"**Tool Result:** Error: could not start the browser: {e}")
                return

        try:
            result = self.executor.execute(
                {"name": action_name, "arguments": action_args}, self._browser_ctx())
        except ValueError as e:
            # Instructional error: teach the model and continue the loop.
            self._continue_loop(f"**Tool Result:** Error: {e}")
            return
        except Exception as e:  # noqa: BLE001 - must not break the loop
            logging.error(f"Error executing browser action: {e}", exc_info=True)
            self._continue_loop(f"**Tool Result:** Error: {e}")
            return

        if not result.ok:
            self._continue_loop(f"**Tool Result:** Error: {result.error}")
            return

        output = result.output_text or "(ok, no output)"
        self.ui.add_message({
            'type': 'collapsible',
            'title': f"Browser Result: {action_name}",
            'content': output[-4000:],
        })
        self._continue_loop(f"**Tool Result:**\n```\n{output}\n```")
