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
import html
import json
import logging
import os
import re
import uuid

from context import model_context
from project_manager import event_text
from states import VisionState

# After this many unsuccessful turns in a row the loop stops asking the
# model and calls the user instead (PAUSED_ON_ERROR). Endless silent
# retries are worse than one honest question. ONE counter for everything:
# timeout deaths count as ordinary failures. Significance is judged only by
# the model (assessment turn); the code below merely picks message wording
# from what actually failed.
MAX_CONSECUTIVE_ERRORS = 3


def _is_error_output(text) -> bool:
    """Whether a loop output line reports a failure (fact, not verdict)."""
    if not text or not isinstance(text, str):
        return False
    if text.startswith("**Error:**") or text.startswith("**Tool Result:** Error"):
        return True
    if text.startswith("**Command Result:** (Exit Code:"):
        first_line = text.split("\n", 1)[0]
        match = re.search(r"Exit Code:\s*(-?\d+)", first_line)
        return bool(match and match.group(1) != "0")
    # A finished background job that failed is a failure observation too —
    # otherwise successful polls keep resetting the streak and escalation
    # never fires while the loop circles a dead job.
    if "'status': 'done'" in text and "'exit_code': 0" not in text:
        return True
    if re.search(r"Background (shell )?job #\d+ done \(exit ", text or ""):
        return "done (exit 0)" not in text
    return False


def _is_timeout_death(text) -> bool:
    """A command/job killed by the shell timeout (not just any failure)."""
    return isinstance(text, str) and "timed out after" in text and "TERMINATED" in text


def _compact_command(body: str, limit: int = 140) -> str:
    """execute_shell({"command": "X", ...}) -> execute_shell: X (truncated).

    The agent command's meaning is in the arguments (the shell command,
    the job id, the path) — not in the JSON boilerplate around them.
    """
    name, _, rest = body.partition("(")
    name = name.strip()
    if rest.endswith(")"):
        try:
            import json as _json
            args = _json.loads(rest[:-1])
            if isinstance(args, dict):
                if "command" in args:
                    text = f"{name}: {_strip_shell_wrapper(str(args['command']))}"
                elif "job_id" in args:
                    text = f"{name} (job #{args['job_id']})"
                elif "seconds" in args:
                    text = f"{name} ({args['seconds']}s)"
                elif "path" in args:
                    text = f"{name}: {args['path']}"
                elif "paths" in args:
                    text = f"{name}: {args['paths']}"
                else:
                    text = body
                return _middle_cut(text, limit)
        except Exception:
            pass
    return _middle_cut(body, limit)


def _middle_cut(text: str, limit: int = 140) -> str:
    """Truncates the middle, keeping start AND end visible."""
    if len(text) <= limit:
        return text
    tail = 40
    head = max(40, limit - tail - 3)
    return text[:head] + " … " + text[-tail:]


def _strip_shell_wrapper(command: str) -> str:
    """Drops routing boilerplate so variants differ at first glance.

    `powershell -Command "Compress-Archive ..."` vs `powershell -Command
    "if (...)"` look identical for 40 chars; the inner cmdlets differ at
    once. Same for `cmd /c`.
    """
    text = command.strip()
    lowered = text.lower()
    for prefix in ('powershell.exe -command', 'powershell -command',
                   'pwsh.exe -command', 'pwsh -command'):
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
                text = text[1:-1]
            return text
    if lowered.startswith("cmd /c "):
        return text[len("cmd /c "):].strip()
    return text


def _outcome_of(result_line: str) -> str:
    """One-line outcome of a result history line (for the tried-list)."""
    if not result_line:
        return "pending"
    if "timed out after" in result_line and "TERMINATED" in result_line:
        m = re.search(r"timed out after (\d+) seconds", result_line)
        return f"timed out after {m.group(1)}s, killed" if m else "timed out, killed"
    m = re.search(r"\(Exit Code: (-?\d+)\)", result_line)
    if m:
        return "ok" if m.group(1) == "0" else f"exit {m.group(1)}"
    if re.search(r"Background (shell )?job #\d+", result_line or ""):
        if " done (exit " in result_line:
            m2 = re.search(r"done \(exit (-?\d+)\)", result_line)
            code = m2.group(1) if m2 else "?"
            return "ok" if code == "0" else f"done, exit {code}"
        return "launched in background"
    if result_line.startswith("**Error:**"):
        rest = result_line[len("**Error:**"):].strip().split("\n")[0][:80]
        return f"error: {rest}" if rest else "error"
    if "Waited " in result_line:
        return "waited"
    return "ok"


def _recent_commands(history, n: int = 3) -> str:
    """Tried-list with outcomes, one attempt per line.

    Answers both questions at a glance: WHAT was tried (real commands,
    middle-truncated so start and end stay visible) and HOW it ended
    (timeout kills say so explicitly).
    """
    lines = history or []
    idx = [i for i, h in enumerate(lines) if h.startswith("**Agent Command:**")][-n:]
    items = []
    for i in idx:
        cmd = _compact_command(lines[i][len("**Agent Command:**"):].strip().strip("`"))
        outcome = "pending"
        for h in lines[i + 1:]:
            if h.startswith(("**Agent Command:**", "**User guidance:**", "**User note",
                             "**User Response:**", "Loop signal:", "**Note:**")):
                outcome = "dropped"
                break
            if h.startswith(("**Tool Result:**", "**Command Result:**", "**Error:**")):
                outcome = _outcome_of(h)
                break
        items.append(f"• {cmd} → {outcome}")
    return "\n".join(items) if items else "(none recorded)"


def _summarize_failure(text, limit: int = 300) -> str:
    """Most actionable lines of a failure output (not just the header).

    'Exit Code: 1' alone tells the user nothing; the timeout sentence or
    the first stderr line does.
    """
    if not text or not isinstance(text, str):
        return "(no details)"
    lines = [ln.strip() for ln in text.split("\n")]
    for ln in lines:
        if "timed out after" in ln and "TERMINATED" in text:
            return ln[:limit]
    useful = [ln for ln in lines
              if ln and ln != "(empty)"
              and not ln.startswith("**") and not ln.startswith("```")]
    if not useful:
        useful = [ln for ln in lines if ln][:2]
    keywords = ("error", "exception", "cannot", "failed", "failure",
                "not found", "timed out", "denied", "missing", "invalid",
                "terminated", "killed")
    prioritized = [ln for ln in useful
                   if any(k in ln.lower() for k in keywords)]
    picked = (prioritized + [ln for ln in useful if ln not in prioritized])[:2]
    out = " / ".join(picked)
    return out[:limit] + ("…" if len(out) > limit else "")


def _is_neutral_output(text) -> bool:
    """Background-launch handles: neither success nor failure.

    Previously they reset the error streak, so death → relaunch → death
    circled forever without ever escalating to the user.
    """
    if not isinstance(text, str):
        return False
    if "'status': 'running'" in text:
        return True
    if re.search(r"Background (shell )?job #\d+", text):
        # Human-shaped reports: running / started / not-found are neutral;
        # done is judged by the error rule (nonzero exit counts).
        return " done (exit " not in text
    return False


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
        self._tool_in_flight = False  # async tool running; superseded answers drop
        self._last_action_sig = None  # (name, args) of the last launched action
        self._consecutive_errors = 0  # unsuccessful turns in a row (escalate at MAX_*)
        self._assessment_sent = False  # her-verdict turn already spent this streak
        self._task_tag = "notask"  # correlation tag for the single-log timeline
        self._turn_seq = 0  # LLM-bound turn counter (same convention as CUA)
        self._active_call_id = None
        self._active_tool_name = ""
        self._active_turn_id = ""
        # NOTE: there is deliberately NO timeout counter. Timeouts count as
        # ordinary failures; significance is the model's call (assessment
        # turn). Do not reintroduce a second fuse.

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
            self._steer_mid_task(user_text)
            return

        logging.info(f"{type(self).__name__} starting task: {user_text}")
        import time as _time
        self._task_tag = f"{type(self).__name__}_{_time.strftime('%H%M%S')}"
        self._turn_seq = 0
        self.original_user_request = user_text
        # Do not wipe history if already populated (e.g. attachments).
        if not self.command_history:
            self.command_history = []
        jobs_note = self._running_jobs_note()
        if jobs_note:
            self.command_history.append(jobs_note)
        self._set_state(VisionState.EXECUTING)
        self._continue_loop()

    def _step_in_flight(self) -> bool:
        """Whether a tool or a background-job wait is currently running.

        A steer must never break it: a long, one-way tool (e.g. a big file
        copy) cannot be resumed, so interrupting it would force a restart.
        The guidance is recorded instead and applied at the next boundary.
        """
        if getattr(self, "_tool_in_flight", False):
            return True
        return getattr(self, "_awaiting_job_id", None) is not None

    def _steer_mid_task(self, user_text: str):
        """Soft steer: fold a mid-task message into the loop without breaking it.

        Mirrors the "understood, continuing my task" behaviour: the message is
        acknowledged at once and taken into account on the next planning turn.
        A running step is never interrupted; only an in-flight (stateless)
        planning request is dropped and re-issued with the guidance.
        """
        # Fresh guidance supersedes any stashed paused response.
        self._stashed_response = None
        guidance = f"**User guidance:** {user_text}"
        if self._step_in_flight():
            # Record it; the step's own completion continues the loop and the
            # planning turn then sees both the guidance and the fresh result.
            self.command_history.append(guidance)
            self.ui.add_message(
                "<i>Noted — I'll take it into account after the current step "
                "finishes.</i>")
            logging.info(f"{type(self).__name__} steering recorded while a "
                         f"step is in flight; deferred to the next turn.")
            return
        # No tool running (possibly a stateless planning request): re-plan with
        # the guidance so the next action already accounts for it.
        if self.controller.llm_worker:
            self.controller.llm_worker.cancel()
        self.controller.thinking_timer.stop()
        self.ui.show_thinking_ui(False)
        self._set_state(VisionState.EXECUTING)
        self.ui.set_paused(False)
        self._continue_loop(last_command_output=guidance)

    def pause_or_resume(self):
        logging.info(f"{type(self).__name__} pause button pressed in state {self.state.name}.")
        if self.state == VisionState.PAUSED:
            # Pick up an unsent draft as context (no extra request), then
            # resume plainly. Resume itself never sends anything to the model.
            # The draft is shown as a normal You-message, like any sent text.
            draft = self.ui.user_input.toPlainText().strip()
            if draft:
                self.ui.add_user_message(draft)
                self.controller.session_turn_id = uuid.uuid4().hex
                self.controller.conversation_history.append({'role': 'user', 'content': draft})
                self.command_history.append(f"**User note (paused):** {draft}")
                self.ui.user_input.clear()
                persist = getattr(self.controller, "_persist_session", None)
                if callable(persist):
                    persist()
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
        self._tool_in_flight = False
        self._last_action_sig = None
        self._consecutive_errors = 0
        self._assessment_sent = False

    def _cleanup_task(self):
        """Heir hook: release mode resources (browsers, threads, UI containers)."""

    def _history_action(self, action: dict) -> dict:
        """Heir hook: redact secrets before writing an action to history."""
        return action

    # -- unified single-step loop ----------------------------------------

    def cancel_job_wait(self):
        """Heir hook: drop a pending background-job wait. Default: none."""
        return

    def _escalation_limit(self):
        """User-configured escalation threshold, read live (no restart needed).

        ONE knob shared by all agent modes (vision inherits it when it moves
        onto the unified loop). Falls back to the module default on trouble.
        """
        try:
            settings = getattr(getattr(self, "controller", None), "settings", None) or {}
            max_errors = int(settings.get("max_consecutive_errors",
                                          MAX_CONSECUTIVE_ERRORS)
                             or MAX_CONSECUTIVE_ERRORS)
        except Exception:
            max_errors = MAX_CONSECUTIVE_ERRORS
        return max(1, max_errors)

    def _assessment_note(self, kind: str) -> str:
        """Assessment instruction for the model, loaded from prompt files.

        Prompts live in prompts/, never hardcoded: per-mode file first,
        then the shared one. The short fallback only covers missing files.
        """
        candidates = []
        subdir = getattr(self, "PROMPTS_SUBDIR", None)
        if subdir:
            candidates.append((f"assessment_{subdir}_{kind}",
                               f"prompts/{subdir}/assessment_{kind}.txt"))
        candidates.append((f"assessment_common_{kind}",
                           f"prompts/common/assessment_{kind}.txt"))
        for key, path in candidates:
            try:
                if not os.path.isfile(path):
                    continue
                text = self.brain._get_cached_prompt(key, path)
                if text and "template not found" not in text:
                    return text.strip()
            except Exception:
                pass
        return ("**System note:** assess via communicate_to_user before acting "
                "further. One message, then wait.")

    def _continue_loop(self, last_command_output=None, from_job_wait=False):
        """
        Plan one action -> execute -> observe -> repeat.
        Evaluation is implicit: the next planning turn sees the result
        (plus loop-awareness notes) and corrects course itself.
        """
        if not from_job_wait:
            # Any explicit new turn supersedes a pending background wait.
            try:
                self.cancel_job_wait()
            except Exception:
                pass
        # Doctrine: significance is the MODEL's call, never the code's. ONE
        # counter counts unsuccessful turns (timeout deaths included — they
        # are ordinary failures). Neutral launches/polls have no outcome yet
        # and preserve the streak; success/None/stop reset it. Whether a
        # SyntaxError is a trifle or the wall is judged by her in the
        # assessment turn, not by marker lists here. The user is only ever
        # bothered via her communicate_to_user or the second-tripwire pause.
        if last_command_output:
            self.command_history.append(last_command_output)
            if _is_neutral_output(last_command_output):
                pass
            elif _is_error_output(last_command_output):
                self._consecutive_errors += 1
                # NOTE: flag intentionally preserved — only success/None/stop
                # grant a new assessment turn, otherwise it would loop forever.
            else:
                self._consecutive_errors = 0
                self._assessment_sent = False
        else:
            self._consecutive_errors = 0
            self._assessment_sent = False

        if self.state in (VisionState.IDLE, VisionState.PAUSED):
            return

        max_errors = self._escalation_limit()
        if self._consecutive_errors < max_errors:
            escalate_kind = None
        else:
            # Timeout flavor is message wording, not a second fuse: if any of
            # the recent failures was a timeout death, the pause names the limit.
            recent = self.command_history[-8:]
            escalate_kind = ("timeout" if any(_is_timeout_death(h) for h in recent)
                             else "general")

        if escalate_kind is not None:
            self._consecutive_errors = 0
            if not self._assessment_sent:
                # Only the model itself can separate real blockers from its
                # own trifles — so SHE gets the first word: one assessment
                # turn via communicate_to_user. If she ignores it and issues
                # a tool instead, the normal flow continues; the next trigger
                # pauses directly with the template below.
                self._assessment_sent = True
                note = self._assessment_note(escalate_kind)
                self.command_history.append(note)
                logging.info(f"{type(self).__name__} requesting self-assessment "
                             f"before paging the user ({escalate_kind}).")
            else:
                failure_summary = _summarize_failure(last_command_output)
                tried = _recent_commands(self.command_history)
                if escalate_kind == "timeout":
                    text = (
                        f"This task keeps getting killed by the shell timeout ({failure_summary}). "
                        f"Repeated retries already failed the same way, so retrying is pointless. "
                        f"Tried most recently: {tried}. "
                        f"Options: raise \"Shell timeout\" in Terminal settings (tell me roughly "
                        f"how long the operation needs), split the work into smaller pieces, "
                        f"or abort it — your call, I'll continue when you reply."
                    )
                    logging.info(f"{type(self).__name__} escalating to user: task keeps "
                                 f"dying at the shell timeout.")
                else:
                    text = (
                        f"I'm stuck — the last steps keep failing (latest: {failure_summary}). "
                        f"Tried most recently: {tried}. "
                        f"Tell me how to proceed and I'll continue when you reply."
                    )
                    logging.info(f"{type(self).__name__} escalating to user after "
                                 f"consecutive failures.")
                self.ui.add_text(text, prefix_html=self._model_prefix())
                self._set_state(VisionState.PAUSED_ON_ERROR)
                self._stop_thinking_timer()
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

        self._turn_seq += 1
        self.controller._send_llm_request(
            user_prompt,
            self._request_attachments(),
            system_prompt,
            [],
            self.SCHEMA_NAME,
            self._task_tag,
            self._turn_seq,
        )

    def _request_attachments(self) -> list:
        """Extra payloads for the next request (images, files). Default: none."""
        return []

    def _running_jobs_note(self) -> str:
        """History line about background jobs outliving their task. Default: none."""
        return ""

    def _model_history_and_stats(self, history=None):
        """Model-facing history plus compaction stats for diagnostics."""
        source = list(self.command_history if history is None else history)
        context_cfg = self.settings.get("context") if isinstance(
            self.settings, dict) else {}
        before, tail = model_context.split_compacted_history(source)
        replacement = ""
        if model_context.has_compaction_marker(source):
            build_block = getattr(self.controller, "compaction_block", None)
            if callable(build_block):
                replacement = build_block(self.MODE_NAME) or ""
            if replacement:
                protected = [
                    entry for entry in before
                    if model_context.is_protected_entry(entry)
                ]
                entries = protected + [replacement] + tail
            else:
                # No summary to stand in for the old part: keep it, but never
                # show the bare marker line to the model.
                entries = before + tail
        else:
            entries = tail
        compacted, stats = model_context.microcompact_agent_history(
            entries, context_cfg, mode=self.MODE_NAME)
        stats["compacted_entries"] = max(0, len(source) - len(entries))
        stats["compaction_active"] = bool(replacement)
        return compacted, stats

    def model_history(self, history=None) -> list:
        """History trimmed for the provider; ``command_history`` stays intact."""
        compacted, stats = self._model_history_and_stats(history)
        if stats.get("compacted_entries"):
            logging.info(
                "Model history for %s: summary replaced %d of %d entries "
                "(%d -> %d chars).",
                self.MODE_NAME, stats["compacted_entries"],
                stats["compacted_entries"] + stats["entries"],
                stats["chars_before"], stats["chars_after"])
        elif stats.get("saved"):
            logging.info(
                "Model history for %s: %d -> %d chars (-%d; "
                "%d old tool result(s) pruned).",
                self.MODE_NAME, stats["chars_before"], stats["chars_after"],
                stats["saved"], stats["pruned_results"])
        return compacted

    def _payload_image_count(self) -> int:
        """Images attached to the next request. Default: none."""
        return 0

    def context_payload(self) -> dict:
        """Text components of the next planning request, for diagnostics.

        Terminal and Browser embed the history in the user prompt. Vision's
        CUA request is assembled elsewhere, so it reports no payload.
        """
        if self.MODE_NAME not in ("terminal", "browser"):
            return {}
        system_prompt, user_prompt = self.build_prompts()
        _, stats = self._model_history_and_stats()
        return {
            "system_text": system_prompt or "",
            "history_text": "",
            "user_text": user_prompt or "",
            "tool_schema_text": model_context.tool_schema_text(
                self.MODE_NAME, self.SCHEMA_NAME),
            "image_count": self._payload_image_count(),
            "compact": stats,
        }

    def _model_prefix(self, suffix: str = ": ") -> str:
        model_name = html.escape(str(self.controller._get_current_model_name()))
        model_color = self.controller.theme_manager.get_style("MODEL_NAME_COLOR")
        return (f'<span style="color: {model_color}; font-weight: 600;">'
                f'{model_name}</span>{suffix}')

    def handle_llm_response(self, reasoning: str, payload: any, schema_name: str):
        """Handles the response from the LLM, which should be a tool call."""
        if self.state == VisionState.PAUSED:
            logging.info(f"{type(self).__name__} LLM response arrived while paused; stashing.")
            self._stashed_response = (reasoning, payload, schema_name)
            self._stop_thinking_timer()
            return
        self._stop_thinking_timer()

        if reasoning:
            self.ui.add_formatted_message(reasoning, prefix_html=self._model_prefix())
            # Her own thoughts persist: without this the next turn sees what
            # was DONE but never what was MEANT — stage tracking evaporates.
            self.command_history.append(f"**Model reasoning:** {reasoning}")
            self._append_session_event(
                "reasoning",
                turn_id=getattr(self.controller, "session_turn_id", ""),
                text=reasoning,
            )

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
            self._record_assistant_message(message)
            success = self.controller.theme_manager.get_style("SUCCESS_COLOR")
            prefix = (f'<span style="color: {success}; font-weight: 600;">'
                      'Task Finished</span>: ')
            self.ui.add_formatted_message(message, prefix_html=prefix)
            self.stop()
            return

        if action.get('name') == 'communicate_to_user':
            message = (action.get('arguments') or {}).get('message', 'The assistant needs your input.')
            self._record_assistant_message(message)
            self.ui.add_formatted_message(message, prefix_html=self._model_prefix())
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return

        if action.get('name') == 'display_message':
            message = (action.get('arguments') or {}).get('message', '...')
            self._record_assistant_message(message)
            self.ui.add_formatted_message(message, prefix_html=self._model_prefix())
            self._continue_loop(f"**Tool Result:** Message displayed to user: {message}")
            return

        self._run_action(action)

    def handle_response_chunk(self, chunk):
        # Agent modes are non-streaming; stray chunks (e.g. a chat stream
        # that outlived a mode switch) are ignored instead of crashing.
        pass

    def _append_session_event(self, event_type: str, **fields):
        append = getattr(self.controller, "append_session_event", None)
        if not callable(append):
            return None
        try:
            return append(event_type, **fields)
        except Exception as e:
            logging.warning(f"Could not append {event_type} session event: {e}")
            return None

    def record_tool_result(self, text, status: str = "", call_id: str = "",
                           name: str = ""):
        call_id = call_id or self._active_call_id
        if not call_id:
            return
        if not status:
            status = "error" if _is_error_output(text) else "ok"
        self._append_session_event(
            "tool_result",
            turn_id=self._active_turn_id or getattr(
                self.controller, "session_turn_id", ""),
            call_id=call_id,
            name=name or getattr(self, "_active_tool_name", "") or "",
            status=status,
            output={"text": str(text or "")},
        )
        self._active_call_id = None
        self._active_tool_name = ""

    def _record_assistant_message(self, text):
        """Persist a user-visible assistant message so replay can show it."""
        text = str(text or "")
        if not text:
            return
        self.controller.conversation_history.append(
            {"role": "assistant", "content": text})
        persist = getattr(self.controller, "_persist_session", None)
        if callable(persist):
            persist()
        schedule = getattr(self.controller, "_schedule_summary_refresh", None)
        if callable(schedule):
            schedule()

    def _add_replay_result(self, text, title, status: str = ""):
        """Render a persisted tool/job result as a collapsed code block."""
        text = str(text or "(no output)")
        if text.startswith("**Tool Result:**\n"):
            text = text[len("**Tool Result:**\n"):].strip()
            if text.startswith("```") and text.endswith("```"):
                text = text[3:-3].strip("\n")
        shown = text
        if len(shown) > 30000:
            shown = shown[:30000] + (
                f"\n… [truncated for display, {len(text) - 30000} chars hidden]")
        block = {
            "type": "collapsible",
            "title": title,
            "content": shown,
            "plain_content": text,
            "copy_payloads": {"output": text},
            "state": "collapsed",
        }
        if status == "error":
            theme = getattr(self.controller, "theme_manager", None)
            if theme is not None:
                block["title_color"] = theme.get_style("ERROR_COLOR")
        self.ui.add_message(block)

    def replay_event(self, event: dict) -> bool:
        """Replay a persisted tool event in the current mode's visual style."""
        event_type = event.get("type")
        if event_type == "tool_call":
            name = str(event.get("name") or "action")
            args = event.get("input") or {}
            self.ui.add_message(f"<b>Executing:</b> {html.escape(name)}")
            self.ui.add_message({
                "type": "collapsible",
                "title": "Arguments",
                "content": json.dumps(args, ensure_ascii=False, indent=2),
            })
            return True
        if event_type in ("tool_result", "job_result"):
            status = str(event.get("status") or "")
            if event_type == "job_result":
                title = (f"Background job #{event.get('job_id', '?')} · "
                         f"{status or 'result'}")
            else:
                title = f"{event.get('name') or 'Tool'} · {status or 'result'}"
            self._add_replay_result(event_text(event), title, status)
            return True
        return False

    def _run_action(self, action: dict):
        if self._tool_in_flight:
            # A duplicate/superseded answer (e.g. user guidance sent a second
            # request while the first answer's tool still runs). The running
            # tool's result continues the loop; running this too would queue
            # a second tool behind it and stall on slow outputs.
            logging.info(f"{type(self).__name__} dropping superseded action "
                         f"'{action.get('name')}' (a tool is still running).")
            return
        action_name = action.get('name')
        action_args = action.get('arguments', {})
        if not isinstance(action_args, dict):
            action_args = {}
            action['arguments'] = action_args
        sig = (action_name, json.dumps(action_args, sort_keys=True, ensure_ascii=False))
        if sig == self._last_action_sig:
            # Identical consecutive action: history already holds it and its
            # result. Executing again cannot produce new information.
            logging.info(f"{type(self).__name__} blocking identical repeat of '{action_name}'.")
            # NOTE: "Blocked", not "Error" — guardrail hits must NOT count
            # toward the escalation streak (a healthy job with busy-looped
            # polls once paged the user while everything worked).
            self._continue_loop(
                "**Tool Result:** Blocked: this action is identical to the previous one "
                "(see its result above). Do not repeat it — vary the approach, check the "
                "current state first, or report to the user."
            )
            return
        self._last_action_sig = sig
        history_action = self._history_action(action)
        command_str_for_llm = (
            f"`{history_action.get('name')}"
            f"({json.dumps(history_action.get('arguments') or {}, ensure_ascii=False)})`")
        self.command_history.append(f"**Agent Command:** {command_str_for_llm}")
        self._active_call_id = uuid.uuid4().hex
        self._active_tool_name = action_name or ""
        self._active_turn_id = getattr(self.controller, "session_turn_id", "")
        self._append_session_event(
            "tool_call",
            turn_id=self._active_turn_id,
            call_id=self._active_call_id,
            name=action_name,
            input=history_action.get("arguments") or {},
        )
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
        self.record_tool_result(result_text)
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
