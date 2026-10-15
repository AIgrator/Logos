"""
Vision mode, rebuilt on the agent loop (no strategic plan).

The handler drives a native Computer Use exchange through a provider that
implements the BaseLLMProvider computer-use contract (one protocol turn per
call). The loop itself — observe, act, count failures, escalate — is the
shared BaseAgent discipline, same as Terminal/Browser.

Per-turn flow (each turn runs in a worker thread, never on the GUI thread):
  1. provider.computer_use_turn(...) -> structured actions (+ safety checks)
  2. safety checks, if any -> user approval dialog (loop pauses meanwhile)
  3. actions execute in order via deterministic performers (pyautogui/mss)
  4. fresh screenshot -> next turn (server-side chain via previous_response_id)

Legacy plan-era entry points (handle_plan_*, regenerate_plan, ...) are kept
as deprecated stubs because the controller/UI still references them; they
explain the removal instead of crashing. See legacy/vision_2026-10-15/ for
the pre-rebuild implementation.
"""
import base64
import io
import logging
import threading
import time

from PySide6.QtCore import QThread, Signal

from states import VisionState
from src.computer.computer import Computer
from src.computer.tools.constants import KEYBOARD_KEYS
from src.handlers.base_agent import (
    BaseAgentHandler as BaseAgent,
    _is_error_output,
    _is_neutral_output,
    _recent_commands,
    _summarize_failure,
)


# ---------------------------------------------------------------------------
# Pure helpers (testable without a display).
# ---------------------------------------------------------------------------

MAX_CUA_TURNS = 20
NETWORK_HINT = "cua-turn"

KEY_ALIASES = {
    "return": "enter",
    "escape": "esc",
    "esc.": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "tab": "tab",
    "space": "space",
    " ": "space",
    "shift": "shift",
    "control": "ctrl",
    "ctrl": "ctrl",
    "alt": "alt",
    "option": "alt",
    "super": "win",
    "win": "win",
    "windows": "win",
    "command": "ctrl",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "insert": "insert",
    "enter": "enter",
}


def normalize_key(name: str) -> str:
    """Maps a vendor key name onto our pyautogui-compatible key table."""
    if not isinstance(name, str):
        raise ValueError(f"Key name must be a string, got {type(name)}")
    key = KEY_ALIASES.get(name.strip().lower(), name.strip().lower())
    if key in KEYBOARD_KEYS:
        return key
    # Single characters are typeable as-is.
    if len(name.strip()) == 1:
        return name.strip()
    raise ValueError(f"Unsupported key '{name}' (normalized to '{key}')")


def describe_action(action: dict) -> str:
    """One-line human summary for history/UI, e.g. click(405, 157)."""
    if not isinstance(action, dict):
        return str(action)
    kind = action.get("type", "?")
    if kind in ("click", "double_click", "move"):
        return f"{kind}({action.get('x')}, {action.get('y')})"
    if kind == "drag":
        path = action.get("path") or []
        return f"drag({len(path)}pts)"
    if kind == "scroll":
        return f"scroll({action.get('scroll_x', 0)}, {action.get('scroll_y', 0)})"
    if kind == "keypress":
        return f"keypress({','.join(action.get('keys', []))})"
    if kind == "type":
        text = str(action.get("text", ""))
        return f"type({text[:40]!r})"
    return kind


def scale_point(x: float, y: float, scale: float) -> tuple:
    """
    Maps model coordinates onto real screen pixels.

    Assumption (Anthropic rule): the model emits coordinates in the pixel
    space of the screenshot we sent. v1 sends native resolution, so scale
    is 1.0. If a vendor uses a normalized 0-1000 space, the test run will
    show it and scale becomes real_size/1000 via settings.
    """
    return (x * scale, y * scale)


def _annotation_font(size: int = 22):
    """Best-effort truetype font; falls back to PIL's bitmap default."""
    from PIL import ImageFont
    for candidate in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def annotate_screenshot(img, actions: list, scale: float = 1.0):
    """
    Draws what the model did onto a COPY of the screenshot (PIL only):
    red circles + labels for clicks, polylines for drags, corner stamp
    for keypress/type/wait. Returns the annotated copy.
    """
    from PIL import ImageDraw
    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)
    font = _annotation_font()
    red = (255, 0, 0)

    def dot(x, y, r=14, label=None):
        x, y = scale_point(float(x), float(y), scale)
        draw.ellipse([x - r, y - r, x + r, y + r], outline=red, width=4)
        draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=red)
        if label:
            draw.text((x + r + 6, y - r - 6), label, fill=red, font=font)

    corner_notes = []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        kind = action.get("type", "?")
        if kind in ("click", "double_click"):
            dot(action.get("x", 0), action.get("y", 0),
                label=f"{kind} {action.get('x')},{action.get('y')}")
        elif kind == "move":
            dot(action.get("x", 0), action.get("y", 0), r=8,
                label=f"move {action.get('x')},{action.get('y')}")
        elif kind == "drag":
            pts = [scale_point(float(p["x"]), float(p["y"]), scale)
                   for p in (action.get("path") or [])]
            if len(pts) >= 2:
                draw.line(pts, fill=red, width=4)
                dot(action["path"][0]["x"], action["path"][0]["y"], r=10)
                dot(action["path"][-1]["x"], action["path"][-1]["y"],
                    label="drag end")
        elif kind == "scroll":
            dot(action.get("x", 0), action.get("y", 0), r=10,
                label=f"scroll {action.get('scroll_x', 0)},{action.get('scroll_y', 0)}")
        elif kind in ("keypress", "type", "wait"):
            corner_notes.append(describe_action(action))

    if corner_notes:
        draw.text((20, 20), "\n".join(corner_notes), fill=red, font=font)
    return canvas


# ---------------------------------------------------------------------------
# Worker: one CUA turn (network + execution + screenshot), off the GUI thread.
# ---------------------------------------------------------------------------

class CuaTurnWorker(QThread):
    """Runs a single computer-use turn: provider call, approvals, execution."""

    turn_done = Signal(dict)      # outcome payload (see _finish)
    safety_needed = Signal(list)  # pending_safety_checks -> GUI approval
    progress = Signal(str)

    def __init__(self, handler, provider, turn_request: dict, parent=None):
        super().__init__(parent)
        self._handler = handler
        self._provider = provider
        self._request = turn_request
        self._stop = False
        self._safety_event = threading.Event()
        self._safety_approved = False

    def stop(self):
        self._stop = True
        self._safety_event.set()

    def resolve_safety(self, approved: bool):
        self._safety_approved = approved
        self._safety_event.set()

    def _paused_spin(self, where: str) -> bool:
        """Returns False when the task was stopped while paused."""
        h = self._handler
        while h.state == VisionState.PAUSED and not self._stop:
            self.progress.emit(f"Paused ({where}) — waiting for Resume…")
            time.sleep(0.4)
        return not self._stop and h.state != VisionState.IDLE

    def run(self):
        outcome: dict
        try:
            # Split: protocol kwargs go to the provider; debug meta stays local.
            # (Passing tag/turn_no/debug_flags into computer_use_turn crashes.)
            protocol = dict(self._request)
            tag = protocol.pop("tag", "cua")
            turn_no = protocol.pop("turn_no", 0)
            flags = protocol.pop("debug_flags", None) or {}
            if not self._paused_spin("before provider turn"):
                outcome = {"aborted": True}
                self.turn_done.emit(outcome)
                return
            self.progress.emit("Thinking (computer-use turn)…")
            turn = self._provider.computer_use_turn(**protocol)

            checks = turn.get("pending_safety_checks") or []
            if checks and not self._stop:
                self.safety_needed.emit(checks)
                self._safety_event.wait()
                if self._stop:
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                if not self._safety_approved:
                    outcome = {"denied": True, "checks": checks}
                    self.turn_done.emit(outcome)
                    return
                # Re-send the turn output WITH acknowledgements. The model
                # replays the call; we execute on the replayed actions.
                self.progress.emit("Safety approved — replaying turn…")
                ack = [{"id": c.get("id"), "code": c.get("code"),
                        "message": c.get("message")} for c in checks]
                follow = dict(protocol)
                follow["acknowledged_safety_checks"] = ack
                turn = self._provider.computer_use_turn(**follow)

            if not self._paused_spin("before execution"):
                outcome = {"aborted": True}
                self.turn_done.emit(outcome)
                return

            actions = turn.get("actions") or []
            done_text = turn.get("done_text")

            if flags.get("save_pre"):
                try:
                    pre_b64, _, _ = self._handler.capture_screenshot_b64()
                    self._handler.save_debug_shot(tag, turn_no, pre_b64, "pre")
                except Exception as e:
                    logging.warning(f"pre-action shot failed: {e}")

            if not actions:
                outcome = {
                    "done": True,
                    "done_text": done_text or "",
                    "response_id": turn.get("response_id"),
                }
                self._handler.trace_turn(tag, turn_no, [], False, None)
                self.turn_done.emit(outcome)
                return

            executed: list = []
            executed_raw: list = []
            for action in actions:
                if self._stop:
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                if not self._paused_spin("between actions"):
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                try:
                    self._handler.execute_action(action)
                    executed.append(describe_action(action))
                    executed_raw.append(action)
                except Exception as e:
                    logging.warning(f"CUA action failed: {action} -> {e}")
                    outcome = {
                        "action_error": f"**Error:** {describe_action(action)} failed: {e}",
                        "executed": executed,
                        "response_id": turn.get("response_id"),
                        "call_id": turn.get("call_id"),
                    }
                    self._handler.trace_turn(tag, turn_no, executed_raw, False, None)
                    self.turn_done.emit(outcome)
                    return

            shot_b64, _, _ = self._handler.capture_screenshot_b64()
            shot_file = None
            if flags.get("save_post"):
                try:
                    shot_file = self._handler.save_debug_shot(
                        tag, turn_no, shot_b64, "post",
                        annotate_with=executed_raw)
                except Exception as e:
                    logging.warning(f"post-action shot failed: {e}")
            self._handler.trace_turn(
                tag, turn_no, executed_raw,
                bool(turn.get("pending_safety_checks")), shot_file)
            outcome = {
                "executed": executed,
                "response_id": turn.get("response_id"),
                "call_id": turn.get("call_id"),
                "screenshot_b64": shot_b64,
            }
            self.turn_done.emit(outcome)
        except Exception as e:
            logging.error(f"CUA turn failed: {e}", exc_info=True)
            try:
                self.turn_done.emit({"fatal": f"**Error:** computer-use turn failed: {e}"})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------

class VisionHandler(BaseAgent):
    """Vision mode on the shared agent loop, grounded by Computer Use."""

    SCHEMA_NAME = None
    MAX_CUA_TURNS = MAX_CUA_TURNS

    def __init__(self, controller):
        super().__init__(controller)
        self.computer = Computer(
            settings_manager=getattr(controller, "settings_manager", None),
            mode="vision",
            working_directory=getattr(controller, "working_directory", None),
        )
        # Chain state for the server-side conversation (previous_response_id).
        self._cua_response_id = None
        self._cua_call_id = None
        self._cua_turns = 0
        self._worker = None
        # Deprecated plan-era surface (controller/UI still reference it).
        self.action_plan = []

    # -- task entry ----------------------------------------------------

    def start_task(self, user_text: str, attachments=None):
        if attachments:
            logging.info(f"Vision CUA ignoring {len(attachments)} attachment(s) (v1).")
        self._cua_response_id = None
        self._cua_call_id = None
        self._cua_turns = 0
        self._cua_tag = time.strftime("%H%M%S")
        super().start_task(user_text)

    def resume_with_guidance(self, user_text: str, attachments=None):
        """
        The CUA protocol cannot inject free text mid-chain, so guidance
        restarts the chain with the augmented task (server history resets;
        local history is kept for escalation context).
        """
        base = self.original_user_request or ""
        combined = f"{base}\n\nAdditional user guidance: {user_text}".strip()
        logging.info("Vision CUA restarting chain with user guidance.")
        self.stop()
        self.start_task(combined)

    # -- agent loop (BaseAgent discipline, CUA tail) --------------------

    def _cua_system_prompt(self) -> str:
        if getattr(self, "_cua_system_cache", None) is None:
            text = ""
            try:
                import os
                base = self.settings.get(
                    "APP_PROMPTS_DIR",
                    os.path.join("prompts"))
                with open(os.path.join(base, "vision", "cua_system.txt"),
                          encoding="utf-8") as f:
                    text = f.read().strip()
            except Exception as e:
                logging.warning(f"CUA system prompt missing: {e}")
            self._cua_system_cache = text
        return self._cua_system_cache or ""

    def _continue_loop(self, last_command_output=None):
        # Counting + escalation: same doctrine as Terminal/Browser (one
        # counter, model-agnostic markers). Deviation: no self-assessment
        # turn — the CUA protocol has no free-text injection channel, so on
        # the limit we pause with a summary straight away.
        if last_command_output:
            self.command_history.append(last_command_output)
            if _is_neutral_output(last_command_output):
                pass
            elif _is_error_output(last_command_output):
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 0
                self._assessment_sent = False
        else:
            self._consecutive_errors = 0
            self._assessment_sent = False

        if self.state in (VisionState.IDLE, VisionState.PAUSED):
            return

        if self._cua_turns >= self.MAX_CUA_TURNS:
            self.ui.add_message(
                f"<i>Stopped after {self.MAX_CUA_TURNS} computer-use turns "
                f"without finishing. Tell me how to proceed.</i>")
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return

        max_errors = self._escalation_limit()
        if self._consecutive_errors >= max_errors:
            self._consecutive_errors = 0
            summary = _summarize_failure(last_command_output)
            tried = _recent_commands(self.command_history)
            self.ui.add_text(
                f"I'm stuck — the last computer-use steps keep failing "
                f"(latest: {summary}). Tried most recently: {tried}. "
                f"Tell me how to proceed and I'll continue when you reply.",
                prefix_html="<b>Vision:</b> ",
            )
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return

        if not self.provider_supports_cua():
            self.ui.add_message(
                "<i>Current provider does not support Computer Use. "
                "Switch to OpenAI (e.g. gpt-6-luna) to use Vision mode.</i>")
            self.stop()
            return

        self._start_thinking_timer("Thinking (computer use)…")
        debug_flags = self._debug_flags()
        if self._cua_response_id is None:
            request = {"task": self.original_user_request,
                       "instructions": self._cua_system_prompt()}
        else:
            request = {
                "call_id": self._cua_call_id,
                "screenshot_b64": self._last_screenshot_b64,
                "previous_response_id": self._cua_response_id,
            }
        request.update({
            "tag": getattr(self, "_cua_tag", "cua"),
            "turn_no": self._cua_turns + 1,
            "debug_flags": debug_flags,
        })
        self._worker = CuaTurnWorker(self, self._provider(), request)
        self._worker.turn_done.connect(self._on_turn_done)
        self._worker.safety_needed.connect(self._on_safety_needed)
        self._worker.progress.connect(
            lambda msg: self.ui.add_message(f"<i>{msg}</i>"))
        self._worker.start()

    # -- turn outcome (GUI thread) --------------------------------------

    def _on_turn_done(self, outcome: dict):
        self._worker = None
        if self.state == VisionState.IDLE:
            return  # stopped meanwhile; drop the late turn
        if outcome.get("aborted"):
            return
        if outcome.get("denied"):
            codes = ", ".join(
                str(c.get("code", "?")) for c in outcome.get("checks", []))
            self.ui.add_text(
                f"Paused: a safety check ({codes}) needs your call and was "
                f"not approved. Tell me how to proceed.",
                prefix_html="<b>Vision:</b> ",
            )
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return
        if outcome.get("fatal"):
            self._stop_thinking_timer()
            self._continue_loop(outcome["fatal"])
            return
        if outcome.get("done"):
            self._stop_thinking_timer()
            text = outcome.get("done_text") or "Done."
            # Confirmation doctrine: a turn finished with ONLY a question
            # means the model asks before a consequential action. Pause with
            # the question — the user's reply restarts the chain (guidance).
            if text.strip().endswith("?"):
                self.command_history.append(f"Model asks: {text[:300]}")
                self.ui.add_text(text, prefix_html="<b>Vision:</b> ")
                self._set_state(VisionState.PAUSED_ON_ERROR)
                return
            self.command_history.append(f"Computer-use finished: {text[:300]}")
            self.ui.add_text(text, prefix_html="<b>Vision:</b> ")
            self._consecutive_errors = 0
            self._assessment_sent = False
            self._set_state(VisionState.IDLE)
            return
        if outcome.get("action_error"):
            self._stop_thinking_timer()
            self._continue_loop(outcome["action_error"])
            return
        executed = outcome.get("executed") or []
        self._cua_response_id = outcome.get("response_id")
        self._cua_call_id = outcome.get("call_id")
        self._last_screenshot_b64 = outcome.get("screenshot_b64")
        self._cua_turns += 1
        line = f"turn {self._cua_turns}: " + (
            ", ".join(executed) if executed else "screenshot")
        self.ui.add_message(f"<i>👁 {line}</i>")
        self._continue_loop(line)

    def _on_safety_needed(self, checks: list):
        worker = self._worker
        try:
            approved = bool(self.controller.ask_safety_approval(checks))
        except Exception as e:
            logging.error(f"Safety approval dialog failed: {e}")
            approved = False
        if worker is not None:
            worker.resolve_safety(approved)

    # -- provider / capture / performers ---------------------------------

    def provider_supports_cua(self) -> bool:
        try:
            return bool(self._provider().supports_computer_use())
        except Exception:
            return False

    def _provider(self):
        worker = getattr(self.controller, "llm_worker", None)
        provider = getattr(worker, "provider", None)
        if provider is None:
            raise RuntimeError("No LLM provider available for Computer Use.")
        return provider

    def _set_own_window_visible(self, visible: bool) -> bool:
        """Hides/shows our main window thread-safely. Returns True if WE hid it
        (so only then we re-show — a minimized window stays minimized)."""
        try:
            window = getattr(self.ui, "main_window", None)
            if window is None:
                return False
            from PySide6.QtCore import QMetaObject, Qt, QThread
            if not visible:
                if not window.isVisible() or window.isMinimized():
                    return False
            if QThread.currentThread() == window.thread():
                window.setVisible(visible)
            else:
                method = "show" if visible else "hide"
                QMetaObject.invokeMethod(
                    window, method, Qt.BlockingQueuedConnection)
            return not visible
        except Exception as e:
            logging.warning(f"own-window visibility failed: {e}")
            return False

    def capture_screenshot_b64(self):
        """Full-screen capture WITHOUT our window -> (base64 png, w, h).

        Restores the legacy get_clean_screenshot behavior: the Logos window
        must not cover screen elements on the shot the model decides from.
        """
        import mss
        from PIL import Image
        we_hid = self._set_own_window_visible(False)
        try:
            if we_hid:
                time.sleep(0.3)  # let the compositor redraw without us
            with mss.mss() as sct:
                raw = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        finally:
            if we_hid:
                self._set_own_window_visible(True)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii"), img.size[0], img.size[1]

    def _debug_flags(self) -> dict:
        """Reads the Debugging settings section (all default ON)."""
        try:
            cfg = (self.settings.get("omniparser", {}) or {})
        except Exception:
            cfg = {}
        return {
            "save_pre": bool(cfg.get("save_pre_intent_screenshot", True)),
            "save_post": bool(cfg.get("save_post_screenshot", True)),
            "save_trace": bool(cfg.get("save_trace_log", True)),
            "trace_path": cfg.get("trace_log_path", "logs/parser_trace.json"),
        }

    def _shots_dir(self) -> str:
        try:
            d = self.settings.get("APP_SCREENSHOTS_DIR", "screenshots")
        except Exception:
            d = "screenshots"
        try:
            import os
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def save_debug_shot(self, tag: str, turn_no: int, shot_b64: str,
                        when: str, annotate_with: list | None = None) -> str:
        """Saves a turn screenshot (post shots annotated, PIL red marks)."""
        import os
        from PIL import Image
        raw = base64.b64decode(shot_b64)
        img = Image.open(io.BytesIO(raw))
        if annotate_with:
            try:
                img = annotate_screenshot(img, annotate_with)
            except Exception as e:
                logging.warning(f"annotation failed: {e}")
        path = os.path.join(
            self._shots_dir(), f"cua_{tag}_turn{turn_no:02d}_{when}.png")
        img.save(path)
        logging.info(f"CUA debug shot saved: {path}")
        return path

    def trace_turn(self, tag: str, turn_no: int, actions: list,
                   had_safety: bool, shot_file: str | None) -> None:
        """Appends one JSON line per turn (respects save_trace_log)."""
        if not self._debug_flags().get("save_trace"):
            return
        import json
        import os
        flags = self._debug_flags()
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tag": tag,
            "turn": turn_no,
            "actions": [describe_action(a) for a in (actions or [])],
            "raw_actions": actions or [],
            "safety": had_safety,
            "screenshot": shot_file,
        }
        try:
            path = flags["trace_path"]
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logging.warning(f"trace write failed: {e}")

    def execute_action(self, action: dict) -> str | None:
        """
        Runs one structured computer-use action. Called on the worker
        thread. Returns a short summary; raises on failure (batch stops).
        """
        if not isinstance(action, dict) or "type" not in action:
            raise ValueError(f"Malformed computer action: {action!r}")
        kind = action["type"]
        pg = self.computer.pyautogui
        scale = 1.0  # native-resolution screenshots; see scale_point()

        if kind == "screenshot":
            return "screenshot"

        if kind in ("click", "double_click", "move"):
            x, y = scale_point(float(action["x"]), float(action["y"]), scale)
            keys = action.get("keys") or []
            held = []
            try:
                for k in keys:
                    kk = normalize_key(k)
                    pg.keyDown(kk)
                    held.append(kk)
                if kind == "move":
                    pg.moveTo(x, y)
                elif kind == "double_click":
                    pg.click(x, y, button=action.get("button", "left") or "left",
                             clicks=2, interval=0.1)
                else:
                    pg.click(x, y, button=action.get("button", "left") or "left")
            finally:
                for kk in reversed(held):
                    try:
                        pg.keyUp(kk)
                    except Exception:
                        pass
            return describe_action(action)

        if kind == "drag":
            path = action.get("path") or []
            if len(path) < 2:
                raise ValueError(f"drag needs >= 2 path points: {action!r}")
            pts = [scale_point(float(p["x"]), float(p["y"]), scale) for p in path]
            pg.moveTo(*pts[0])
            pg.mouseDown()
            try:
                for pt in pts[1:]:
                    pg.moveTo(*pt, duration=0.1)
            finally:
                pg.mouseUp()
            return describe_action(action)

        if kind == "scroll":
            x, y = scale_point(float(action.get("x", 0)), float(action.get("y", 0)), scale)
            pg.moveTo(x, y)
            # Pixel deltas -> wheel clicks (WHEEL_DELTA=120). Sign: assumed
            # screen-space (positive down/right); verify on the live test.
            v = int(round(float(action.get("scroll_y", 0)) / 120))
            h = int(round(float(action.get("scroll_x", 0)) / 120))
            if v:
                pg.scroll(-v)
            if h:
                try:
                    pg.hscroll(h)
                except Exception:
                    pass
            return describe_action(action)

        if kind == "keypress":
            keys = action.get("keys") or []
            if not keys:
                raise ValueError(f"keypress without keys: {action!r}")
            norm = [normalize_key(k) for k in keys]
            if len(norm) == 1:
                self._press_single(norm[0])
            else:
                pg.hotkey(*norm)
            return describe_action(action)

        if kind == "type":
            text = action.get("text", "")
            if text is None:
                raise ValueError(f"type without text: {action!r}")
            self._type_text(str(text))
            return describe_action(action)

        if kind == "wait":
            time.sleep(1.0)
            return "wait"

        raise ValueError(f"Unsupported computer action: {kind!r}")

    # -- low-level performers ---------------------------------------------

    def _press_single(self, key: str) -> None:
        import sys
        if sys.platform == "win32":
            try:
                from src.computer.tools.win_keys import press_key_pynput
                press_key_pynput(key)
                return
            except Exception as e:
                logging.warning(f"pynput press failed for '{key}', pyautogui fallback: {e}")
        self.computer.pyautogui.press(key)

    def _type_text(self, text: str) -> None:
        # Clipboard paste: fast, layout-independent (same approach as the
        # legacy write_text tool).
        import sys
        import pyperclip
        pg = self.computer.pyautogui
        original = None
        try:
            try:
                original = pyperclip.paste()
            except Exception:
                pass
            pyperclip.copy(text)
            time.sleep(0.2)
            if sys.platform == "darwin":
                pg.hotkey("command", "v")
            else:
                pg.hotkey("ctrl", "v")
            time.sleep(0.2)
        finally:
            if original is not None:
                try:
                    pyperclip.copy(original)
                except Exception:
                    pass

    # -- lifecycle ----------------------------------------------------------

    def build_prompts(self):
        return "", self.original_user_request or ""

    def _cleanup_task(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                worker.stop()
                worker.wait(3000)
            except Exception:
                pass
        self._cua_response_id = None
        self._cua_call_id = None

    def cancel_job_wait(self):
        pass

    # -- deprecated plan-era surface (controller/UI still reference it) -----

    def _removed(self, name: str):
        msg = (f"Vision plan workflow was removed in the agent-loop rebuild "
               f"({name}). Use the task input — the agent loops by itself.")
        logging.warning(msg)
        try:
            self.ui.add_message(f"<i>{msg}</i>")
        except Exception:
            pass
        return None

    def handle_plan_response(self, reasoning, tool_calls_obj, schema_name):
        return self._removed("handle_plan_response")

    def handle_fatal_error(self, error_message):
        return self._removed("handle_fatal_error")

    def handle_plan_approval(self):
        return self._removed("handle_plan_approval")

    def regenerate_plan(self):
        return self._removed("regenerate_plan")

    def clear_plan(self):
        return self._removed("clear_plan")

    def apply_edited_plan(self, edited):
        self._removed("apply_edited_plan")
        return False

    def pause_for_feedback(self):
        if self.state == VisionState.EXECUTING:
            self.pause_or_resume()

    def next_step(self):
        return self._removed("next_step")

    def _on_analysis_complete(self, *args, **kwargs):
        return None

    def _on_analysis_error(self, *args, **kwargs):
        return None
