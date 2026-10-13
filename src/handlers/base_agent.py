"""
Unified agent loop (shared mechanism for terminal/browser/vision-style modes).

Mirrors the provider pattern (BaseLLMProvider + concrete providers): this
base class owns ONLY the loop mechanics — states, single-step
plan -> execute -> observe cycle, pause/resume/stop, user communication,
loop-awareness notes. Everything mode-specific (prompts, tools, execution,
UI rendering) lives in the concrete heirs.

Contract for heirs:
  - MODE_NAME: str, SCHEMA_NAME: str|None class attributes.
  - build_prompts() -> (system_prompt, user_prompt) | (None, None).
  - execute_action(action: dict) -> str | None: run the tool(s), show any
    mode-specific UI, and return the history text for the LLM. Return None
    when completion is asynchronous (the heir continues the loop itself
    once the result arrives, e.g. terminal web requests).
  - _cleanup_task(): heir hook called by stop() (close browsers, kill
    threads, end UI containers). Default does nothing.
"""
import logging

from states import VisionState


class BaseAgentHandler:
    MODE_NAME = "agent"
    SCHEMA_NAME = None

    def __init__(self, controller):
        self.controller = controller
        self.ui = controller.ui
        self.settings = controller.settings
        self.brain = controller.brain
        self.state = VisionState.IDLE
        self.pre_pause_state = None
        self.original_user_request = None
        self.command_history = []
        self._stashed_response = None  # LLM answer arrived while paused

    # -- shared state machine ------------------------------------------

    def _set_state(self, new_state: VisionState):
        if self.state != new_state:
            logging.info(f"{type(self).__name__} state changed from {self.state.name} to {new_state.name}")
            self.state = new_state
            try:
                updater = getattr(self.ui, "update_pause_button", None)
                if callable(updater):
                    updater()
            except Exception:
                pass

    def _start_thinking_timer(self, message: str):
        self.ui.show_thinking_ui(True, message=message, actor=self.controller._get_current_model_name())
        self.controller.thinking_time = 0
        self.controller.thinking_timer.start(1000)

    def _stop_thinking_timer(self):
        self.ui.show_thinking_ui(False)
        self.controller.thinking_timer.stop()

    # -- task lifecycle (shared) ----------------------------------------

    def start_task(self, user_text: str):
        """Starts a new task, resumes a paused one, or steers a running one."""
        if self.state == VisionState.PAUSED_ON_ERROR:
            self._set_state(VisionState.EXECUTING)
            self.controller._reenter_agent_mode(self)
            self._continue_loop(last_command_output=f"**User Response:** {user_text}")
            return

        if self.state == VisionState.PAUSED:
            # Paused: record the text, but do NOT resume and do NOT send
            # anything to the model. Execution continues only via Resume.
            self.command_history.append(f"**User note (paused):** {user_text}")
            self.ui.add_message("<i>Noted, staying paused. Press Resume to continue.</i>")
            return

        if self.state == VisionState.EXECUTING:
            # Free-text hint mid-task: keep history, steer the next step.
            # (A fresh task starts only from IDLE, e.g. after Stop.)
            # Fresh guidance supersedes any stashed paused response.
            self._stashed_response = None
            if self.controller.llm_worker:
                self.controller.llm_worker.cancel()
            self.controller.thinking_timer.stop()
            self.ui.show_thinking_ui(False)
            self._set_state(VisionState.EXECUTING)
            self.ui.set_paused(False)
            self._continue_loop(last_command_output=f"**User guidance:** {user_text}")
            return

        logging.info(f"{type(self).__name__} starting task: {user_text}")
        self.original_user_request = user_text
        # Do not wipe history if already populated (e.g. attachments).
        if not self.command_history:
            self.command_history = []
        self._set_state(VisionState.EXECUTING)
        self._continue_loop()

    def pause_or_resume(self):
        logging.info(f"{type(self).__name__} pause button pressed in state {self.state.name}.")
        if self.state == VisionState.PAUSED:
            # Pick up an unsent draft as context (no extra request), then
            # resume plainly. Resume itself never sends anything to the model.
            # The draft is shown as a normal You-message, like any sent text.
            draft = self.ui.user_input.toPlainText().strip()
            if draft:
                self.ui.add_message(f"<b>You:</b> {draft}")
                self.controller.conversation_history.append({'role': 'user', 'content': draft})
                self.command_history.append(f"**User note (paused):** {draft}")
                self.ui.user_input.clear()
            self._set_state(VisionState.EXECUTING)
            self.controller._reenter_agent_mode(self)
            self.ui.add_message("<i>Execution resumed.</i>")
            self.ui.set_paused(False)
            stashed, self._stashed_response = self._stashed_response, None
            if stashed is not None:
                reasoning, payload, schema_name = stashed
                logging.info(f"{type(self).__name__} resuming with stashed LLM response.")
                self.handle_llm_response(reasoning, payload, schema_name)
            else:
                self._continue_loop()
            return
        if self.state == VisionState.EXECUTING:
            self.pre_pause_state = self.state
            self._set_state(VisionState.PAUSED)
            # NOTE: in-flight work is deliberately NOT cancelled: a running
            # tool or request finishes on its own, the loop just sends
            # nothing new until Resume. History keeps being recorded.
            self.ui.add_message("<i>Execution paused after the current step. "
                                "Chat freely — actions stay frozen until Resume.</i>")
            self.ui.set_paused(True)
            return
        logging.info(f"{type(self).__name__} pause button ignored in state {self.state.name} (nothing pausable).")

    def stop(self):
        """Stops the current task and resets state. Heirs clean resources via hook."""
        was_active = self.state != VisionState.IDLE
        try:
            self._cleanup_task()
        except Exception as e:
            logging.warning(f"{type(self).__name__} task cleanup failed: {e}")
        if was_active:
            logging.info(f"{type(self).__name__} stopped.")
        self._set_state(VisionState.IDLE)
        self._stop_thinking_timer()
        self.original_user_request = None
        self.command_history = []

    def _cleanup_task(self):
        """Heir hook: release mode resources (browsers, threads, UI containers)."""

    # -- unified single-step loop ----------------------------------------

    def _continue_loop(self, last_command_output=None):
        """
        Plan one action -> execute -> observe -> repeat.
        Evaluation is implicit: the next planning turn sees the result
        (plus loop-awareness notes) and corrects course itself.
        """
        if last_command_output:
            self.command_history.append(last_command_output)

        if self.state in (VisionState.IDLE, VisionState.PAUSED):
            return

        self._start_thinking_timer("Thinking...")

        loop_note = self._repetition_note()
        if loop_note:
            self.command_history.append(loop_note)

        system_prompt, user_prompt = self.build_prompts()
        if not system_prompt or not user_prompt:
            self.ui.add_message("<i>Error: Could not load prompts.</i>")
            self.stop()
            return

        self.controller._send_llm_request(
            user_prompt,
            self._request_attachments(),
            system_prompt,
            [],
            self.SCHEMA_NAME,
        )

    def _request_attachments(self) -> list:
        """Extra payloads for the next request (images, files). Default: none."""
        return []

    def handle_llm_response(self, reasoning: str, payload: any, schema_name: str):
        """Handles the response from the LLM, which should be a tool call."""
        if self.state == VisionState.PAUSED:
            logging.info(f"{type(self).__name__} LLM response arrived while paused; stashing.")
            self._stashed_response = (reasoning, payload, schema_name)
            self._stop_thinking_timer()
            return
        self._stop_thinking_timer()

        if reasoning:
            self.ui.add_message(f"<b>{self.controller._get_current_model_name()}:</b> {reasoning}")

        if schema_name != self.SCHEMA_NAME or not payload or 'action' not in payload:
            self.ui.add_message("<i>Received an unexpected response from the model. Cannot proceed.</i>")
            self.stop()
            return

        action = payload.get('action')
        if not action or 'name' not in action:
            self.ui.add_message("<i>The model's plan was invalid. Cannot proceed.</i>")
            self.stop()
            return

        if action.get('name') == 'finish':
            message = (action.get('arguments') or {}).get('message', 'Task completed successfully.')
            self.ui.add_formatted_message(message, prefix_html="<b>Task Finished:</b> ")
            self.stop()
            return

        if action.get('name') == 'communicate_to_user':
            message = (action.get('arguments') or {}).get('message', 'The assistant needs your input.')
            prefix = f"<b>{self.controller._get_current_model_name()}:</b> "
            self.ui.add_formatted_message(message, prefix_html=prefix)
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return

        if action.get('name') == 'display_message':
            message = (action.get('arguments') or {}).get('message', '...')
            prefix = f"<b>{self.controller._get_current_model_name()}:</b> "
            self.ui.add_formatted_message(message, prefix_html=prefix)
            self._continue_loop(f"**Tool Result:** Message displayed to user: {message}")
            return

        self._run_action(action)

    def handle_response_chunk(self, chunk):
        # Agent modes are non-streaming; stray chunks (e.g. a chat stream
        # that outlived a mode switch) are ignored instead of crashing.
        pass

    def _run_action(self, action: dict):
        import json
        action_name = action.get('name')
        action_args = action.get('arguments', {})
        if not isinstance(action_args, dict):
            action_args = {}
            action['arguments'] = action_args
        command_str_for_llm = f"`{action_name}({json.dumps(action_args, ensure_ascii=False)})`"
        self.command_history.append(f"**Agent Command:** {command_str_for_llm}")
        try:
            result_text = self.execute_action(action)
        except ValueError as e:
            # Instructional error: the message teaches the correct usage.
            result_text = f"**Tool Result:** Error: {e}"
        except Exception as e:  # noqa: BLE001 - must not break the loop
            logging.error(f"Error executing {self.MODE_NAME} action: {e}", exc_info=True)
            result_text = f"**Tool Result:** Error: {e}"
        if result_text is None:
            return  # async heir continues the loop itself on completion
        self._continue_loop(result_text)

    # -- loop awareness (facts for the model; it decides) ------------------

    def _repetition_note(self) -> str | None:
        """Detects mechanical repetition; the MODEL decides if it is stuck.

        Returns a history line or None. Skipped if already the last line.
        """
        hist = self.command_history
        if hist and hist[-1].startswith("Loop signal:"):
            return None
        commands = [h for h in hist if h.startswith("**Agent Command:**")]
        if len(commands) >= 3 and commands[-1] == commands[-2] == commands[-3]:
            return ("Loop signal: you issued the identical action 3 times in a row. "
                    "If there is no progress, stop and call communicate_to_user.")
        results = [h for h in hist if h.startswith("**Tool Result:**")
                   or h.startswith("**Command Result:**") or h.startswith("**Error:**")]
        if len(results) >= 3 and results[-1] == results[-2] == results[-3]:
            return ("Loop signal: the last 3 tool results are identical. "
                    "If there is no progress, stop and call communicate_to_user.")
        return None

    # -- heir contract -----------------------------------------------------

    def build_prompts(self):
        """Returns (system_prompt, user_prompt) for the next planning turn."""
        raise NotImplementedError

    def execute_action(self, action: dict):
        """Runs the tool(s); shows mode UI; returns history text (None if async)."""
        raise NotImplementedError
