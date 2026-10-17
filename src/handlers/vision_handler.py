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

# The only kwargs computer_use_turn() accepts. The worker request dict may
# carry extra local meta (tag, turn_no, debug_flags, ...); the allowlist
# keeps them from leaking into the provider call (TypeError).
# screenshot_b64 is injected by the worker itself (first turn only, and only
# when the provider declares wants_first_screenshot()).
_PROVIDER_KWARGS = frozenset({
    "task", "outputs", "previous_response_id", "instructions",
    "screenshot_b64",
})

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


def comm_log_static(direction: str, payload: dict):
    """Task-level entries into llm_communication.log (no worker needed)."""
    import json
    from datetime import datetime
    try:
        logger = logging.getLogger("llm_communication")
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "direction": direction,
            "mode": "Vision-CUA",
        }
        entry.update(payload)
        logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        pass


def scale_point(x: float, y: float, scale: float) -> tuple:
    """
    Maps model coordinates onto real screen pixels.

    Assumption (Anthropic rule): the model emits coordinates in the pixel
    space of the screenshot we sent. v1 sends native resolution, so scale
    is 1.0. If a vendor uses a normalized 0-1000 space, the test run will
    show it and scale becomes real_size/1000 via settings.
    """
    return (x * scale, y * scale)


def _annotation_font(font_path: str | None = None, size: int = 22):
    """Settings font first (FontComboBox path), then system fallbacks,
    then PIL's bitmap default. A default sentinel means: skip to fallbacks."""
    from PIL import ImageFont
    candidates = []
    if font_path and font_path not in ("default", "cv2_default"):
        candidates.append(font_path)
    candidates += ["arial.ttf", "DejaVuSans.ttf"]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def annotate_screenshot(img, actions: list, scale: float = 1.0,
                        font_path: str | None = None, font_size: int = 22,
                        padding: int = 6):
    """
    Draws what the model did onto a COPY of the screenshot (PIL only):
    red circles + labels for clicks, polylines for drags, corner stamp
    for keypress/type/wait. Returns the annotated copy.
    """
    from PIL import ImageDraw
    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)
    font = _annotation_font(font_path, font_size)
    red = (255, 0, 0)

    def dot(x, y, r=14, label=None):
        x, y = scale_point(float(x), float(y), scale)
        draw.ellipse([x - r, y - r, x + r, y + r], outline=red, width=4)
        draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=red)
        if label:
            draw.text((x + r + padding, y - r - padding),
                      label, fill=red, font=font)

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
    ask_needed = Signal(list)     # ask_user calls -> GUI question dialogs
    ensure_visible = Signal(float, float)  # point must not hide under us
    progress = Signal(str)

    def __init__(self, handler, provider, turn_request: dict, parent=None):
        super().__init__(parent)
        self._handler = handler
        self._provider = provider
        self._request = turn_request
        self._stop = False
        self._safety_event = threading.Event()
        self._safety_approved = False
        self._ask_event = threading.Event()
        self._ask_answers = None  # None = cancelled
        self._visible_event = threading.Event()
        self._visible_moved = False

    def resolve_ask(self, answers):
        """answers: list of {"call_id":..., "answer": str|None} or None."""
        self._ask_answers = answers
        self._ask_event.set()

    def stop(self):
        self._stop = True
        self._safety_event.set()

    def resolve_safety(self, approved: bool):
        self._safety_approved = approved
        self._safety_event.set()

    def _paused_spin(self, where: str) -> bool:
        """Returns False when the task was stopped while paused.

        Announces once per wait site — never a line every 0.4s (that
        flooded the chat when the user paused to type).
        """
        h = self._handler
        announced = False
        while h.state == VisionState.PAUSED and not self._stop:
            if not announced:
                self.progress.emit(f"Paused ({where}) — waiting for Resume…")
                announced = True
            time.sleep(0.4)
        return not self._stop and h.state != VisionState.IDLE

    def _comm_log(self, direction: str, payload: dict):
        """One JSON line into llm_communication.log (never base64 blobs)."""
        import json
        from datetime import datetime
        try:
            logger = logging.getLogger("llm_communication")
            try:
                _prov = self._provider.get_provider_id()
            except Exception:
                _prov = "?"
            entry = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "direction": direction,
                "mode": "Vision-CUA",
                "provider": _prov,
                "model": getattr(self._provider, "model", "?"),
            }
            entry.update(payload)
            logger.info(json.dumps(entry, ensure_ascii=False))
        except Exception:
            pass

    def run(self):
        outcome: dict
        # Only these keys may reach the provider. Everything else in the
        # request dict is local meta (tag/turn/debug/refs) — newly added
        # meta fields can never leak into computer_use_turn again.
        # Split BEFORE try: except-block references these, so they must
        # exist even if the first line inside try raises.
        protocol = {k: v for k, v in self._request.items()
                    if k in _PROVIDER_KWARGS}
        tag = self._request.get("tag", "cua")
        turn_no = self._request.get("turn_no", 0)
        flags = self._request.get("debug_flags") or {}
        prev_shot_file = self._request.get("prev_shot_file")
        # Google-style providers ground the very first turn on an opening
        # screenshot (their documented loop always sends one). Capture it
        # here, off the GUI thread — same path as the pre-action debug shot.
        # Providers that don't declare it never see the key (and their
        # signatures stay untouched).
        try:
            wants_shot = bool(self._provider.wants_first_screenshot())
        except Exception:
            wants_shot = False
        if (wants_shot and protocol.get("previous_response_id") is None
                and not protocol.get("outputs")
                and not protocol.get("screenshot_b64")):
            try:
                first_b64, _, _ = self._handler.capture_screenshot_b64()
                protocol["screenshot_b64"] = first_b64
            except Exception as e:
                logging.warning(f"opening screenshot failed: {e}")
        try:
            from src.llm.base_provider import set_log_ctx
            set_log_ctx(f"Vision-{tag}", turn_no)
        except Exception:
            pass
        try:
            if not self._paused_spin("before provider turn"):
                outcome = {"aborted": True}
                self.turn_done.emit(outcome)
                return
            self.progress.emit("Thinking (computer-use turn)…")
            first = protocol.get("previous_response_id") is None
            outs = protocol.get("outputs") or []
            self._comm_log("REQUEST", {
                "turn": turn_no,
                "task": protocol.get("task"),
                "instructions_chars": len(protocol.get("instructions") or ""),
                "instructions": protocol.get("instructions") if first else "<chain continues>",
                "previous_response_id": protocol.get("previous_response_id"),
                "opening_screenshot_chars": len(protocol.get("screenshot_b64") or ""),
                "outputs": [
                    {"kind": o.get("kind"), "call_id": o.get("call_id"),
                     "screenshot_bytes": len(o.get("screenshot_b64") or ""),
                     "output_text": (o.get("output_text") or "")[:300],
                     "ack": bool(o.get("acknowledged_safety_checks"))}
                    for o in outs
                ],
                "screenshot_sent_file": prev_shot_file,
            })
            t0 = time.perf_counter()
            turn = self._provider.computer_use_turn(**protocol)
            took = round(time.perf_counter() - t0, 1)
            self._comm_log("RESPONSE", {
                "turn": turn_no,
                "took_seconds": took,
                "response_id": turn.get("response_id"),
                "call_id": turn.get("call_id"),
                "actions": turn.get("actions"),
                "pending_safety_checks": turn.get("pending_safety_checks"),
                "ask": turn.get("ask"),
                "done_text": turn.get("done_text"),
            })

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
                follow["outputs"] = [
                    dict(o, acknowledged_safety_checks=ack)
                    if o.get("kind") == "computer" else dict(o)
                    for o in (follow.get("outputs") or [])
                ]
                self._comm_log("REQUEST", {
                    "turn": turn_no,
                    "replay_with_ack": True,
                    "acknowledged_safety_checks": ack,
                })
                t0 = time.perf_counter()
                turn = self._provider.computer_use_turn(**follow)
                took = round(time.perf_counter() - t0, 1)
                self._comm_log("RESPONSE", {
                    "turn": turn_no,
                    "replay_with_ack": True,
                    "took_seconds": took,
                    "response_id": turn.get("response_id"),
                    "call_id": turn.get("call_id"),
                    "actions": turn.get("actions"),
                    "pending_safety_checks": turn.get("pending_safety_checks"),
                    "ask": turn.get("ask"),
                    "done_text": turn.get("done_text"),
                })

            if not self._paused_spin("before execution"):
                outcome = {"aborted": True}
                self.turn_done.emit(outcome)
                return

            actions = turn.get("actions") or []
            done_text = turn.get("done_text")

            # Explicit ask_user calls: the model itself decided by meaning
            # that a human answer is needed. Resolve via GUI dialogs first;
            # answers ride the NEXT turn as function outputs (same chain).
            asks = turn.get("ask") or []
            function_outs: list = []
            asked_log: list = []
            if asks and not self._stop:
                self.progress.emit("Waiting for your answer…")
                self.ask_needed.emit(asks)
                self._ask_event.wait()
                if self._stop:
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                if self._ask_answers is None:
                    outcome = {
                        "ask_cancelled": True,
                        "asks": asks,
                        "response_id": turn.get("response_id"),
                    }
                    self._comm_log("CONSUMED", {
                        "turn": turn_no,
                        "status": "ask_cancelled",
                    })
                    self.turn_done.emit(outcome)
                    return
                by_id = {a.get("call_id"): a.get("question", "")
                         for a in asks}
                for ans in self._ask_answers:
                    function_outs.append({
                        "kind": "function",
                        "call_id": ans.get("call_id"),
                        "output_text": ans.get("answer", ""),
                    })
                    asked_log.append({
                        "question": by_id.get(ans.get("call_id"), ""),
                        "answer": ans.get("answer", ""),
                    })
                self._comm_log("CONSUMED", {
                    "turn": turn_no,
                    "asked": asked_log,
                })

            if flags.get("save_pre"):
                try:
                    pre_b64, _, _ = self._handler.capture_screenshot_b64()
                    self._handler.save_debug_shot(tag, turn_no, pre_b64, "pre")
                except Exception as e:
                    logging.warning(f"pre-action shot failed: {e}")

            if not actions and not (done_text or "").strip() and not function_outs:
                # Empty turn (no actions, no text, no questions): the model
                # stalled instead of acting. Must NOT count as done.
                outcome = {
                    "action_error": "**Error:** model returned an empty turn "
                                    "(no actions, no text) — stuck, not done.",
                    "executed": [],
                    "response_id": turn.get("response_id"),
                    "next_outputs": [],
                }
                self._handler.trace_turn(tag, turn_no, [], False, None)
                self._comm_log("CONSUMED", {
                    "turn": turn_no,
                    "status": "empty_turn",
                })
                self.turn_done.emit(outcome)
                return

            if not actions and not function_outs:
                outcome = {
                    "done": True,
                    "done_text": done_text or "",
                    "response_id": turn.get("response_id"),
                }
                self._handler.trace_turn(tag, turn_no, [], False, None)
                self._comm_log("CONSUMED", {
                    "turn": turn_no,
                    "status": "done",
                    "done_text": done_text,
                })
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
                    err_b64, _, _ = self._handler.capture_screenshot_b64()
                    err_outs = list(function_outs)
                    err_ids = turn.get("call_ids") or (
                        [turn.get("call_id")] if turn.get("call_id") else [])
                    for i, cid in enumerate(err_ids):
                        # Screenshot rides on the last item only (token cost).
                        item = {"kind": "computer", "call_id": cid}
                        if i == len(err_ids) - 1:
                            item["screenshot_b64"] = err_b64
                        err_outs.append(item)
                    outcome = {
                        "action_error": f"**Error:** {describe_action(action)} failed: {e}",
                        "executed": executed,
                        "response_id": turn.get("response_id"),
                        "next_outputs": err_outs,
                        "asked": asked_log,
                    }
                    self._handler.trace_turn(
                        tag, turn_no, executed_raw, False, None,
                        asked=asked_log)
                    self._comm_log("CONSUMED", {
                        "turn": turn_no,
                        "status": "action_error",
                        "executed": executed,
                        "asked": asked_log,
                        "error": outcome["action_error"],
                    })
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
            next_outputs = list(function_outs)
            ok_ids = turn.get("call_ids") or (
                [turn.get("call_id")] if turn.get("call_id") else [])
            for i, cid in enumerate(ok_ids):
                comp_out = {"kind": "computer", "call_id": cid}
                # Per-call own results (cursor text, zoom/member image)
                # win over the post shot; matched by position.
                if i < len(executed_raw) and isinstance(
                        executed_raw[i], dict):
                    res = executed_raw[i].get("_result") or {}
                    if res.get("text"):
                        comp_out["result_text"] = res["text"]
                    if res.get("image_b64"):
                        comp_out["result_image_b64"] = res["image_b64"]
                # Post screenshot rides on the last item only (token cost).
                if i == len(ok_ids) - 1:
                    comp_out["screenshot_b64"] = shot_b64
                next_outputs.append(comp_out)
            self._handler.trace_turn(
                tag, turn_no, executed_raw,
                bool(turn.get("pending_safety_checks")), shot_file,
                asked=asked_log)
            self._comm_log("CONSUMED", {
                "turn": turn_no,
                "executed": executed,
                "asked": asked_log,
                "post_screenshot_file": shot_file,
            })
            outcome = {
                "executed": executed,
                "response_id": turn.get("response_id"),
                "asked": asked_log,
                "shot_file": shot_file,
                "next_outputs": next_outputs,
            }
            self.turn_done.emit(outcome)
        except Exception as e:
            logging.error(f"CUA turn failed: {e}", exc_info=True)
            self._comm_log("ERROR", {
                "turn": turn_no,
                "error": f"{type(e).__name__}: {e}",
            })
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
    MAX_CUA_TURNS = MAX_CUA_TURNS  # fallback; live value from settings

    def _max_turns(self) -> int:
        """Turn cap, read live from Vision settings (default 20)."""
        try:
            value = int((self.settings.get("vision", {}) or {}).get(
                "max_turns_per_task", MAX_CUA_TURNS))
            return max(1, value)
        except Exception:
            return MAX_CUA_TURNS

    def __init__(self, controller):
        super().__init__(controller)
        self.computer = Computer(
            settings_manager=getattr(controller, "settings_manager", None),
            mode="vision",
            working_directory=getattr(controller, "working_directory", None),
        )
        # Chain state for the server-side conversation (previous_response_id).
        self._cua_response_id = None
        self._cua_outbox = []  # outputs answered into the next turn
        self._cua_turns = 0
        self._worker = None
        self._compact_owned = False
        self._visible_box = {}
        self._visible_event = threading.Event()
        self._pause_hist_mark = 0
        # Deprecated plan-era surface (controller/UI still reference it).
        self.action_plan = []

    # -- task entry ----------------------------------------------------

    def _enter_compact(self):
        """Shrinks to the compact strip for the task (progress stays visible,
        the strip barely covers anything). Only tracks self-initiated state."""
        try:
            if not getattr(self.ui, "is_compact", False):
                self.ui.switchToCompactMode()
                self._compact_owned = True
        except Exception as e:
            logging.warning(f"compact enter failed: {e}")

    def _exit_compact(self):
        """Restores full window if WE compacted it (never fights the user)."""
        try:
            if getattr(self, "_compact_owned", False):
                self._compact_owned = False
                if getattr(self.ui, "is_compact", False):
                    self.ui.switchToFullMode()
        except Exception as e:
            logging.warning(f"compact exit failed: {e}")

    def pause_or_resume(self):
        if self.state == VisionState.PAUSED:
            # Resume with notes: plain base resume would continue the old
            # chain WITHOUT the typed notes reaching the model (no text
            # injection in CUA). Restart with notes embedded instead.
            mark = getattr(self, "_pause_hist_mark", 0)
            notes = [h for h in self.command_history[mark:]
                     if "**User note" in h]
            try:
                draft = self.ui.user_input.toPlainText().strip()
            except Exception:
                draft = ""
            texts = notes + ([f"**User note (paused):** {draft}"] if draft else [])
            if texts:
                if draft:
                    self.ui.add_message(f"<b>You:</b> {draft}")
                    self.command_history.append(
                        f"**User note (paused):** {draft}")
                    try:
                        self.ui.user_input.clear()
                    except Exception:
                        pass
                base = self.original_user_request or ""
                combined = (f"{base}\n\nUser notes written while paused "
                            f"(take into account and continue):\n"
                            + "\n".join(texts)).strip()
                self._set_state(VisionState.EXECUTING)
                self.controller._reenter_agent_mode(self)
                self.ui.add_message("<i>Execution resumed with your notes.</i>")
                self.ui.set_paused(False)
                logging.info("Vision CUA resuming with paused notes embedded.")
                self.stop()
                self.start_task(combined)
                return
        super().pause_or_resume()
        try:
            if self.state == VisionState.PAUSED:
                # Pausing must hand the keyboard back: the thinking UI
                # locks the input in Vision mode, and a paused user wants
                # to type guidance. The next turn re-shows thinking.
                self._pause_hist_mark = len(self.command_history)
                self._stop_thinking_timer()
                if getattr(self, "_compact_owned", False):
                    self._exit_compact()
            elif (self.state == VisionState.EXECUTING
                    and self.original_user_request
                    and not getattr(self.ui, "is_compact", False)):
                self._enter_compact()
        except Exception:
            pass

    def ensure_point_visible(self, x: float, y: float) -> bool:
        """Called from the worker thread before acting at a screen point.

        If our compact strip covers the target, it is relocated (GUI side)
        and the worker should let the compositor settle. Returns True when
        the window moved.
        """
        worker = self._worker
        if worker is None:
            return self._ensure_visible_do(x, y)
        self._visible_box = {}
        self._visible_event = threading.Event()
        worker.ensure_visible.emit(x, y)
        self._visible_event.wait(timeout=5.0)
        return bool(self._visible_box.get("moved"))

    def _ensure_visible_do(self, x: float, y: float) -> bool:
        try:
            ui = self.ui
            if getattr(ui, "is_compact", False) and ui.is_click_colliding(
                    int(x), int(y)):
                ui.relocate_compact_window()
                return True
        except Exception as e:
            logging.warning(f"visibility check failed: {e}")
        return False

    def _on_ensure_visible(self, x: float, y: float):
        try:
            self._visible_box = {"moved": self._ensure_visible_do(x, y)}
        except Exception:
            self._visible_box = {"moved": False}
        try:
            self._visible_event.set()
        except Exception:
            pass

    def _steer_with_text(self, user_text: str):
        """Restarts the chain with user text embedded in the task.

        The CUA protocol has no free-text injection: continuing the old
        chain would leave the text invisible to the model (local history
        only). So every steering message becomes a restarted chain.
        """
        base = self.original_user_request or ""
        combined = (f"{base}\n\nUser message during the task "
                    f"(take it into account and continue): "
                    f"{user_text}".strip())
        logging.info("Vision CUA restarting chain with user text.")
        self.stop()
        self.start_task(combined)

    def start_task(self, user_text: str, attachments=None):
        if attachments:
            logging.info(f"Vision CUA ignoring {len(attachments)} attachment(s) (v1).")
        if self.state == VisionState.PAUSED_ON_ERROR and (user_text or "").strip():
            # Steering after an error pause: plain _continue_loop would
            # continue the old chain WITHOUT the text reaching the model.
            self._steer_with_text(user_text.strip())
            return
        if self.state == VisionState.EXECUTING and (user_text or "").strip():
            # Mid-flight hint: same invisibility problem — restart with it.
            self._steer_with_text(user_text.strip())
            return
        self._cua_response_id = None
        self._cua_outbox = []
        self._cua_turns = 0
        self._cua_tag = time.strftime("%H%M%S")
        self._last_shot_file = None
        try:
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[1]
                display = f"{mon['width']}x{mon['height']}"
        except Exception:
            display = "?"
        try:
            model = self._provider().model
        except Exception:
            model = "?"
        comm_log_static("TASK", {
            "tag": self._cua_tag,
            "task": user_text,
            "model": model,
            "display": display,
        })
        try:
            self.ui.add_message(
                "<i>👁 Computer use started — please don't touch the mouse or "
                "keyboard until it finishes (any movement breaks clicks). "
                "Stop button aborts.</i>")
        except Exception:
            pass
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

    def _cua_shell_allowed(self) -> bool:
        """Live read of the Vision settings checkbox (default OFF)."""
        try:
            cfg = self.settings.get("vision", {}) or {}
            return bool(cfg.get("allow_shell_commands", False))
        except Exception:
            return False

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
        base_text = self._cua_system_cache or ""
        if self._cua_shell_allowed():
            return base_text + (
                "\n- Ground-truth verification: when a task needs facts the "
                "screenshots can't prove (folder sizes, file counts, disk "
                "state), you may open a terminal yourself (Win+R, type "
                "powershell, Enter), type the checking command, and read "
                "its output from the next screenshot. Prefer read-only "
                "commands. Anything modifying or deleting falls under the "
                "destruction rule above and requires user approval first."
            )
        return base_text + (
            "\n- Work through the graphical interface only. Do not open "
            "terminal or console windows and do not type shell commands."
        )

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

        turn_cap = self._max_turns()
        if self._cua_turns >= turn_cap:
            self.ui.add_message(
                f"<i>Stopped after {turn_cap} computer-use turns "
                f"without finishing. Tell me how to proceed.</i>")
            self._exit_compact()
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
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return

        if not self.provider_supports_cua():
            self.ui.add_message(
                "<i>Current provider does not support Computer Use. "
                "Switch to OpenAI, Anthropic or Google (Gemini 3.x) "
                "to use Vision mode.</i>")
            self.stop()
            return

        self._start_thinking_timer("Thinking (computer use)…")
        debug_flags = self._debug_flags()
        if self._cua_response_id is None:
            # First turn, or error retry before any response arrived.
            request = {"task": self.original_user_request,
                       "instructions": self._cua_system_prompt()}
        else:
            request = {
                "previous_response_id": self._cua_response_id,
                "outputs": list(getattr(self, "_cua_outbox", None) or []),
                "prev_shot_file": getattr(self, "_last_shot_file", None),
            }
        if not request.get("task") and not request.get("outputs"):
            self._continue_loop(
                "**Error:** nothing to send (no task, no outputs).")
            return
        request.update({
            "tag": getattr(self, "_cua_tag", "cua"),
            "turn_no": self._cua_turns + 1,
            "debug_flags": debug_flags,
        })
        self._enter_compact()
        self._worker = CuaTurnWorker(self, self._provider(), request)
        self._worker.turn_done.connect(self._on_turn_done)
        self._worker.safety_needed.connect(self._on_safety_needed)
        self._worker.ask_needed.connect(self._on_ask_needed)
        self._worker.ensure_visible.connect(self._on_ensure_visible)
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
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return
        if outcome.get("fatal"):
            self._stop_thinking_timer()
            self._continue_loop(outcome["fatal"])
            return
        if outcome.get("ask_cancelled"):
            self.ui.add_message(
                "<i>Question dismissed — paused. Type guidance to steer, "
                "or Resume to continue.</i>")
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return
        if outcome.get("done"):
            self._stop_thinking_timer()
            text = outcome.get("done_text") or "Done."
            # Done text is a report, nothing more. Questions to the human
            # go through the ask_user tool (explicit call) or safety flags
            # (native channel) — never through text matching.
            self.command_history.append(f"Computer-use finished: {text[:300]}")
            self.ui.add_text(text, prefix_html="<b>Vision:</b> ")
            self._consecutive_errors = 0
            self._assessment_sent = False
            logging.info("task-done: restoring full mode")
            self._exit_compact()
            logging.info("task-done: full mode restored, going IDLE")
            self._set_state(VisionState.IDLE)
            return
        if outcome.get("action_error"):
            self._stop_thinking_timer()
            self._continue_loop(outcome["action_error"])
            return
        executed = outcome.get("executed") or []
        asked = outcome.get("asked") or []
        self._cua_response_id = outcome.get("response_id")
        self._cua_outbox = outcome.get("next_outputs") or []
        self._last_shot_file = outcome.get("shot_file")
        self._cua_turns += 1
        parts = list(executed)
        parts += [f"asked: {a.get('question', '')[:80]}"
                  for a in asked]
        line = f"turn {self._cua_turns}: " + (
            ", ".join(parts) if parts else "screenshot")
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

    def _on_ask_needed(self, asks: list):
        """One input dialog per question (GUI thread). Cancel pauses."""
        worker = self._worker
        answers: list | None = []
        try:
            for item in asks or []:
                question = item.get("question", "")
                self.ui.add_text(
                    question, prefix_html="<b>Vision asks:</b> ")
                answer = self.controller.ask_user_text(question)
                if answer is None:
                    answers = None
                    break
                answers.append({"call_id": item.get("call_id"),
                                "answer": answer})
        except Exception as e:
            logging.error(f"ask dialog failed: {e}")
            answers = None
        if worker is not None:
            worker.resolve_ask(answers)

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

    def _set_own_window_visible(self, visible: bool) -> None:
        """Makes our main window transparent/opaque thread-safely.

        Opacity (not hide/show): after a silent full-process exit that
        coincided with hide/show cycling, the window is never unmapped —
        DWM just composites nothing while transparent.
        """
        try:
            window = getattr(self.ui, "main_window", None)
            if window is None:
                return
            from PySide6.QtCore import Q_ARG, QMetaObject, Qt, QThread
            if QThread.currentThread() == window.thread():
                window.set_visibility_for_screenshot(visible)
            else:
                QMetaObject.invokeMethod(
                    window, "set_visibility_for_screenshot",
                    Qt.BlockingQueuedConnection,
                    Q_ARG(bool, visible))
        except Exception as e:
            logging.warning(f"own-window visibility failed: {e}")

    def _shot_max_width(self) -> int:
        """Max sent width, 0 = native. Vendors normalize anyway (OpenAI:
        strong results at 1440x900/1600x900); smaller = fewer tokens."""
        try:
            return int((self.settings.get("vision", {}) or {}).get(
                "screenshot_max_width", 1600) or 0)
        except Exception:
            return 1600

    def capture_screenshot_b64(self):
        """Full-screen capture WITHOUT our window -> (base64 png, w, h).

        The Logos window must not cover screen elements on the shot the
        model decides from. Done via opacity so the window is never
        unmapped (no taskbar/focus churn, no recreation risk).

        Downscales to screenshot_max_width (aspect kept); model coords
        then live in sent-image space — execute_action scales them back
        via self._shot_scale (Anthropic rule, OpenAI recipes agree).
        """
        import mss
        from PIL import Image
        self._set_own_window_visible(False)
        try:
            time.sleep(0.3)  # let DWM recomposite without us
            with mss.mss() as sct:
                raw = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        finally:
            self._set_own_window_visible(True)
        native_w = img.size[0]
        max_w = self._shot_max_width()
        if max_w and native_w > max_w:
            img = img.resize((max_w, int(img.size[1] * max_w / native_w)),
                             Image.LANCZOS)
        self._shot_scale = native_w / img.size[0] if img.size[0] else 1.0
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

    def _shots_dir(self, tag: str | None = None) -> str:
        """Per-task folder: screenshots/cua_<tag>/ (survives startup wipe)."""
        try:
            base = self.settings.get("APP_SCREENSHOTS_DIR", "screenshots")
        except Exception:
            base = "screenshots"
        import os
        d = os.path.join(base, f"cua_{tag or 'manual'}")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def _annotation_style(self) -> dict:
        """Font/size/padding from Vision settings (Annotation Settings)."""
        try:
            cfg = self.settings.get("omniparser", {}) or {}
        except Exception:
            cfg = {}
        try:
            size = int(cfg.get("font_size", 22) or 22)
        except Exception:
            size = 22
        try:
            padding = int(cfg.get("text_padding", 6) or 6)
        except Exception:
            padding = 6
        return {"font_path": cfg.get("font_path"),
                "font_size": size, "padding": padding}

    def save_debug_shot(self, tag: str, turn_no: int, shot_b64: str,
                        when: str, annotate_with: list | None = None) -> str:
        """Saves a turn screenshot (post shots annotated, PIL red marks)."""
        import os
        from PIL import Image
        raw = base64.b64decode(shot_b64)
        img = Image.open(io.BytesIO(raw))
        if annotate_with:
            try:
                style = self._annotation_style()
                img = annotate_screenshot(img, annotate_with, **style)
            except Exception as e:
                logging.warning(f"annotation failed: {e}")
        path = os.path.join(
            self._shots_dir(tag), f"turn{turn_no:02d}_{when}.png")
        img.save(path)
        logging.info(f"CUA debug shot saved: {path}")
        return path

    def trace_turn(self, tag: str, turn_no: int, actions: list,
                   had_safety: bool, shot_file: str | None,
                   asked: list | None = None) -> None:
        """One JSON line per turn into the single app timeline
        (respects save_trace_log). Lands in app.log via the llm_communication
        logger — no separate trace file anymore."""
        if not self._debug_flags().get("save_trace"):
            return
        import json
        from datetime import datetime
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "direction": "TRACE",
            "mode": "Vision-CUA",
            "tag": tag,
            "turn": turn_no,
            "actions": [describe_action(a) for a in (actions or [])],
            "raw_actions": actions or [],
            "safety": had_safety,
            "asked": asked or [],
            "screenshot": shot_file,
        }
        try:
            logging.getLogger("llm_communication").info(
                json.dumps(record, ensure_ascii=False))
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
        # Mouse AND held keys go through pynput (cross-platform, one stack).
        # pyautogui's input proved flaky on this setup (dropped Win combos,
        # layout-dependent typing); only screenshots still use mss/PIL.
        from pynput.mouse import Button as _Btn, Controller as _MCtl
        mouse = _MCtl()
        scale = getattr(self, "_shot_scale", 1.0) or 1.0
        _buttons = {"left": _Btn.left, "middle": _Btn.middle,
                    "right": _Btn.right}

        if kind == "screenshot":
            shot_b64, _, _ = self.capture_screenshot_b64()
            action["_result"] = {"image_b64": shot_b64}
            return "screenshot"

        if kind == "cursor_position":
            from pynput.mouse import Controller as _MCtl
            pos = _MCtl().position
            action["_result"] = {"text": f"X={pos[0]}, Y={pos[1]}"}
            return f"cursor_position({pos[0]}, {pos[1]})"

        if kind == "zoom":
            region = action.get("region") or [0, 0, 100, 100]
            x0, y0, x1, y1 = (int(float(v)) for v in list(region)[:4])
            shot_b64, _, _ = self.capture_screenshot_b64()
            import base64 as _b64
            import io as _io
            from PIL import Image as _Image
            img = _Image.open(_io.BytesIO(_b64.b64decode(shot_b64)))
            w, h = img.size
            box = (max(0, x0), max(0, y0), min(w, x1), min(h, y1))
            crop = img.crop(box) if box[2] > box[0] and box[3] > box[1] else img
            buf = _io.BytesIO()
            crop.save(buf, format="PNG")
            action["_result"] = {
                "image_b64": _b64.b64encode(buf.getvalue()).decode("ascii")}
            return f"zoom({x0}, {y0}, {x1}, {y1})"

        if kind == "left_mouse_down":
            from pynput.mouse import Button as _Btn, Controller as _MCtl
            _MCtl().press(_Btn.left)
            return "left_mouse_down"

        if kind == "left_mouse_up":
            from pynput.mouse import Button as _Btn, Controller as _MCtl
            _MCtl().release(_Btn.left)
            return "left_mouse_up"

        if kind == "hold_key":
            keys = action.get("keys") or ([action.get("key")]
                                          if action.get("key") else [])
            try:
                duration = min(float(action.get("duration", 1.0)), 30.0)
            except Exception:
                duration = 1.0
            norm = [normalize_key(k) for k in keys] or ["shift"]
            kbd = self._kbd()
            pressed = []
            try:
                for kk in norm:
                    key = self._pyn_key(kk)
                    kbd.press(key)
                    pressed.append(key)
                time.sleep(duration)
            finally:
                for key in reversed(pressed):
                    try:
                        kbd.release(key)
                    except Exception:
                        pass
            return f"hold_key({','.join(norm)}, {duration}s)"

        if kind in ("click", "double_click", "triple_click", "move"):
            x, y = scale_point(float(action["x"]), float(action["y"]), scale)
            if self.ensure_point_visible(x, y):
                time.sleep(0.6)  # compositor settle after relocate
            button = _buttons.get(
                (action.get("button", "left") or "left").lower())
            if button is None and kind != "move":
                raise ValueError(f"Unsupported mouse button: {action.get('button')!r}")
            keys = action.get("keys") or []
            held = []
            try:
                for k in keys:
                    kk = self._pyn_key(k)
                    self._kbd().press(kk)
                    held.append(kk)
                mouse.position = (int(x), int(y))
                if kind == "move":
                    pass
                else:
                    presses = {"double_click": 2, "triple_click": 3}.get(kind, 1)
                    for i in range(presses):
                        mouse.press(button)
                        mouse.release(button)
                        if i < presses - 1:
                            time.sleep(0.1)
            finally:
                kbd = self._kbd()
                for kk in reversed(held):
                    try:
                        kbd.release(kk)
                    except Exception:
                        pass
            return describe_action(action)

        if kind == "drag":
            path = action.get("path") or []
            if len(path) < 2:
                raise ValueError(f"drag needs >= 2 path points: {action!r}")
            pts = [scale_point(float(p["x"]), float(p["y"]), scale) for p in path]
            if any(self.ensure_point_visible(px, py) for px, py in pts):
                time.sleep(0.6)  # compositor settle after relocate
            mouse.position = (int(pts[0][0]), int(pts[0][1]))
            mouse.press(_Btn.left)
            try:
                for pt in pts[1:]:
                    mouse.position = (int(pt[0]), int(pt[1]))
                    time.sleep(0.02)
            finally:
                mouse.release(_Btn.left)
            return describe_action(action)

        if kind == "scroll":
            x, y = scale_point(float(action.get("x", 0)), float(action.get("y", 0)), scale)
            if self.ensure_point_visible(x, y):
                time.sleep(0.6)  # compositor settle after relocate
            mouse.position = (int(x), int(y))
            # Pixel deltas -> wheel clicks (WHEEL_DELTA=120). Sign verified
            # live: positive scroll_y goes DOWN. pynput: dy>0 is up.
            v = int(round(float(action.get("scroll_y", 0)) / 120))
            h = int(round(float(action.get("scroll_x", 0)) / 120))
            if v or h:
                mouse.scroll(h, -v)
            return describe_action(action)

        if kind == "keypress":
            keys = action.get("keys") or []
            if not keys:
                raise ValueError(f"keypress without keys: {action!r}")
            norm = [normalize_key(k) for k in keys]
            if len(norm) == 1:
                self._press_single(norm[0])
            else:
                from src.computer.tools.win_keys import hotkey_pynput
                hotkey_pynput(norm)
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

    # -- low-level performers (pynput throughout; pyautogui input is dead
    # on this setup and must not be used for acting) ---------------------

    def _kbd(self):
        """Cached pynput keyboard controller."""
        kbd = getattr(self, "_kbd_controller", None)
        if kbd is None:
            from pynput.keyboard import Controller
            kbd = Controller()
            self._kbd_controller = kbd
        return kbd

    @staticmethod
    def _pyn_key(name: str):
        """normalize_key() names -> pynput keys (mirrors win_keys mapping)."""
        from pynput.keyboard import Key, KeyCode
        table = {
            "enter": Key.enter, "tab": Key.tab, "esc": Key.esc,
            "escape": Key.esc, "space": Key.space, "backspace": Key.backspace,
            "delete": Key.delete, "up": Key.up, "down": Key.down,
            "left": Key.left, "right": Key.right, "home": Key.home,
            "end": Key.end, "pageup": Key.page_up, "pagedown": Key.page_down,
            "insert": Key.insert, "shift": Key.shift, "ctrl": Key.ctrl,
            "alt": Key.alt, "win": Key.cmd, "capslock": Key.caps_lock,
        }
        for i in range(1, 25):
            table[f"f{i}"] = getattr(Key, f"f{i}", None)
        key = table.get(str(name).lower())
        if key is None:
            if len(str(name)) == 1:
                return KeyCode.from_char(str(name))
            raise ValueError(f"Cannot map key '{name}' for pynput.")
        return key

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
        # legacy write_text tool). Paste chord via pynput, not pyautogui.
        import sys
        import pyperclip
        from src.computer.tools.win_keys import hotkey_pynput
        original = None
        try:
            try:
                original = pyperclip.paste()
            except Exception:
                pass
            pyperclip.copy(text)
            time.sleep(0.2)
            if sys.platform == "darwin":
                hotkey_pynput(["command", "v"])
            else:
                hotkey_pynput(["ctrl", "v"])
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
        self._exit_compact()
        self._cua_response_id = None
        self._cua_outbox = []

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
