"""
Handles the logic for the vision mode, including planning and execution.
Implements a "Task-Evaluation" loop with self-correction capabilities.
"""
import logging
import time
import os
from collections import namedtuple
from PySide6.QtCore import QTimer, QMetaObject
import platform
from PIL import Image

from states import VisionState
from src.computer.computer import Computer
# Heavy/slow imports are deferred to the methods that use them:
# - numpy as np
# - vision.annotator.IntentAnnotator
# - actions


class VisionHandler:
    MAX_ACTION_HISTORY = 3
    UI_UPDATE_DELAY_MS = 100       # 0.1 seconds

    def __init__(self, controller):
        self.controller = controller
        self.computer = Computer(
            settings_manager=getattr(controller, 'settings_manager', None),
            mode='vision',
            working_directory=getattr(controller, 'working_directory', None),
        )
        from src.executor.executor import Executor
        self.executor = Executor(self.computer, ui=None)  # ui wired below
        self.executor.ui = controller.ui
        self.ui = controller.ui
        self.settings = controller.settings
        self.brain = controller.brain
        self.conversation_history = controller.conversation_history

        # Load configurable settings
        vision_settings = self.settings.get("vision", {})
        self.WAIT_CHECK_INTERVAL_MS = vision_settings.get("wait_check_interval_ms", 5000)
        self.POST_ACTION_DELAY_MS = vision_settings.get("post_action_delay_ms", 1000)
        self.MAX_CORRECTION_ATTEMPTS = vision_settings.get("max_correction_attempts", 3)

        self.state = VisionState.IDLE
        self.pre_pause_state = None
        self.action_plan = []
        from collections import deque
        self.recent_action_history = deque(maxlen=self.MAX_ACTION_HISTORY)
        self.original_user_request = None
        self.original_plan = None
        self.current_task_index = 0
        self.last_click_position = None
        self.is_replan_flow = False
        self.replan_reason = None
        self.is_paused_for_guidance = False
        self.analysis_start_time = 0

        self.correction_attempts = 0

        self.action_timer = QTimer()
        self.action_timer.setSingleShot(True)

        self._evaluation_context = {}
        self.pre_action_screenshot = None
        self.pre_action_annotated_screenshot = None

        self.analysis_thread = None
        self.analysis_worker = None

        self.wait_timer = QTimer()
        self.wait_timer.setSingleShot(True)
        self.wait_timer.timeout.connect(self._perform_llm_wait_check)
        self.wait_start_time = 0
        self.wait_action = None

        from src.apps.launcher import CrossPlatformAppLauncher
        self.app_launcher = CrossPlatformAppLauncher()
        self.allowed_apps = self.settings.get('allowed_apps', [])
        self.installed_apps = [app.get('name') for app in self.allowed_apps if app.get('name')]

        self.screen_resolution = "Unknown"
        self.os_info = "Unknown"
        try:
            import pyautogui
            width, height = pyautogui.size()
            self.screen_resolution = f"{width}x{height}"
            self.os_info = f"{platform.system()} {platform.release()}"
        except Exception as e:
            logging.error(f"Failed to get screen resolution or OS info on init: {e}", exc_info=True)

    def _set_state(self, new_state: VisionState):
        logging.info(f"VisionHandler state changed from {self.state.name} to {new_state.name}")
        self.state = new_state

    def _focus_user_input_if_main_window_is_active(self):
        """Sets focus to the user input field if the main window is active."""
        if self.ui.main_window.isActiveWindow():
            self.ui.user_input.setFocus()

    def _start_thinking_timer(self, message: str):
        """Resets and starts the thinking timer with a specific message."""
        actor = "You" if message == "Analyzing screen..." else self.controller._get_current_model_name()
        self.ui.show_thinking_ui(True, message=message, actor=actor)
        self.controller.thinking_time = 0
        self.controller.thinking_timer.start(1000)

    def _stop_analysis_timer(self):
        """Stops the thinking timer after an analysis phase."""
        if self.analysis_start_time > 0:
            self.ui.show_thinking_ui(False)
            self.controller.thinking_timer.stop()
            self.analysis_start_time = 0

    def start_task(self, user_text: str, attachments: list):
        vision_manager = self.computer.get_vision_manager()
        if not vision_manager or not vision_manager.parser_initialized:
            self.ui.add_message("<i>The vision module is not yet available. Please wait a moment and try again.</i>")
            return

        if self.state == VisionState.WAITING_FOR_APPROVAL:
            self.ui.add_message("<i>Got it. Incorporating your feedback and regenerating the plan...</i>")
            self.is_replan_flow = True
            self.replan_reason = f"User provided feedback on the proposed plan: '{user_text}'"
        elif self.state in [VisionState.EXECUTING, VisionState.PAUSED, VisionState.EVALUATING, VisionState.WAITING_FOR_ELEMENT]:
            self.action_plan = []
            self.current_task_index = 0
            self.ui.add_message("<i>New instructions received. Stopping current plan and replanning...</i>", force_scroll=True)
            if self.ui.is_compact:
                self.ui.switchToFullMode()
            self.original_user_request = user_text
            self.original_plan = None
            self.correction_attempts = 0
        else:
            self.original_user_request = user_text
            self.original_plan = None
            self.correction_attempts = 0

        self._set_state(VisionState.PLANNING)

        self.analysis_start_time = time.time()
        self._start_thinking_timer("Analyzing screen...")

        # --- Capture Screenshot in Main Thread ---
        screenshot = self.computer.take_screenshot(self.ui.main_window)
        if not screenshot:
            self.ui.add_message("<i>Error: Could not capture a screenshot for analysis.</i>")
            self._set_state(VisionState.IDLE)
            return

        # --- Start Background Analysis ---
        self._start_background_analysis(screenshot)

    def _start_background_analysis(self, screenshot):
        """Helper method to start the vision analysis in a background thread."""
        from vision.analyzer import VisionAnalyzer
        from vision.analyzer_worker import AnalyzerWorker

        vision_manager = self.computer.get_vision_manager()
        analyzer = VisionAnalyzer(vision_manager, self.controller.settings_manager)

        self.analysis_thread = self.controller.thread_pool.get_thread()
        self.analysis_worker = AnalyzerWorker(analyzer, screenshot)
        self.analysis_worker.moveToThread(self.analysis_thread)

        from PySide6.QtCore import Qt
        # Connect worker signals to the controller's proxy signals
        self.analysis_worker.analysis_complete.connect(self.controller.analysis_finished, Qt.QueuedConnection)
        self.analysis_worker.error.connect(self.controller.analysis_failed, Qt.QueuedConnection)

        # Asynchronously invoke the worker's run method in its thread's event loop.
        if not self.analysis_thread.isRunning():
            self.analysis_thread.start()
        QMetaObject.invokeMethod(self.analysis_worker, "run", Qt.QueuedConnection)

    def _on_analysis_error(self, error_message):
        self.ui.add_message(f"<i>Error during screen analysis: {error_message}</i>")
        self._set_state(VisionState.IDLE)
        self.controller.thread_pool.release_thread(self.analysis_thread)

    def _on_analysis_complete(self, clean_screenshot, annotated_frame, parsed_content):
        """
        Dispatcher for handling the results of a background screen analysis.
        Routes the results to the appropriate handler based on the current state.
        """
        if self.state == VisionState.PLANNING:
            self._handle_planning_analysis_result(clean_screenshot, annotated_frame, parsed_content)
        elif self.state == VisionState.EVALUATING:
            self._on_evaluation_analysis_complete(clean_screenshot, annotated_frame, parsed_content)
        else:
            logging.warning(f"Analysis completed in an unexpected state: {self.state.name}. Ignoring.")
            self.controller.thread_pool.release_thread(self.analysis_thread)

    def _handle_planning_analysis_result(self, clean_screenshot, _, parsed_content):
        """Handles the analysis result when the assistant is in the PLANNING state."""
        self._stop_analysis_timer()

        failure_context = self.replan_reason if self.is_replan_flow else None
        plan_status = self._generate_plan_status_str() if self.is_replan_flow else None

        # Reset the replan flag after using it
        self.is_replan_flow = False
        self.replan_reason = None

        self.controller.thread_pool.release_thread(self.analysis_thread)

        if not clean_screenshot:
            self.ui.add_message("<i>Error: Could not get a screenshot for analysis.</i>")
            self._set_state(VisionState.IDLE)
            return

        clean_screenshot_bytes = self.computer.compress_image_to_bytes(clean_screenshot)
        if clean_screenshot_bytes:
            self.controller.pending_attachments = [clean_screenshot_bytes]

        self._start_thinking_timer("Planning...")

        parsed_content_str = "\n".join([f"- {item}" for item in parsed_content]) if parsed_content else "No screen context available."
        system_prompt, user_prompt = self.brain.get_strategic_prompts(
            user_request=self.original_user_request,
            screen_context=parsed_content_str,
            installed_applications=self.installed_apps,
            screen_resolution=self.screen_resolution,
            os_info=self.os_info,
            is_replan=(failure_context is not None),
            plan_status=plan_status,
            failure_context=failure_context,
            working_directory=getattr(self.controller, "working_directory", None),
        )

        if not system_prompt or not user_prompt:
            self._handle_unrecoverable_error("Could not load strategic prompts.")
            return

        self.controller.llm_request_sent.emit(
            user_prompt,
            self.controller.pending_attachments,
            system_prompt,
            self.conversation_history,
            "strategic_plan"
        )

    def handle_plan_response(self, reasoning: str, payload: any, schema_name: str):
        logging.info(f"--- Handling LLM response in state: {self.state.name} (received schema: {schema_name}) ---")
        self.ui.show_thinking_ui(False)

        if reasoning and self.state != VisionState.EVALUATING:
            self.ui.add_message(f"<b>{self.controller._get_current_model_name()}:</b> {reasoning}")

        if not payload:
            self._handle_unrecoverable_error("LLM did not provide any action or plan.")
            return

        # Extract the actual payload for processing
        actual_payload = payload.get('payload')
        if not actual_payload:
            self._handle_unrecoverable_error("LLM response was valid but missing a 'payload'.")
            return

        if self.state == VisionState.PLANNING:
            if schema_name == 'strategic_plan':
                self._handle_initial_plan(actual_payload)
            else:
                self._handle_unrecoverable_error(f"Error: Expected a 'strategic_plan' but received '{schema_name}'.")

        elif self.state == VisionState.EVALUATING:
            if schema_name == 'evaluation':
                self._handle_evaluation_response(actual_payload)
            else:
                self._handle_unrecoverable_error(f"Error: Expected 'evaluation' but received '{schema_name}'.")

        elif self.state == VisionState.WAITING_FOR_ELEMENT:
            if schema_name == 'wait_check_result':
                if actual_payload.get("condition_met"):
                    reason = actual_payload.get('reason', 'Condition met.')
                    self.ui.add_message(f"<i>Check successful: {reason}</i>")
                    logging.info("Wait condition MET. Proceeding to next task.")
                    self.current_task_index += 1
                    self._execute_next_task()
                else:
                    reason = actual_payload.get('reason', 'Condition not met yet.')
                    self.ui.add_message(f"<i>Still waiting: {reason}</i>")
                    logging.info(f"Wait condition NOT met. Scheduling next check. Reason: {reason}")
                    self.wait_timer.start(self.WAIT_CHECK_INTERVAL_MS)
            else:
                self._handle_unrecoverable_error(f"Error: Expected a 'wait_check_result' but received '{schema_name}'.")

        elif self.state == VisionState.EXPLAINING_ERROR:
            if schema_name == 'error_explanation':
                self._handle_explanation_response(payload)
            else:
                logging.warning(f"Received schema '{schema_name}' while expecting 'error_explanation'. Ignoring.")

        elif self.state == VisionState.FINDING_WAIT_ORIGIN:
            if schema_name == 'wait_origin_result':
                origin_index = payload.get('origin_action_index')
                try:
                    action, screenshot = self.recent_action_history[origin_index]
                    wait_task = {
                        "task_description": f"Wait for the action '{action['arguments']['description']}' to complete.",
                        "actions": [{
                            "name": "wait_element",
                            "arguments": {
                                "description": f"Wait for the action '{action['arguments']['description']}' to complete.",
                                "wait_context": "The progress indicator or dialog that appeared after the action should disappear."
                            }
                        }],
                        "expected_outcome": "The progress indicator or dialog should disappear."
                    }
                    self._handle_wait_action(wait_task, screenshot)
                except (IndexError, TypeError):
                    self._handle_unrecoverable_error(f"LLM requested historical context at invalid index {origin_index}.")
            else:
                self._handle_unrecoverable_error(f"Error: Expected a 'wait_origin_result' but received '{schema_name}'.")

        else:
            logging.warning(f"Received LLM response in unexpected state: {self.state.name}. Ignoring.")

        logging.info(f"--- Finished handling LLM response in state: {self.state.name} ---")

    def _find_wait_origin(self):
        logging.info("Finding wait origin...")
        recent_history_list = [f"#-{i + 1}: {action.get('arguments', {}).get('description', action.get('name'))}" for i, (action, _) in enumerate(reversed(self.recent_action_history))]
        recent_history_str = "\n".join(recent_history_list)

        attachments = [self.computer.compress_image_to_bytes(screenshot) for _, screenshot in reversed(self.recent_action_history)]
        post_action_screenshot = self.computer.take_screenshot(self.ui.main_window)
        if post_action_screenshot:
            attachments.append(self.computer.compress_image_to_bytes(post_action_screenshot))

        system_prompt, user_prompt = self.brain.get_wait_origin_prompts(recent_history_str)

        if not system_prompt or not user_prompt:
            self._handle_unrecoverable_error("Could not load wait origin prompts.")
            return

        self._set_state(VisionState.FINDING_WAIT_ORIGIN)
        self.controller.llm_request_sent.emit(user_prompt, attachments, system_prompt, [], "wait_origin_result")

    def _handle_initial_plan(self, plan: list | None):
        if not plan:
            self.ui.add_message("<i>I was unable to form a plan to complete your request.</i>")
            self._set_state(VisionState.IDLE)
            return

        self.action_plan = plan
        self.original_plan = self.action_plan.copy()

        readable_plan = [f"{i+1}. {task.get('task_description', 'No description')}" for i, task in enumerate(self.action_plan)]
        plan_summary_for_history = "I have devised a plan to address the user's request. The steps are:\n" + "\n".join(readable_plan)
        self.conversation_history.append({'role': 'assistant', 'content': plan_summary_for_history})

        self.ui.add_message(f"<b>My Plan:</b><br>{'<br>'.join(readable_plan)}")

        self.ui.set_plan_controls_visible(True)
        self._set_state(VisionState.WAITING_FOR_APPROVAL)
        self._focus_user_input_if_main_window_is_active()

    def apply_edited_plan(self, new_plan: list) -> bool:
        if not new_plan or not isinstance(new_plan, list):
            self.ui.add_message("<i>Edited plan is invalid. It must be a non-empty list of steps.</i>")
            return False

        last_task = new_plan[-1]
        if not isinstance(last_task, dict) or not last_task.get('actions') or last_task['actions'][-1].get('name') != 'finish':
            self.ui.add_message("<i>Validation error: The last action of the last task must be a 'finish' action.</i>")
            return False

        self.action_plan = new_plan
        self.original_plan = self.action_plan.copy()
        self.current_task_index = 0
        self.correction_attempts = 0
        self._set_state(VisionState.WAITING_FOR_APPROVAL)

        readable_plan = [f"{i+1}. {task.get('task_description', 'No description')}" for i, task in enumerate(self.action_plan)]
        self.ui.add_message(f"<b>Edited Plan:</b><br>{'<br>'.join(readable_plan)}")
        self.ui.set_plan_controls_visible(True)
        return True

    def regenerate_plan(self):
        if not self.original_user_request:
            self.ui.add_message("<i>No original request available to regenerate a plan.</i>")
            return
        self.start_task(self.original_user_request, [])

    def _generate_plan_status_str(self) -> str:
        if not self.original_plan:
            return "No plan available."

        status_lines = []
        for i, task in enumerate(self.original_plan):
            description = task.get('task_description', 'No description')
            if i < self.current_task_index:
                status_lines.append(f"[DONE] {i + 1}. {description}")
            elif i == self.current_task_index:
                status_lines.append(f"[CURRENT] {i + 1}. {description}")
            else:
                status_lines.append(f"[TODO] {i + 1}. {description}")
        return "\n".join(status_lines)

    def _handle_evaluation_response(self, evaluation_data: dict | None):
        if not evaluation_data:
            self._handle_unrecoverable_error("Invalid response structure from LLM during evaluation.")
            return

        evaluation = evaluation_data.get('evaluation', {})
        next_move = evaluation_data.get('next_move', {})
        status = evaluation.get('status')
        reasoning = evaluation.get('reason', 'No reasoning provided.')

        self.conversation_history.append({'role': 'assistant', 'content': reasoning})

        if status == 'unexpected_wait':
            self.ui.add_message("<i>Unexpected loading detected. Analyzing history...</i>")
            self._find_wait_origin()
            return

        logging.info(f"Evaluation status: {status}. Next move type: {next_move.get('type') if next_move else 'N/A'}. Reason: {reasoning}")

        if not status:
            self._handle_unrecoverable_error("Invalid response structure from LLM. Missing 'status'.")
            return

        if status == 'success':
            self.ui.add_message(f"<b>{self.controller._get_current_model_name()}:</b> Task successful: {reasoning}")
            self.correction_attempts = 0

            next_action = next_move.get('action')
            if next_action:
                logging.info("LLM provided a specific next action. Updating next task in the plan.")
                next_task_index = self.current_task_index + 1
                if next_task_index < len(self.action_plan):
                    self.action_plan[next_task_index]['actions'] = [next_action]
                else:
                    logging.warning("LLM provided a next_action, but the plan is already complete. Ignoring.")

            self.current_task_index += 1
            self.ui.add_message("<i>Proceeding with plan...</i>")
            self._execute_next_task()

        elif status == 'failure':
            self.correction_attempts += 1
            if self.correction_attempts > self.MAX_CORRECTION_ATTEMPTS:
                self._handle_unrecoverable_error("Maximum correction attempts reached.")
                return

            self.ui.add_message(f"<b>{self.controller._get_current_model_name()}:</b> Task failed. {reasoning}. Attempting correction ({self.correction_attempts}/{self.MAX_CORRECTION_ATTEMPTS})...")

            next_move_type = next_move.get('type')

            match next_move_type:
                case 'action_correction':
                    next_action = next_move.get('action')
                    if next_action:
                        self.ui.add_message("<i>Simple failure detected. Correcting current step.</i>")
                        self.action_plan[self.current_task_index]['actions'] = [next_action]
                        self._execute_next_task()
                    else:
                        self._handle_unrecoverable_error("LLM reported action_correction but provided no action.")

                case 'task_replacement':
                    new_task = next_move.get('new_task')
                    if new_task:
                        self.ui.add_message("<i>Uncertain outcome. Trying an alternative method.</i>")
                        self.action_plan[self.current_task_index] = new_task
                        self._execute_next_task()
                    else:
                        self._handle_unrecoverable_error("LLM reported task_replacement but provided no new task.")

                case 'insertion_correction':
                    new_task = next_move.get('new_task')
                    if new_task:
                        self.ui.add_message("<i>Predictable deviation detected. Inserting new step.</i>")
                        self.current_task_index += 1
                        self.action_plan.insert(self.current_task_index, new_task)
                        self._execute_next_task()
                    else:
                        self._handle_unrecoverable_error("LLM reported insertion_correction but provided no new task.")

                case 'strategic_failure':
                    # --- ADAPTIVE PLAN INTEGRATION ---
                    new_plan = next_move.get('new_plan')
                    if new_plan:
                        # The plan was provided pre-emptively by our fallback parser.
                        # We can adopt it directly without another LLM call.
                        self.ui.add_message("<i>LLM suggested a new strategic plan. Adopting it directly.</i>")
                        logging.info("Strategic failure with a pre-emptive plan received. Adopting the new plan directly.")
                        if self.ui.is_compact:
                            self.ui.switchToFullMode()
                        self._handle_initial_plan(new_plan)
                    else:
                        # Standard strategic failure: trigger a full replan.
                        self.ui.add_message("<i>Strategic failure detected. Triggering a full re-plan.</i>")
                        self._trigger_strategic_replan(reasoning)
                    # --- END ADAPTIVE PLAN INTEGRATION ---

                case 'action':
                    next_action = next_move.get('action')
                    if next_action and next_action.get('name') == 'communicate_to_user':
                         self._execute_actions(self.action_plan[self.current_task_index], [next_action])
                    else:
                        self._handle_unrecoverable_error("LLM returned a plain 'action' on failure without a valid communicate_to_user call.")

                case _:
                    self._handle_unrecoverable_error(f"LLM reported a failure with an invalid next move type: '{next_move_type}'.")

        else:
            self._handle_unrecoverable_error(f"Received unknown status from evaluation: '{status}'")

    def _trigger_strategic_replan(self, reason: str):
        """Initiates a full strategic replan after a fatal error."""
        logging.info(f"Triggering strategic replan. Reason: {reason}")
        self._set_state(VisionState.PLANNING)
        self.correction_attempts = 0
        self.is_replan_flow = True
        self.replan_reason = reason

        self.ui.add_message("<i>Attempting to create a new plan from the current state...</i>")

        screenshot = self.computer.take_screenshot(self.ui.main_window)
        if not screenshot:
            self._handle_unrecoverable_error("Screenshot failed during re-plan trigger.")
            return

        self._start_background_analysis(screenshot)

    def handle_plan_approval(self):
        if not self.action_plan:
            self.ui.add_message("<i>Error: No plan to approve.</i>")
            self._set_state(VisionState.IDLE)
            return
        self.ui.switchToCompactMode()
        self.action_timer.singleShot(100, self._start_execution_post_resize)

    def _start_execution_post_resize(self):
        self.ui.add_message("<i>Plan approved. Starting execution...</i>", force_scroll=True)
        self.current_task_index = 0
        self.ui.set_plan_controls_visible(False)
        self._execute_next_task()

    def _execute_actions(self, current_task: dict, actions_to_execute: list):
        """
        Helper function to execute a list of actions.
        If a 'communicate_to_user' action is present, it will pause execution afterwards.
        Otherwise, it will trigger the standard evaluation flow.
        """
        task_result_error = None
        action_result_content = None
        executed_actions_for_log = []
        last_shell_result = None

        if not actions_to_execute:
            self.action_timer.singleShot(self.POST_ACTION_DELAY_MS, lambda: self._evaluate_task_result(current_task, [], None, None))
            return

        # Check if a communication action exists, which requires pausing instead of evaluating.
        contains_communication = any(action.get('name') == 'communicate_to_user' for action in actions_to_execute)

        for action in actions_to_execute:
            try:
                self.recent_action_history.append((action, self.pre_action_screenshot))
                ctx = {
                    "allowed_apps": self.allowed_apps,
                    "app_launcher": self.app_launcher,
                    "shell_allowed": bool((self.settings.get("vision", {}) or {}).get("allow_shell_commands", False)),
                }
                result = self.executor.execute(action, ctx)
                if not result.ok:
                    # Instructional ValueError feeds the structural
                    # self-correction loop exactly like before.
                    task_result_error = ValueError(result.error or f"Tool '{action.get('name')}' failed.")
                    break
                if result.output_text:
                    action_result_content = result.output_text
                elif result.extra.get("raw"):
                    # Compatibility: UI tools may return dicts/strings
                    # consumed by evaluation as clipboard content.
                    action_result_content = result.extra["raw"]
                if result.kind == "shell":
                    last_shell_result = result

                executed_actions_for_log.append(action)
                time.sleep(0.1)
            except Exception as e:
                task_result_error = e
                break

        # Deterministic textual contract check for shell tasks (once per
        # task, after all actions): catches silent no-ops (exit 0, wrong
        # output) before LLM evaluation.
        if task_result_error is None and last_shell_result is not None:
            from src.executor.executor import check_textual_contract as _check_contract
            contract_problem = _check_contract(current_task, last_shell_result)
            if contract_problem:
                logging.warning(f"Textual contract check failed: {contract_problem}")
                task_result_error = ValueError(contract_problem)

        # If a communication action was executed, pause and wait for user guidance.
        if contains_communication:
            logging.info("Pausing after communicate_to_user action.")
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self.controller.handle_vision_error()
        elif self._try_shell_autosuccess(current_task, executed_actions_for_log, last_shell_result):
            # Pure-shell task with a verified textual contract: no screenshots,
            # no LLM round-trip. The loop continues with the next task.
            pass
        else:
            # Otherwise, proceed with the standard evaluation flow.
            self.action_timer.singleShot(self.POST_ACTION_DELAY_MS, lambda: self._evaluate_task_result(current_task, executed_actions_for_log, task_result_error, action_result_content))

    def _try_shell_autosuccess(self, current_task: dict, executed_actions: list, last_shell_result) -> bool:
        """Fast path for pure-shell tasks with a verifiable textual contract.

        When every executed action is textual (shell/info), the task declares
        `expected_output`, and the deterministic contract check already passed,
        the outcome is proven by the command output itself: screenshots and
        an LLM evaluation round-trip would add nothing but cost. Mark the
        task done and advance the plan.
        """
        from src.executor.executor import task_kind, check_textual_contract, expects_visual_result
        if not executed_actions or last_shell_result is None:
            return False
        if task_kind(executed_actions) != "shell":
            return False
        if not (isinstance(current_task, dict) and current_task.get("expected_output")):
            return False
        if expects_visual_result(current_task):
            # Shell task with a declared visual effect (e.g. "open the folder
            # so the user sees it"): screenshots and LLM evaluation stay.
            return False
        if check_textual_contract(current_task, last_shell_result) is not None:
            return False
        logging.info(
            f"Shell task auto-verified (exit {last_shell_result.exit_code}), "
            f"skipping screenshots and LLM evaluation."
        )
        self.ui.add_message("<i>Shell task verified by output, skipping screen analysis.</i>", force_scroll=True)
        self.correction_attempts = 0
        self.current_task_index += 1
        self._execute_next_task()
        return True

    def _execute_next_task(self):
        if self.current_task_index >= len(self.action_plan):
            self.ui.add_message("<b>Action plan completed successfully.</b>")
            self.controller._deactivate_vision_mode()
            self._focus_user_input_if_main_window_is_active()
            return

        current_task = self.action_plan[self.current_task_index]
        task_description = current_task.get('task_description', 'No description')
        self.ui.add_message(f"<b>Task {self.current_task_index + 1}:</b> {task_description}")
        self._set_state(VisionState.EXECUTING)

        actions_in_task = current_task.get('actions', [])
        if not actions_in_task:
            logging.warning(f"Task {self.current_task_index + 1} has no actions. Evaluating as-is.")
            self._evaluate_task_result(current_task, [], "Task has no actions.")
            return

        # --- Handle special single-action tasks that don't need pre/post screenshots ---
        first_action = actions_in_task[0]
        action_name = first_action.get('name')
        action_args = first_action.get('arguments', {}) or {}

        if action_name == 'communicate_to_user':
            message = action_args.get('message', 'The assistant needs your input.')
            prefix = f"<b>{self.controller._get_current_model_name()}:</b> "
            self.ui.add_formatted_message(message, prefix_html=prefix)
            self.pause_for_feedback(is_communication=True)
            return

        if action_name == 'finish':
            message = action_args.get('message', 'Task completed successfully.')
            self.ui.add_formatted_message(message, prefix_html="<b>Task Finished:</b> ")
            self.controller._deactivate_vision_mode()
            self._focus_user_input_if_main_window_is_active()
            return

        if action_name == 'wait_element':
            pre_wait_screenshot = self.computer.take_screenshot(self.ui.main_window)
            self._handle_wait_action(current_task, pre_wait_screenshot)
            return
        # --- End of special actions ---

        # --- Standard execution flow with screenshots and annotation ---
        self.pre_action_screenshot = self.computer.take_screenshot(self.ui.main_window)
        self.pre_action_annotated_screenshot = None  # Reset

        omniparser_settings = self.settings.get("omniparser", {})
        if self.pre_action_screenshot and omniparser_settings.get("save_pre_intent_screenshot", True):
            # Determine the base position for the intent text block
            base_pos = self.last_click_position or (10, 10)
            # Find if there is a click in the current actions to update the base position
            for action in actions_in_task:
                if action.get('name') == 'click':
                    args = action.get('arguments', {}) or {}
                    # Prefer explicit center coordinates if available
                    if 'x' in args and 'y' in args:
                        base_pos = (args['x'], args['y'])
                        self.last_click_position = base_pos
                        break
                    # Otherwise, calculate center from bounding box
                    elif 'x1' in args and 'y1' in args and 'x2' in args and 'y2' in args:
                        center_x = (args['x1'] + args['x2']) // 2
                        center_y = (args['y1'] + args['y2']) // 2
                        base_pos = (center_x, center_y)
                        self.last_click_position = base_pos
                        break

            # --- Deferred imports for performance ---
            import numpy as np
            from vision.annotator import IntentAnnotator
            # ------------------------------------

            # Create an annotated version of the pre-action screenshot
            annotated_image_np = np.array(self.pre_action_screenshot.convert('RGB'))
            ToolCall = namedtuple('ToolCall', ['name', 'arguments'])
            font_path = omniparser_settings.get("font_path", "cv2_default")
            font_size = omniparser_settings.get("font_size", 18)
            intent_annotator = IntentAnnotator(font_path=font_path, font_size=font_size)

            # Pass the determined base position to the annotator
            tool_calls = [ToolCall(name=a.get('name'), arguments=a.get('arguments', {})) for a in actions_in_task]
            if tool_calls:
                annotated_image_np = intent_annotator.annotate(
                    image_np=annotated_image_np,
                    tool_calls=tool_calls,
                    base_position=base_pos  # Pass the correct base position here
                )
            self.pre_action_annotated_screenshot = Image.fromarray(annotated_image_np)

        # --- Handle warnings before executing the rest of the actions ---
        if action_name == 'show_warning':
            remaining_actions = actions_in_task[1:]

            def after_confirm():
                logging.info("User confirmed warning. Proceeding with remaining actions.")
                # Pass the original task object but the remaining actions to the executor
                self._execute_actions(current_task, remaining_actions)

            self.handle_show_warning_action(action_args, on_confirm_callback=after_confirm)
        else:
            # No warning, execute all actions
            self._execute_actions(current_task, actions_in_task)

    def _on_evaluation_analysis_complete(self, post_action_screenshot, post_action_annotated_frame, post_action_context_list):
        """Callback function for when background screen analysis for evaluation is finished."""
        self.controller.thread_pool.release_thread(self.analysis_thread)

        self._stop_analysis_timer()

        # Now, start the timer for the evaluation LLM call
        self._start_thinking_timer("Evaluating...")

        if not post_action_screenshot:
            self._handle_unrecoverable_error("Could not get a screenshot for evaluation.")
            return

        # Retrieve the context from when the evaluation was started
        performed_task = self._evaluation_context.get("performed_task")
        executed_actions = self._evaluation_context.get("executed_actions")
        error = self._evaluation_context.get("error")
        action_result = self._evaluation_context.get("action_result")
        self._evaluation_context = {}  # Clear the context

        omniparser_settings = self.settings.get("omniparser", {})
        should_annotate_post = omniparser_settings.get("save_post_screenshot", True)

        # --- Save screenshots for debugging (conditionally) ---
        try:
            timestamp = int(time.time())

            # Save the annotated pre-action screenshot (with intent)
            if self.pre_action_annotated_screenshot and omniparser_settings.get("save_pre_intent_screenshot", True):
                pre_intent_path = os.path.join('screenshots', f'task_{self.current_task_index}_{timestamp}_pre_intent.png')
                self.pre_action_annotated_screenshot.save(pre_intent_path)
                logging.info(f"Saved pre-action intent screenshot to: {pre_intent_path}")

            # Save the annotated post-action screenshot
            if post_action_annotated_frame is not None and should_annotate_post:
                post_action_path = os.path.join('screenshots', f'task_{self.current_task_index}_{timestamp}_post.png')
                Image.fromarray(post_action_annotated_frame).save(post_action_path)
                logging.info(f"Saved annotated post-action screenshot to: {post_action_path}")

        except Exception as e:
            logging.error(f"Failed to save debug screenshots: {e}")
        # ------------------------------------

        post_action_context_str = "\n".join([f"- {item}" for item in post_action_context_list]) if post_action_context_list else "No screen context available."

        pre_action_bytes = self.computer.compress_image_to_bytes(self.pre_action_screenshot)
        post_action_bytes = self.computer.compress_image_to_bytes(post_action_screenshot)

        plan_status_str = self._generate_plan_status_str()

        system_prompt, user_prompt = self.brain.get_evaluation_prompts(
            original_request=self.original_user_request,
            plan_status_str=plan_status_str,
            history=self.conversation_history,
            performed_task=performed_task,
            executed_actions=executed_actions,
            post_action_context=post_action_context_str,
            error_message=error,
            correction_attempts=self.correction_attempts,
            max_correction_attempts=self.MAX_CORRECTION_ATTEMPTS,
            os_info=self.os_info,
            action_result=action_result,
            working_directory=getattr(self.controller, "working_directory", None),
        )

        if not system_prompt or not user_prompt:
            self._handle_unrecoverable_error("Could not load evaluation prompts.")
            return

        self.controller.llm_request_sent.emit(
            user_prompt,
            [pre_action_bytes, post_action_bytes],
            system_prompt,
            self.conversation_history,
            "evaluation"
        )

    def _evaluate_task_result(self, performed_task: dict, executed_actions: list, error: str = None, action_result: str = None):
        self._set_state(VisionState.EVALUATING)
        self.analysis_start_time = time.time()
        self._start_thinking_timer("Analyzing screen...")

        # Store the context for the callback
        self._evaluation_context = {
            "performed_task": performed_task,
            "executed_actions": executed_actions,
            "error": error,
            "action_result": action_result
        }

        # --- Capture Screenshot in Main Thread ---
        screenshot = self.computer.take_screenshot(self.ui.main_window)
        if not screenshot:
            self.ui.add_message("<i>Error: Could not capture a screenshot for evaluation.</i>")
            self._handle_unrecoverable_error("Screenshot capture failed during evaluation.")
            return
        # ------------------------------------

        # --- Start Background Analysis ---
        self._start_background_analysis(screenshot)
        # ---------------------------------

    def _handle_unrecoverable_error(self, reason: str):
        logging.error(f"Unrecoverable error: {reason}")
        self.ui.add_message(f"<i>I'm stuck. {reason}</i>")

        self._set_state(VisionState.EXPLAINING_ERROR)
        self.ui.show_thinking_ui(True, message="Explaining...", actor=self.controller._get_current_model_name())

        last_task = self.action_plan[self.current_task_index] if self.current_task_index < len(self.action_plan) else "N/A"
        system_prompt, user_prompt = self.brain.get_error_explanation_prompts(self.original_user_request, last_task, reason)

        if not system_prompt or not user_prompt:
            self.ui.add_error_message("Could not generate an explanation due to missing prompt templates.")
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self.controller.handle_vision_error()
            return

        self.controller.llm_request_sent.emit(user_prompt, [], system_prompt, [], "error_explanation")

        self._set_state(VisionState.PAUSED_ON_ERROR)
        self.controller.handle_vision_error()
        self._focus_user_input_if_main_window_is_active()

    def stop(self):
        was_active = self.state not in [VisionState.IDLE]
        self._set_state(VisionState.IDLE)
        self.action_plan = []
        self.current_task_index = 0
        self.is_paused_for_guidance = False
        if was_active:
            self.ui.add_message("<i>Execution stopped.</i>")
        self.controller.handle_cancel_request()
        if self.ui.is_compact:
            self.ui.switchToFullMode()
        self._focus_user_input_if_main_window_is_active()

    def clear_plan(self):
        had_plan = bool(self.action_plan or self.original_plan)
        self.action_plan = []
        self.original_plan = None
        self.original_user_request = None
        self.current_task_index = 0
        self.correction_attempts = 0
        self.is_paused_for_guidance = False
        self.wait_timer.stop()
        self.action_timer.stop()
        self.wait_action = None
        self.pre_action_screenshot = None
        self._set_state(VisionState.IDLE)
        if had_plan:
            self.ui.set_plan_controls_visible(False)
            self.ui.add_message("<i>Plan cancelled and forgotten.</i>", force_scroll=True)


    def pause_or_resume(self):
        if self.state == VisionState.PAUSED:
            self._set_state(self.pre_pause_state or VisionState.EXECUTING)
            self.ui.add_message("<i>Execution resumed.</i>")
            self.ui.set_paused(False)
            self._execute_next_task()
        elif self.state in [VisionState.EXECUTING, VisionState.EVALUATING, VisionState.WAITING_FOR_ELEMENT]:
            self.pre_pause_state = self.state
            self._set_state(VisionState.PAUSED)
            self.ui.add_message("<i>Execution paused. You can make manual changes and then press Resume.</i>")
            self.ui.set_paused(True)

    def pause_for_feedback(self):
        """Pauses execution and switches to full UI to allow the user to provide text guidance."""
        if self.state in [VisionState.EXECUTING, VisionState.EVALUATING, VisionState.WAITING_FOR_ELEMENT, VisionState.PAUSED]:
            self.is_paused_for_guidance = True
            self.pre_pause_state = self.state
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self.ui.add_message("<i>Execution paused. Please provide guidance in the input field.</i>")
            self.controller.handle_vision_error()
        else:
            logging.warning(f"Cannot pause for feedback from current state: {self.state.name}")

    def resume_with_guidance(self, user_guidance: str, attachments: list):
        if self.state != VisionState.PAUSED_ON_ERROR:
            logging.warning("resume_with_guidance called in an incorrect state.")
            return

        self.ui.add_message("<i>Received your guidance. Attempting to replan and continue...</i>")
        self.correction_attempts = 0

        if self.is_paused_for_guidance:
            reason = f"Execution was paused by the user who provided new guidance: '{user_guidance}'"
            self.is_paused_for_guidance = False # reset flag
        else:
            reason = f"The assistant encountered an error and the user provided guidance to correct it: '{user_guidance}'"

        self.conversation_history.append({'role': 'user', 'content': f"User guidance: {user_guidance}"})

        self._trigger_strategic_replan(reason)

    def handle_show_warning_action(self, action_args: dict, on_confirm_callback=None):
        message = action_args.get('message', 'Are you sure you want to proceed?')
        if self.ui.show_warning_dialog(message):
            logging.info("User confirmed the warning dialog.")
            self.conversation_history.append({'role': 'user', 'content': f"User confirmed: {message}"})
            if on_confirm_callback:
                on_confirm_callback()
        else:
            logging.info("User cancelled the warning dialog.")
            self.stop()

    def _handle_wait_action(self, wait_task: dict, pre_wait_screenshot: Image.Image):
        logging.info(f"Initiating wait action: {wait_task.get('task_description')}")
        self._set_state(VisionState.WAITING_FOR_ELEMENT)
        self.wait_action = wait_task
        self.wait_start_time = time.time()
        self.pre_action_screenshot = pre_wait_screenshot
        self.ui.add_message(f"<i>{wait_task.get('task_description')}</i>")
        self._perform_llm_wait_check()

    def _perform_llm_wait_check(self):
        logging.info("Performing LLM wait check...")
        wait_action_args = self.wait_action.get('actions', [{}])[0].get('arguments', {})
        timeout = wait_action_args.get('timeout', 30)

        if (time.time() - self.wait_start_time) > timeout:
            self.ui.add_message("<i>Timed out waiting for the condition.</i>")
            logging.warning("Wait action timed out.")
            wait_context = wait_action_args.get('wait_context', 'No context available.')
            error_message = f"Timed out waiting for element. The original wait condition was: '{wait_context}'"
            self._evaluate_task_result(self.wait_action, [], error=error_message)
            return

        self.ui.show_thinking_ui(True, message="Checking...", actor=self.controller._get_current_model_name())
        _, _, parsed_content = self.vision.analyze_screen(self.ui.main_window, self.controller.settings_manager)

        if parsed_content is None:
            self.ui.show_thinking_ui(False)
            self.ui.add_message("<i>Screen analysis failed, will retry...</i>")
            logging.warning("Screen analysis failed during wait check. Retrying...")
            self.wait_timer.start(self.WAIT_CHECK_INTERVAL_MS)
            return

        post_action_context_str = "\n".join([f"- {str(item)}" for item in parsed_content])
        wait_context = wait_action_args.get('wait_context')

        system_prompt, user_prompt = self.brain.get_wait_check_prompts(
            wait_context=wait_context,
            new_screen_context=post_action_context_str
        )

        if not system_prompt or not user_prompt:
            self.ui.add_message("<i>Could not load wait check prompts. Will retry...</i>")
            logging.error("Failed to get wait check prompts from brain.")
            self.wait_timer.start(self.WAIT_CHECK_INTERVAL_MS)
            return

        logging.info("Sending wait check prompt to LLM.")
        self.controller.llm_request_sent.emit(user_prompt, [], system_prompt, [], "wait_check_result")

    def handle_fatal_error(self, formatted_error_message: str):
        """
        Handles a fatal, unrecoverable error from the LLM provider.
        This method is called externally by the AppController when it catches
        a fatal error signal from the LLMWorker while in vision mode.
        """
        logging.error(f"VisionHandler handling fatal LLM provider error: {formatted_error_message}")

        # Stop all current operations (timers, etc.) and reset state.
        # This will also switch the UI back to full mode.
        self.stop()

        # Display the clean, pre-formatted error message from the formatter.
        self.ui.add_message(formatted_error_message, force_scroll=True)

        # Final state check to ensure we are idle.
        self._set_state(VisionState.IDLE)
        self._focus_user_input_if_main_window_is_active()
