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

See legacy/vision_2026-10-15/ for the pre-rebuild implementation.
"""
import base64
import html
import io
import logging
import threading
import time

from PySide6.QtCore import QThread, Signal

from states import VisionState
from src.browser.policy import is_sensitive_text
from src.computer.computer import Computer
from src.computer.input_access import InputBlockedError
from src.computer.tools.constants import KEYBOARD_KEYS
from src.computer.vision_policy import (action_point, active_window,
                                        classify_action, redact_action,
                                        redact_answer)
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
MAX_TASK_SECONDS = 1800
MAX_ACTIONS_PER_TURN = 20
ACTION_TIMEOUT_SECONDS = 30
MAX_WAIT_SECONDS = 30
NETWORK_HINT = "cua-turn"


class CuaActionCancelledError(Exception):
    """Raised inside an action when Stop/Pause superseded it."""


class CuaBudgetExceededError(Exception):
    """Raised when the wall-clock or per-turn action budget is exhausted."""

# The only kwargs computer_use_turn() accepts. The worker request dict may
# carry extra local meta (tag, turn_no, debug_flags, ...); the allowlist
# keeps them from leaking into the provider call (TypeError).
# screenshot_b64 is injected by the worker itself (first turn only, and only
# when the provider declares wants_first_screenshot()).
_PROVIDER_KWARGS = frozenset({
    "task", "outputs", "previous_response_id", "instructions",
    "screenshot_b64", "launch_apps",
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
        return f"type(<redacted:{len(text)} chars>)"
    return kind


def _action_signature(action: dict) -> str:
    """Stable, redacted identity used to verify a safety replay."""
    import json
    try:
        clean = {k: v for k, v in (action or {}).items()
                 if not str(k).startswith("_")}
        return json.dumps(clean, sort_keys=True, ensure_ascii=False)
    except Exception:
        return repr(action)


def _actions_signature(actions) -> tuple:
    return tuple(_action_signature(a) for a in (actions or []))


def _turn_call_ids(turn: dict) -> list:
    ids = turn.get("call_ids")
    if isinstance(ids, list) and ids:
        return [cid for cid in ids if cid]
    cid = turn.get("call_id")
    return [cid] if cid else []


def _ack_outputs(call_ids, checks, shot_b64, function_outs=None) -> list:
    """Outputs that acknowledge native safety checks for THIS turn's calls.

    The old code acked the previous turn's outbox, so the flagged call was
    never acknowledged. Materialize the current call ids instead.
    """
    ack = [{"id": c.get("id"), "code": c.get("code"),
            "message": c.get("message")} for c in (checks or [])]
    outs = list(function_outs or [])
    for index, cid in enumerate(call_ids or []):
        item = {"kind": "computer", "call_id": cid,
                "acknowledged_safety_checks": ack}
        if index == len(call_ids) - 1 and shot_b64:
            item["screenshot_b64"] = shot_b64
        outs.append(item)
    return outs


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
    usage_ready = Signal(dict)

    def __init__(self, handler, provider, turn_request: dict, parent=None,
                 generation: int = 0):
        super().__init__(parent)
        self._handler = handler
        self._provider = provider
        self._request = turn_request
        self._generation = int(generation)
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
        """Wakes EVERY worker wait site, not just safety approval."""
        self._stop = True
        self._safety_event.set()
        self._ask_event.set()
        self._visible_event.set()

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
                "launch_apps": protocol.get("launch_apps"),
                "opening_screenshot_chars": len(protocol.get("screenshot_b64") or ""),
                "outputs": [
                    {"kind": o.get("kind"), "call_id": o.get("call_id"),
                     "screenshot_bytes": len(o.get("screenshot_b64") or ""),
                     "output_text": (
                         "<redacted>" if o.get("kind") == "function"
                         else (o.get("output_text") or "")[:300]),
                     "ack": bool(o.get("acknowledged_safety_checks"))}
                    for o in outs
                ],
                "screenshot_sent_file": prev_shot_file,
            })
            t0 = time.perf_counter()
            turn = self._provider.computer_use_turn(**protocol)
            took = round(time.perf_counter() - t0, 1)
            usage = getattr(self._provider, "last_usage", None)
            if usage:
                payload = dict(usage)
                payload["purpose"] = "completion"
                self.usage_ready.emit(payload)
                self._provider.last_usage = None
            self._comm_log("RESPONSE", {
                "turn": turn_no,
                "took_seconds": took,
                "response_id": turn.get("response_id"),
                "call_id": turn.get("call_id"),
                "actions": [redact_action(a)
                            for a in (turn.get("actions") or [])],
                "pending_safety_checks": turn.get("pending_safety_checks"),
                "ask": turn.get("ask"),
                "launch": turn.get("launch"),
                "done_text": turn.get("done_text"),
            })

            actions = turn.get("actions") or []
            done_text = turn.get("done_text")
            if not self._paused_spin("before acting"):
                self.turn_done.emit({"aborted": True})
                return

            # Explicit ask_user calls: resolve them BEFORE a safety replay so
            # the same follow-up can answer both the question and the flagged
            # computer call. The answer rides the next turn as function output.
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
                    question = by_id.get(ans.get("call_id"), "")
                    function_outs.append({
                        "kind": "function",
                        "call_id": ans.get("call_id"),
                        "output_text": ans.get("answer", ""),
                    })
                    asked_log.append({
                        "question": question,
                        "answer": redact_answer(question,
                                                ans.get("answer", "")),
                    })
                self._comm_log("CONSUMED", {
                    "turn": turn_no,
                    "asked": asked_log,
                })
                if not self._paused_spin("after question"):
                    self.turn_done.emit({"aborted": True})
                    return

            # launch_app calls: deterministic, limited to the user's
            # Applications list. Execute them before the computer actions
            # so the follow-up screenshot shows the app the model asked for,
            # and answer each call as a function output on the next turn.
            launch_calls = turn.get("launch") or []
            launched: list = []
            launched_log: list = []
            for call in launch_calls:
                if self._stop:
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                if self._handler._cua_wall_clock_exceeded():
                    outcome = {
                        "budget_exceeded": "**Stopped:** Vision task time "
                                            "budget exhausted.",
                        "executed": [],
                        "launched": launched,
                    }
                    self.turn_done.emit(outcome)
                    return
                if not self._paused_spin("before launching an app"):
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                app_name = str(call.get("app_name") or "").strip()
                result = self._handler.launch_cua_app(app_name)
                launched.append(f"launch_app({app_name or '?'})")
                launched_log.append({"app": app_name, "result": result})
                function_outs.append({
                    "kind": "function",
                    "call_id": call.get("call_id"),
                    "function_name": "launch_app",
                    "output_text": result,
                })

            checks = turn.get("pending_safety_checks") or []
            pre_checks = self._handler.classify_cua_actions(actions)
            hard_blocks = [c for c in pre_checks if c.get("hard")]
            if hard_blocks:
                message = "; ".join(c.get("message", "blocked") for c in hard_blocks)
                outcome = {
                    "policy_blocked": f"**Blocked:** {message}",
                    "response_id": turn.get("response_id"),
                    "executed": [],
                }
                self._comm_log("CONSUMED", {
                    "turn": turn_no,
                    "status": "policy_blocked",
                    "checks": hard_blocks,
                })
                self.turn_done.emit(outcome)
                return

            approval = list(checks) + [
                c for c in pre_checks if not c.get("hard")]
            if approval and not self._stop:
                self.safety_needed.emit(approval)
                self._safety_event.wait()
                if self._stop:
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                if not self._safety_approved:
                    outcome = {"denied": True, "checks": approval}
                    self.turn_done.emit(outcome)
                    return
                if checks:
                    # Native safety replay: ack THIS turn's call ids, not the
                    # previous outbox. The current actions have not run yet.
                    shot_ack, _, _ = self._handler.capture_screenshot_b64()
                    ack_ids = _turn_call_ids(turn)
                    if not turn.get("response_id") or not ack_ids:
                        outcome = {
                            "fatal": "**Error:** safety replay is impossible "
                                     "(missing response_id/call_id); task paused."
                        }
                        self.turn_done.emit(outcome)
                        return
                    follow = {
                        "previous_response_id": turn.get("response_id"),
                        "outputs": _ack_outputs(
                            ack_ids, checks, shot_ack, function_outs),
                        "launch_apps": self._handler.cua_launch_names(),
                    }
                    self._comm_log("REQUEST", {
                        "turn": turn_no,
                        "replay_with_ack": True,
                        "call_ids": ack_ids,
                        "acknowledged_safety_checks": checks,
                    })
                    t0 = time.perf_counter()
                    replayed = self._provider.computer_use_turn(**follow)
                    took = round(time.perf_counter() - t0, 1)
                    self._comm_log("RESPONSE", {
                        "turn": turn_no,
                        "replay_with_ack": True,
                        "took_seconds": took,
                        "response_id": replayed.get("response_id"),
                        "call_id": replayed.get("call_id"),
                        "actions": [redact_action(a) for a in
                                    (replayed.get("actions") or [])],
                        "pending_safety_checks":
                            replayed.get("pending_safety_checks"),
                        "ask": replayed.get("ask"),
                        "launch": replayed.get("launch"),
                        "done_text": replayed.get("done_text"),
                    })
                    if replayed.get("pending_safety_checks"):
                        outcome = {
                            "fatal": "**Error:** the provider repeated its safety "
                                     "check after approval; task paused."
                        }
                        self.turn_done.emit(outcome)
                        return
                    if replayed.get("ask"):
                        outcome = {
                            "fatal": "**Error:** the provider asked a new question "
                                     "during safety replay; task paused."
                        }
                        self.turn_done.emit(outcome)
                        return
                    if replayed.get("launch"):
                        outcome = {
                            "fatal": "**Error:** the provider requested a new "
                                     "application launch during safety replay; "
                                     "task paused."
                        }
                        self.turn_done.emit(outcome)
                        return
                    if _actions_signature(replayed.get("actions") or []) != \
                            _actions_signature(actions):
                        outcome = {
                            "fatal": "**Error:** safety replay returned different "
                                     "actions than the approved ones; refusing to run."
                        }
                        self.turn_done.emit(outcome)
                        return
                    turn = replayed
                    actions = turn.get("actions") or []
                    done_text = turn.get("done_text")

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

            max_actions = self._handler._max_actions_per_turn()
            if len(actions) > max_actions:
                outcome = {
                    "budget_exceeded": (
                        f"**Stopped:** model returned {len(actions)} actions in "
                        f"one turn; the limit is {max_actions}. Split the step "
                        f"into smaller batches."),
                    "executed": [],
                    "launched": launched,
                    "response_id": turn.get("response_id"),
                }
                self._comm_log("CONSUMED", {
                    "turn": turn_no,
                    "status": "budget_exceeded",
                    "actions": len(actions),
                    "limit": max_actions,
                })
                self.turn_done.emit(outcome)
                return

            executed: list = []
            executed_raw: list = []
            approved_local = {
                (c.get("action_index"), c.get("code"))
                for c in pre_checks if not c.get("hard")
            }
            for index, action in enumerate(actions):
                if self._stop:
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                if self._handler._cua_wall_clock_exceeded():
                    outcome = {
                        "budget_exceeded": "**Stopped:** Vision task time "
                                            "budget exhausted.",
                        "executed": executed,
                    }
                    self.turn_done.emit(outcome)
                    return
                if not self._paused_spin("between actions"):
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return

                # Re-check with the CURRENT foreground window: a previous
                # action may have opened a terminal, UAC prompt or payment
                # page that was not visible when the batch was approved.
                point_getter = getattr(
                    self._handler, "cua_screen_point", None)
                screen_point = (point_getter(action)
                                if callable(point_getter) else None)
                now_checks = classify_action(
                    action, self._handler.cua_context(action),
                    allowed_apps=self._handler.cua_gate_apps(),
                    shell_allowed=self._handler._cua_shell_allowed(),
                    system_surfaces_allowed=(
                        self._handler._cua_system_surfaces_allowed()),
                    screen_point=screen_point,
                    action_index=index)
                hard_now = [c for c in now_checks if c.get("hard")]
                if hard_now:
                    message = "; ".join(
                        c.get("message", "blocked") for c in hard_now)
                    outcome = {
                        "policy_blocked": f"**Blocked:** {message}",
                        "executed": executed,
                        "response_id": turn.get("response_id"),
                    }
                    self._comm_log("CONSUMED", {
                        "turn": turn_no,
                        "status": "policy_blocked",
                        "checks": hard_now,
                    })
                    self.turn_done.emit(outcome)
                    return
                for check in now_checks:
                    if check.get("hard"):
                        continue
                    key = (index, check.get("code"))
                    if key in approved_local:
                        continue
                    self.safety_needed.emit([check])
                    self._safety_event.wait()
                    if self._stop:
                        outcome = {"aborted": True}
                        self.turn_done.emit(outcome)
                        return
                    if not self._safety_approved:
                        outcome = {"denied": True, "checks": [check]}
                        self.turn_done.emit(outcome)
                        return
                    approved_local.add(key)

                try:
                    started = time.monotonic()
                    self._handler.execute_action(action)
                    elapsed = time.monotonic() - started
                    limit = self._handler._action_timeout_seconds()
                    if limit and elapsed > limit:
                        raise TimeoutError(
                            f"action exceeded the {limit:g}s timeout")
                    executed.append(describe_action(action))
                    executed_raw.append(action)
                except CuaActionCancelledError:
                    outcome = {"aborted": True}
                    self.turn_done.emit(outcome)
                    return
                except CuaBudgetExceededError as e:
                    outcome = {
                        "budget_exceeded": f"**Stopped:** {e}",
                        "executed": executed,
                    }
                    self.turn_done.emit(outcome)
                    return
                except InputBlockedError as e:
                    logging.error(f"CUA input blocked: {e}")
                    self._comm_log("CONSUMED", {
                        "turn": turn_no,
                        "status": "input_blocked",
                        "error": str(e),
                    })
                    outcome = {"input_blocked": str(e)}
                    self.turn_done.emit(outcome)
                    return
                except Exception as e:
                    logging.warning(f"CUA action failed: {describe_action(action)} -> {e}")
                    try:
                        err_b64, _, _ = self._handler.capture_screenshot_b64()
                    except Exception:
                        err_b64 = ""
                    err_outs = list(function_outs)
                    err_ids = _turn_call_ids(turn)
                    for i, cid in enumerate(err_ids):
                        item = {
                            "kind": "computer",
                            "call_id": cid,
                            "result_status": "error",
                        }
                        if i == index or len(err_ids) == 1:
                            item["result_text"] = f"Action failed: {e}"
                        elif i < index:
                            item["result_text"] = (
                                "Executed before a later action failed.")
                        else:
                            item["result_text"] = (
                                "Not executed because a previous action failed.")
                        # Screenshot rides on the last item only (token cost).
                        if i == len(err_ids) - 1 and err_b64:
                            item["screenshot_b64"] = err_b64
                        err_outs.append(item)
                    outcome = {
                        "action_error": f"**Error:** {describe_action(action)} failed: {e}",
                        "executed": executed,
                        "launched": launched,
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
                        "launched": launched_log,
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
            ok_ids = _turn_call_ids(turn)
            for i, cid in enumerate(ok_ids):
                comp_out = {"kind": "computer", "call_id": cid,
                            "result_status": "ok"}
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
                tag, turn_no, executed_raw, bool(checks), shot_file,
                asked=asked_log)
            self._comm_log("CONSUMED", {
                "turn": turn_no,
                "executed": executed,
                "launched": launched_log,
                "asked": asked_log,
                "post_screenshot_file": shot_file,
            })
            outcome = {
                "executed": executed,
                "launched": launched,
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

    def _vision_cfg(self) -> dict:
        try:
            return dict(self.settings.get("vision", {}) or {})
        except Exception:
            return {}

    def _max_turns(self) -> int:
        """Turn cap, read live from Vision settings (default 20)."""
        try:
            value = int(self._vision_cfg().get(
                "max_turns_per_task", MAX_CUA_TURNS))
            return max(1, value)
        except Exception:
            return MAX_CUA_TURNS

    def _max_task_seconds(self) -> float:
        try:
            value = float(self._vision_cfg().get(
                "max_task_seconds", MAX_TASK_SECONDS))
            return max(1.0, value)
        except Exception:
            return float(MAX_TASK_SECONDS)

    def _max_actions_per_turn(self) -> int:
        try:
            value = int(self._vision_cfg().get(
                "max_actions_per_turn", MAX_ACTIONS_PER_TURN))
            return max(1, value)
        except Exception:
            return MAX_ACTIONS_PER_TURN

    def _action_timeout_seconds(self) -> float:
        try:
            value = float(self._vision_cfg().get(
                "action_timeout_seconds", ACTION_TIMEOUT_SECONDS))
            return max(1.0, value)
        except Exception:
            return float(ACTION_TIMEOUT_SECONDS)

    def _max_wait_seconds(self) -> float:
        try:
            value = float(self._vision_cfg().get(
                "max_wait_seconds", MAX_WAIT_SECONDS))
            return max(1.0, value)
        except Exception:
            return float(MAX_WAIT_SECONDS)

    def cua_screen_point(self, action=None):
        """Screenshot point scaled to real screen coordinates, if any."""
        point = action_point(action) if action is not None else None
        if point is None:
            return None
        scale = getattr(self, "_shot_scale", 1.0) or 1.0
        return (point[0] * scale, point[1] * scale)

    def cua_context(self, action=None) -> dict:
        """Window context under the action point (or the foreground)."""
        try:
            point = self.cua_screen_point(action)
            if point is not None:
                return active_window(*point)
            return active_window()
        except Exception as e:
            logging.warning(f"window context probe failed: {e}")
            return {"title": "", "process": "", "pid": 0, "own": False}

    def cua_allowed_apps(self) -> list:
        try:
            apps = self.settings.get("allowed_apps", [])
            return list(apps or [])
        except Exception:
            return []

    def cua_launch_names(self) -> list[str]:
        """Exact display names of the user-selected launchable apps."""
        names: list[str] = []
        for app in self.cua_allowed_apps():
            if not isinstance(app, dict):
                continue
            name = " ".join(str(app.get("name") or "").split())
            if name and name not in names:
                names.append(name)
        return names

    def launch_cua_app(self, app_name: str) -> str:
        """Launches one user-selected app; returns a model-readable result."""
        name = " ".join(str(app_name or "").split())
        apps = [app for app in self.cua_allowed_apps()
                if isinstance(app, dict)
                and str(app.get("name") or "").strip()]
        if not name:
            return "Error: launch_app requires app_name."
        match = next(
            (app for app in apps
             if " ".join(str(app.get("name")).split()).lower()
             == name.lower()),
            None)
        if match is None:
            available = ", ".join(
                str(app.get("name")).strip() for app in apps) or "none"
            return (f"Error: '{name}' is not in the Applications list. "
                    f"Available applications: {available}.")
        try:
            from src.apps.launcher import CrossPlatformAppLauncher
            CrossPlatformAppLauncher().launch(match)
        except Exception as e:
            logging.warning(f"launch_app failed for '{name}': {e}")
            return f"Error: could not launch '{name}': {e}"
        display_name = " ".join(str(match.get("name") or name).split())
        logging.info(f"launch_app: launched '{display_name}'.")
        return f"Launched '{display_name}' and returned to the task."

    def cua_gate_apps(self) -> list:
        if not self._cua_restrict_allowed_apps():
            return []
        return self.cua_allowed_apps()

    def classify_cua_actions(self, actions) -> list:
        """Gate every action against the window it actually targets."""
        allowed = self.cua_gate_apps()
        shell_allowed = self._cua_shell_allowed()
        system_surfaces_allowed = self._cua_system_surfaces_allowed()
        checks: list = []
        for index, action in enumerate(actions or []):
            checks.extend(classify_action(
                action, self.cua_context(action),
                allowed_apps=allowed, shell_allowed=shell_allowed,
                system_surfaces_allowed=system_surfaces_allowed,
                screen_point=self.cua_screen_point(action),
                action_index=index))
        return checks

    def _cua_wall_clock_exceeded(self) -> bool:
        if not self._cua_started_at:
            return False
        return (time.monotonic() - self._cua_started_at) >= self._max_task_seconds()

    def _cua_provider_identity(self):
        try:
            provider = self._provider()
            return (provider.get_provider_id(), getattr(provider, "model", None))
        except Exception:
            return ("?", None)

    def _cua_restart_chain(self, reason: str) -> None:
        """Drops the native chain and seeds the next first turn with a note."""
        self._cua_response_id = None
        self._cua_outbox = []
        self._last_shot_file = None
        self._cua_seed_note = reason

    def _sleep_checked(self, seconds: float) -> None:
        """Sleep in slices so Stop/Pause/wall-clock interrupt long actions."""
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self.state in (VisionState.IDLE, VisionState.PAUSED):
                raise CuaActionCancelledError("Vision task is no longer running.")
            worker = self._worker
            if worker is not None and getattr(worker, "_stop", False):
                raise CuaActionCancelledError("Vision action cancelled.")
            if self._cua_wall_clock_exceeded():
                raise CuaBudgetExceededError(
                    f"vision task time budget ({self._max_task_seconds():g}s) exhausted")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

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
        self._steps_msg = None
        self._steps_shown = 0
        self._steps_log = []
        self._cua_generation = 0
        self._cua_identity = None
        self._cua_seed_note = None
        self._cua_started_at = 0.0
        self._worker = None
        self._compact_owned = False
        self._visible_box = {}
        self._visible_event = threading.Event()
        self._pause_hist_mark = 0
        self._paused_turn_inflight = False
        self._input_block_seen = False
        self._capture_block_seen = False

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
                    self.ui.add_user_message(draft)
                    self.command_history.append(
                        f"**User note (paused):** {draft}")
                    try:
                        self.ui.user_input.clear()
                    except Exception:
                        pass
                self._paused_turn_inflight = False
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
            if self._paused_turn_inflight:
                # Resume must not execute a batch approved before the pause:
                # its screenshot is stale. Drop the native chain, re-observe.
                self._paused_turn_inflight = False
                self._set_state(VisionState.EXECUTING)
                self.controller._reenter_agent_mode(self)
                self.ui.add_message(
                    "<i>Execution resumed — discarded the unexecuted batch and "
                    "will re-check the screen.</i>")
                self.ui.set_paused(False)
                self._cua_restart_chain(
                    "User paused while a turn was in flight. The previous "
                    "unexecuted actions were discarded. Re-observe the screen "
                    "and continue the task.")
                logging.info("Vision CUA resume discarded the in-flight batch.")
                self._continue_loop()
                return
        super().pause_or_resume()
        try:
            if self.state == VisionState.PAUSED:
                # Pausing must hand the keyboard back: the thinking UI
                # locks the input in Vision mode, and a paused user wants
                # to type guidance. The next turn re-shows thinking.
                self._pause_hist_mark = len(self.command_history)
                self._stop_thinking_timer()
                worker = self._worker
                if worker is not None:
                    self._paused_turn_inflight = True
                    self._cua_generation += 1
                    self._worker = None
                    try:
                        worker.stop()
                    except Exception:
                        pass
                    try:
                        self._visible_event.set()
                    except Exception:
                        pass
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
            if self.state in (VisionState.IDLE, VisionState.PAUSED):
                raise CuaActionCancelledError("Vision action cancelled.")
            return self._ensure_visible_do(x, y)
        self._visible_box = {}
        event = threading.Event()
        self._visible_event = event
        worker.ensure_visible.emit(x, y)
        event.wait(timeout=5.0)
        if (self._worker is not worker
                or self.state in (VisionState.IDLE, VisionState.PAUSED)
                or getattr(worker, "_stop", False)):
            raise CuaActionCancelledError(
                "Vision action cancelled while showing the window.")
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
        if not self._guard_capture():
            return
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
        self._steps_msg = None
        self._steps_shown = 0
        self._steps_log = []
        self._cua_tag = time.strftime("%H%M%S")
        self._last_shot_file = None
        self._cua_generation += 1
        self._cua_started_at = time.monotonic()
        self._cua_identity = self._cua_provider_identity()
        self._cua_seed_note = None
        self._paused_turn_inflight = False
        self._cua_system_cache = None
        self._prune_debug_shots()
        try:
            import mss
            with mss.MSS() as sct:
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

    def _cua_system_surfaces_allowed(self) -> bool:
        try:
            cfg = self.settings.get("vision", {}) or {}
            return bool(cfg.get("allow_system_surfaces", False))
        except Exception:
            return False

    def _cua_restrict_allowed_apps(self) -> bool:
        try:
            cfg = self.settings.get("vision", {}) or {}
            return bool(cfg.get("restrict_to_allowed_apps", False))
        except Exception:
            return False

    def _task_with_handoff(self, task: str) -> str:
        """Seed a fresh CUA chain with project rules, session state and handoff."""
        task = task or ""
        blocks = []
        baseline_prompt = getattr(self.controller, "project_baseline_prompt", None)
        if callable(baseline_prompt):
            try:
                block = baseline_prompt()
            except Exception as e:
                logging.warning(f"Could not read Vision project rules: {e}")
                block = ""
            if block and "Project instructions (AGENTS.md)" not in task:
                blocks.append(block)
        session_prompt = getattr(self.controller, "session_context_prompt", None)
        if callable(session_prompt):
            try:
                block = session_prompt()
            except Exception as e:
                logging.warning(f"Could not read Vision session context: {e}")
                block = ""
            if block and "Current session context" not in task:
                blocks.append(block)
        prompt = getattr(self.controller, "handoff_prompt", None)
        if callable(prompt):
            try:
                block = prompt(include_same_session=True)
            except Exception as e:
                logging.warning(f"Could not read Vision handoff context: {e}")
                block = ""
            if block and "Handoff context from earlier work" not in task:
                blocks.append(block)
        compact = getattr(self.controller, "compaction_block", None)
        if callable(compact):
            try:
                block = compact("vision")
            except Exception as e:
                logging.warning(f"Could not read Vision compacted context: {e}")
                block = ""
            if block and "Compacted session context" not in task:
                blocks.append(block)
        consume = getattr(self.controller, "consume_handoff_seed", None)
        if callable(consume):
            consume()
        if not blocks:
            return task
        return "\n\n".join([task, *blocks]).strip()

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
            if text:
                self._cua_system_cache = text
        base_text = self._cua_system_cache or ""
        if not base_text:
            return ""
        apps = self.cua_launch_names()
        app_block = ""
        if apps:
            app_block = (
                "\n- Launching applications: when the task needs an app "
                "that is not on screen, call the \"launch_app\" tool with "
                "an exact name from this list instead of searching the "
                "Start menu. If the app you need is not listed, continue "
                "through the desktop or ask the user.\n"
                "Available applications:\n"
                + "\n".join(f"  - {name}" for name in apps))
        surfaces = ""
        if self._cua_system_surfaces_allowed():
            if self._cua_restrict_allowed_apps():
                surfaces = (
                    "\n- Windows Search/Start menu may be used to launch "
                    "applications; the launched app still needs to be in "
                    "Vision allowed_apps."
                )
            else:
                surfaces = (
                    "\n- Windows Search/Start menu may be used to launch "
                    "applications freely. Destructive actions and credential "
                    "entry still require approval."
                )
        if self._cua_shell_allowed():
            return base_text + app_block + surfaces + (
                "\n- Ground-truth verification: when a task needs facts the "
                "screenshots can't prove (folder sizes, file counts, disk "
                "state), you may open a terminal yourself (Win+R, type "
                "powershell, Enter), type the checking command, and read "
                "its output from the next screenshot. Prefer read-only "
                "commands. Anything modifying or deleting falls under the "
                "destruction rule above and requires user approval first."
            )
        return base_text + app_block + surfaces + (
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

        if self._cua_wall_clock_exceeded():
            self.ui.add_message(
                f"<i>Stopped: Vision time budget "
                f"({self._max_task_seconds():g}s) exhausted. Tell me how to "
                f"proceed.</i>")
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return

        identity = self._cua_provider_identity()
        if (self._cua_response_id is not None
                and self._cua_identity is not None
                and identity != self._cua_identity):
            self._cua_restart_chain(
                "Model/provider changed mid-task; the previous native chain "
                "cannot be reused. Re-observe the screen and continue.")
            logging.info("Vision CUA chain reset after model/provider change.")
        self._cua_identity = identity

        self._start_thinking_timer("Thinking (computer use)…")
        debug_flags = self._debug_flags()
        if self._cua_response_id is None:
            # First turn, or an explicit restart after a failure/steer.
            task = self.original_user_request or ""
            if self._cua_seed_note:
                task = (f"{task}\n\nPrevious attempt: "
                        f"{self._cua_seed_note}").strip()
                self._cua_seed_note = None
            task = self._task_with_handoff(task)
            instructions = self._cua_system_prompt()
            if not (instructions or "").strip():
                message = (
                    "**Vision refused to start:** the CUA safety prompt is "
                    "missing or empty (prompts/vision/cua_system.txt). Restore "
                    "it and retry; running without it would fail open.")
                self.ui.add_text(message)
                logging.error("CUA system prompt missing/empty; refusing to run.")
                self._exit_compact()
                self._set_state(VisionState.PAUSED_ON_ERROR)
                self._stop_thinking_timer()
                return
            request = {
                "task": task,
                "instructions": instructions,
                "launch_apps": self.cua_launch_names(),
            }
        else:
            request = {
                "previous_response_id": self._cua_response_id,
                "outputs": list(getattr(self, "_cua_outbox", None) or []),
                "prev_shot_file": getattr(self, "_last_shot_file", None),
                "launch_apps": self.cua_launch_names(),
            }
        if not request.get("task") and not request.get("outputs"):
            if self._cua_response_id is not None and self.original_user_request:
                self._cua_restart_chain(
                    "The previous turn produced no outputs to answer. "
                    "Re-observe the screen and continue.")
                self._continue_loop(
                    "**Error:** the previous turn had no callable outputs; "
                    "restarted the chain.")
                return
            self.ui.add_text(
                "**Error:** nothing to send (no task, no outputs).")
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return
        self._cua_turns += 1
        request.update({
            "tag": getattr(self, "_cua_tag", "cua"),
            "turn_no": self._cua_turns,
            "debug_flags": debug_flags,
        })
        self._enter_compact()
        worker = CuaTurnWorker(self, self._provider(), request,
                               generation=self._cua_generation)
        self._worker = worker
        worker.turn_done.connect(
            lambda outcome, w=worker: self._on_turn_done(outcome, w))
        worker.safety_needed.connect(
            lambda checks, w=worker: self._on_safety_needed(checks, w))
        worker.ask_needed.connect(
            lambda asks, w=worker: self._on_ask_needed(asks, w))
        worker.ensure_visible.connect(self._on_ensure_visible)
        worker.progress.connect(self._on_cua_progress)
        worker.usage_ready.connect(self._on_cua_usage)
        worker.start()

    def _on_cua_usage(self, usage: dict):
        if isinstance(usage, dict) and usage:
            self.controller.record_provider_usage(usage)

    def _on_cua_progress(self, msg: str):
        """Worker progress: live status goes to the status line, not the log."""
        text = (msg or "").strip()
        if not text:
            return
        if text.startswith("Paused"):
            # The paused banner already explains the state; keep the worker's
            # wait-site note as a normal transcript line.
            self.ui.add_message(f"<i>{html.escape(text)}</i>")
            return
        self.ui.show_thinking_ui(
            True, text.rstrip(".…").strip() or "Thinking")

    def _update_steps_card(self, parts: list):
        """One expandable card: the title shows the latest turn, the body every
        action the model took. Expanding survives later updates."""
        self._steps_shown += 1
        detail = ", ".join(parts) if parts else "screenshot"
        self._steps_log.append(detail)
        label = "step" if self._steps_shown == 1 else "steps"
        summary = detail if len(detail) <= 90 else detail[:90].rstrip() + "…"
        title = f"👁 Computer use · {self._steps_shown} {label} · {summary}"
        color = self.controller.theme_manager.get_style("COLLAPSIBLE_META")
        rows = "".join(
            f'<p style="margin: 0 0 2px 0; color: {color};">'
            f'{index}. {html.escape(text)}</p>'
            for index, text in enumerate(self._steps_log, 1))
        if self._steps_msg is not None and self.ui.update_collapsible(
                self._steps_msg, title, rows):
            return
        self._steps_msg = self.ui.add_message({
            'type': 'collapsible',
            'title': title,
            'content': rows,
            'content_is_html': True,
            'state': 'collapsed',
            'title_color': color,
        }, keep_stream=True)

    # -- turn outcome (GUI thread) --------------------------------------

    def _on_turn_done(self, outcome: dict, worker=None):
        if worker is not None:
            if worker is not self._worker:
                return  # superseded by Stop/Pause/steer
            if getattr(worker, "_generation", self._cua_generation) != \
                    self._cua_generation:
                return
        self._worker = None
        if self.state == VisionState.IDLE:
            return  # stopped meanwhile; drop the late turn
        if outcome.get("aborted"):
            return
        if outcome.get("policy_blocked"):
            self._stop_thinking_timer()
            self.ui.add_text(outcome["policy_blocked"])
            self._cua_restart_chain(
                f"Previous attempt was blocked by the safety gate: "
                f"{outcome['policy_blocked']}")
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            return
        if outcome.get("budget_exceeded"):
            self._stop_thinking_timer()
            self.ui.add_text(outcome["budget_exceeded"])
            self._cua_restart_chain(
                f"Previous attempt stopped by a budget: "
                f"{outcome['budget_exceeded']}")
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            return
        if outcome.get("denied"):
            codes = ", ".join(
                str(c.get("code", "?")) for c in outcome.get("checks", []))
            self.ui.add_text(
                f"Paused: a safety check ({codes}) needs your call and was "
                f"not approved. Tell me how to proceed.",
            )
            self._cua_restart_chain(
                f"The user declined a safety check ({codes}); the action was "
                f"not executed.")
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            self._stop_thinking_timer()
            return
        if outcome.get("fatal"):
            self._stop_thinking_timer()
            self._cua_restart_chain(
                f"Previous attempt paused: {outcome['fatal']} "
                f"Re-observe the screen before continuing.")
            self._continue_loop(outcome["fatal"])
            return
        if outcome.get("ask_cancelled"):
            self._paused_turn_inflight = True
            self.ui.add_message(
                "<i>Question dismissed — paused. Type guidance to steer, "
                "or Resume to continue (the unanswered turn will be "
                "discarded).</i>")
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
            self.ui.add_text(text)
            self._consecutive_errors = 0
            self._assessment_sent = False
            logging.info("task-done: restoring full mode")
            self._exit_compact()
            logging.info("task-done: full mode restored, going IDLE")
            self._set_state(VisionState.IDLE)
            return
        if outcome.get("input_blocked"):
            # Hard OS block (Windows UIPI / macOS TCC): not fixable mid-task.
            # Stop now, name the culprit and the fix; no further model turns.
            self._stop_thinking_timer()
            self.ui.add_text(outcome["input_blocked"])
            self._cua_restart_chain(
                "Previous attempt was stopped by an OS input block.")
            self._exit_compact()
            self._set_state(VisionState.PAUSED_ON_ERROR)
            return
        if outcome.get("action_error"):
            self._stop_thinking_timer()
            if outcome.get("response_id"):
                self._cua_response_id = outcome.get("response_id")
                self._cua_outbox = outcome.get("next_outputs") or []
                self._last_shot_file = None
            else:
                self._cua_restart_chain(
                    f"Previous action failed: {outcome['action_error']}")
            self._continue_loop(outcome["action_error"])
            return
        executed = outcome.get("executed") or []
        launched = outcome.get("launched") or []
        asked = outcome.get("asked") or []
        self._cua_response_id = outcome.get("response_id")
        self._cua_outbox = outcome.get("next_outputs") or []
        self._last_shot_file = outcome.get("shot_file")
        if not self._cua_response_id or not self._cua_outbox:
            self._cua_restart_chain(
                "The previous turn produced no callable outputs (or no "
                "response id). Re-observe the screen and continue.")
        parts = list(launched) + list(executed)
        parts += [f"asked: {a.get('question', '')[:80]}"
                  for a in asked]
        line = f"turn {self._cua_turns}: " + (
            ", ".join(parts) if parts else "screenshot")
        self._update_steps_card(parts)
        self._continue_loop(line)

    def _current_worker(self, worker):
        if worker is not None:
            if worker is not self._worker:
                return None
            if getattr(worker, "_generation", self._cua_generation) != \
                    self._cua_generation:
                return None
            return worker
        return self._worker

    def _on_safety_needed(self, checks: list, worker=None):
        worker = self._current_worker(worker)
        if worker is None or self.state in (VisionState.IDLE,):
            return
        try:
            approved = bool(self.controller.ask_safety_approval(checks))
        except Exception as e:
            logging.error(f"Safety approval dialog failed: {e}")
            approved = False
        if self._current_worker(worker) is worker:
            worker.resolve_safety(approved)

    def _on_ask_needed(self, asks: list, worker=None):
        """One input dialog per question (GUI thread). Cancel pauses."""
        worker = self._current_worker(worker)
        if worker is None or self.state in (VisionState.IDLE,):
            return
        answers: list | None = []
        try:
            for item in asks or []:
                question = item.get("question", "")
                self.ui.add_formatted_message(question)
                answer = self.controller.ask_user_text(
                    question, sensitive=is_sensitive_text(question))
                if answer is None:
                    answers = None
                    break
                answers.append({"call_id": item.get("call_id"),
                                "answer": answer})
        except Exception as e:
            logging.error(f"ask dialog failed: {e}")
            answers = None
        if self._current_worker(worker) is worker:
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
            with mss.MSS() as sct:
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

    def _prune_debug_shots(self) -> None:
        """Deletes CUA debug screenshots older than the retention window."""
        try:
            days = int(self._vision_cfg().get(
                "debug_screenshot_retention_days", 7))
        except Exception:
            days = 7
        if days <= 0:
            return
        import os
        try:
            base = self.settings.get("APP_SCREENSHOTS_DIR", "screenshots")
            cutoff = time.time() - days * 86400
            for name in os.listdir(base):
                if not str(name).startswith("cua_"):
                    continue
                folder = os.path.join(base, name)
                if not os.path.isdir(folder):
                    continue
                for root, _dirs, files in os.walk(folder):
                    for fname in files:
                        path = os.path.join(root, fname)
                        try:
                            if os.path.getmtime(path) < cutoff:
                                os.remove(path)
                        except Exception:
                            pass
                try:
                    if not os.listdir(folder):
                        os.rmdir(folder)
                except Exception:
                    pass
        except Exception as e:
            logging.warning(f"CUA screenshot retention failed: {e}")

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
            "raw_actions": [redact_action(a) for a in (actions or [])],
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
        # Keyboard-only actions have no point to probe; while the mouse
        # actions check both the foreground and the window under the target.
        if kind in ("keypress", "type", "hold_key",
                    "left_mouse_down", "left_mouse_up"):
            note = self._guard_input()
            if note:
                action["_result"] = {"text": note}
                return f"{describe_action(action)} (input blocked)"
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
                duration = min(float(action.get("duration", 1.0)),
                               self._max_wait_seconds())
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
                self._sleep_checked(duration)
            finally:
                for key in reversed(pressed):
                    try:
                        kbd.release(key)
                    except Exception:
                        pass
            return f"hold_key({','.join(norm)}, {duration}s)"

        if kind in ("click", "double_click", "triple_click", "move"):
            x, y = scale_point(float(action["x"]), float(action["y"]), scale)
            note = self._guard_input(x, y)
            if note:
                action["_result"] = {"text": note}
                return f"{describe_action(action)} (input blocked)"
            if self.ensure_point_visible(x, y):
                time.sleep(0.6)  # compositor settle after relocate
            self._audit_pointer(kind, x, y, scale=scale)
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
                self._audit_pointer(kind, x, y, phase="post", scale=scale)
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
            note = self._guard_input(pts[0][0], pts[0][1])
            if note:
                action["_result"] = {"text": note}
                return f"{describe_action(action)} (input blocked)"
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
            note = self._guard_input(x, y)
            if note:
                action["_result"] = {"text": note}
                return f"{describe_action(action)} (input blocked)"
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
            try:
                seconds = float(action.get("seconds",
                                           action.get("duration", 1.0)))
            except Exception:
                seconds = 1.0
            seconds = min(max(0.0, seconds), self._max_wait_seconds())
            self._sleep_checked(seconds)
            return f"wait({seconds:g}s)"

        raise ValueError(f"Unsupported computer action: {kind!r}")

    def _guard_input(self, x: float | None = None, y: float | None = None) -> str | None:
        """Probe for an OS block on synthetic input (Windows UIPI, missing
        macOS Accessibility permission, Wayland).

        Hard blocks (UIPI / TCC) cannot be lifted mid-task, so they raise
        InputBlockedError: the task stops at once and the user is told to
        restart Logos as administrator — no pointless model retries/tokens.
        Soft blocks (Wayland with XWayland) return a note to hand to the
        model, which then decides what to do.
        """
        try:
            from src.computer.input_access import input_block
            block = input_block(x, y)
        except Exception as e:
            logging.warning(f"input-access probe failed: {e}")
            return None
        if block is None:
            return None
        if block.hard:
            logging.error(
                f"input injection blocked [{block.platform}]: "
                f"{block.detail or block.reason}")
            raise InputBlockedError(block.chat_message())
        if not self._input_block_seen:
            self._input_block_seen = True
            logging.warning(
                f"input injection partially blocked [{block.platform}]: "
                f"{block.detail or block.reason}")
            try:
                self.ui.add_text(block.chat_message())
            except Exception as e:
                logging.warning(f"could not surface input block: {e}")
        return block.chat_message()

    def _guard_capture(self) -> bool:
        """Refuse to start the task when screen capture is silently blocked.

        macOS without Screen Recording, or Wayland, hand back black frames —
        the model would loop over nothing. Tell the user once and skip the
        run (hard) instead of a catatonic task.
        """
        try:
            from src.computer.capture_access import capture_block
            block = capture_block()
        except Exception as e:
            logging.warning(f"capture-access probe failed: {e}")
            return True
        if block is None:
            return True
        if not self._capture_block_seen:
            self._capture_block_seen = True
            logging.error(
                f"screen capture blocked [{block.platform}]: "
                f"{block.detail or block.reason}")
            try:
                self.ui.add_text(block.chat_message())
            except Exception as e:
                logging.warning(f"could not surface capture block: {e}")
        return not block.hard

    def _audit_pointer(self, kind: str, x: float, y: float,
                       phase: str = "pre", scale: float = 1.0) -> None:
        """Forensic one-liner per pointing action: where we asked the cursor
        to go vs where it actually is, whose window owns the point, and the
        focused window. Tells apart dead input (cursor never moves) from
        eaten clicks (cursor arrives but our always-on-top window takes it).
        """
        try:
            import sys as _sys
            if _sys.platform != "win32":
                return
            import ctypes
            import os as _os
            from ctypes.wintypes import POINT
            user32 = ctypes.windll.user32
            cur = POINT()
            user32.GetCursorPos(ctypes.byref(cur))
            pt = POINT(int(x), int(y))
            hwnd_at = user32.WindowFromPoint(pt)

            def _owner(hwnd):
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                return pid.value

            fg = user32.GetForegroundWindow()
            n = user32.GetWindowTextLengthW(fg)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(fg, buf, n + 1)
            logging.info(
                f"audit-{phase} {kind}@({int(x)},{int(y)}) scale={scale:.3f}: "
                f"cursor=({cur.x},{cur.y}) "
                f"under_ours={_owner(hwnd_at) == _os.getpid()} "
                f"focus={buf.value!r} (fg_pid={_owner(fg)})")
        except Exception as e:
            logging.warning(f"pointer audit failed: {e}")

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
            try:
                # Never leave the agent's text (possibly a secret) on the
                # clipboard: restore it, or clear it when it was unreadable.
                pyperclip.copy(original if original is not None else "")
            except Exception:
                pass

    # -- lifecycle ----------------------------------------------------------

    def build_prompts(self):
        return "", self.original_user_request or ""

    def _cleanup_task(self):
        worker, self._worker = self._worker, None
        self._cua_generation += 1
        self._cua_started_at = 0.0
        self._paused_turn_inflight = False
        self._cua_seed_note = None
        self._visible_event.set()
        if worker is not None:
            try:
                worker.stop()
                if not worker.wait(3000):
                    logging.warning("Vision CUA worker did not stop in 3s; "
                                    "abandoning it to the generation guard.")
            except Exception as e:
                logging.warning(f"Vision CUA worker stop failed: {e}")
        self._exit_compact()
        self._cua_response_id = None
        self._cua_outbox = []

    def cancel_job_wait(self):
        pass

    def handle_fatal_error(self, error_message):
        """Shows an unrecoverable LLM error and returns to idle."""
        logging.error(f"VisionHandler displaying fatal error to user: {error_message}")
        try:
            self.ui.add_message(error_message, force_scroll=True)
        except Exception:
            pass
        self._stop_thinking_timer()
        self._exit_compact()
        self._cua_generation += 1
        self._paused_turn_inflight = False
        self._cua_restart_chain("The previous task ended with a fatal error.")
        self._set_state(VisionState.IDLE)

    def pause_for_feedback(self):
        if self.state == VisionState.EXECUTING:
            self.pause_or_resume()

    def _on_analysis_complete(self, *args, **kwargs):
        return None

    def _on_analysis_error(self, *args, **kwargs):
        return None
