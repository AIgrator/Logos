"""Unified action executor shared by all agent modes.

The Executor is deliberately "dumb": it receives an already validated
ToolCall ({name, arguments}), performs it through the bound Computer
instance, and returns a uniform ActionResult envelope. All *decisions*
(what to do, whether the outcome is satisfactory) stay in the per-mode
handlers (Vision/Terminal/Chat) and in the prompts.

Kinds of results:
- kind="ui":   judged against a visual contract (screenshot comparison).
- kind="shell": judged against a textual contract (stdout/stderr/exit code).
- kind="info":  no verification needed (finish, communicate_to_user, ...).
"""
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Tools whose result is judged textually, not visually.
SHELL_TOOLS = frozenset({"execute_shell"})

# Tools that need no success verification at all.
INFO_TOOLS = frozenset({
    "finish", "communicate_to_user", "display_message",
    "show_warning", "get_clipboard_content",
})

# UI tools that move the pointer: subject to self-occlusion checks.
POINTER_TOOLS = frozenset({"click", "double_click", "drag_and_drop"})

# Conservative denylist for shell commands executed from agent plans.
# Matched case-insensitively against the raw command string. This is a
# safety net, not a sandbox: the primary protection is plan review
# (show_warning) by the user.
DESTRUCTIVE_SHELL_PATTERNS = (
    "rm -rf /", "rm -rf ~", "rm -rf /*", ":(){:|:&};:",
    "mkfs", "dd if=", "diskpart", "format c:",
    "del /f /s /q c:\\", "rd /s /q c:\\",
    "shutdown", "reboot", "halt", "poweroff",
    "chmod -r 777 /", "chown -r ",
)


@dataclass
class ActionResult:
    """Uniform envelope for any executed action."""
    ok: bool
    kind: str = "ui"  # "ui" | "shell" | "info"
    output_text: str = ""
    exit_code: int | None = None
    error: str | None = None
    duration_ms: float = 0.0
    extra: dict = field(default_factory=dict)

    def for_evaluation(self) -> str | None:
        """Textual payload forwarded into the evaluation prompt, if any."""
        if self.kind == "shell":
            return self.output_text or None
        if not self.ok and self.error:
            return self.error
        return None


# Portable location tokens for shell commands. The model writes portable
# commands using these tokens; the executor (which knows the machine)
# expands them. Folder layouts differ per user, so hardcoded profile
# paths like C:\Users\NAME\Desktop are forbidden by the prompt rules.
# The user speaks in human terms ("рабочий стол", "документы") and never
# provides paths — the model maps those words onto these tokens.
PATH_TOKENS = ("{DESKTOP}", "{HOME}", "{WORKDIR}", "{DOCUMENTS}", "{DOWNLOADS}")


def expand_path_tokens(command: str, working_directory: str | None) -> str:
    """Replace {DESKTOP}/{HOME}/{WORKDIR}/{DOCUMENTS}/{DOWNLOADS} with real paths."""
    import os
    if not command or "{" not in command:
        return command
    home = ""
    try:
        home = os.path.expanduser("~")
        if home == "~":
            home = ""
    except Exception:
        home = ""

    def _existing(*parts: str) -> str:
        path = os.path.join(*parts) if parts else ""
        try:
            if path and os.path.isdir(path):
                return path
        except Exception:
            pass
        return ""

    desktop = _existing(home, "Desktop") if home else ""
    if not desktop:
        desktop = r"C:\Users\Public\Desktop"
    documents = _existing(home, "Documents") if home else ""
    downloads = _existing(home, "Downloads") if home else ""
    mapping = {
        "{DESKTOP}": desktop,
        "{HOME}": home,
        "{WORKDIR}": working_directory or "",
        "{DOCUMENTS}": documents,
        "{DOWNLOADS}": downloads,
    }
    for token, value in mapping.items():
        if token in command:
            command = command.replace(token, value)
    return command


def task_kind(actions: list) -> str:
    """Classify a task by how its outcome must be verified.

    Returns "shell" only when *every* action is textual (shell/info) —
    such tasks are judged by command output and need no screenshots.
    Anything else (any UI action) is "ui" and is judged visually.
    """
    if not actions:
        return "ui"
    for action in actions:
        name = (action.get("name") if isinstance(action, dict) else None) or ""
        if name not in SHELL_TOOLS and name not in INFO_TOOLS:
            return "ui"
    return "shell"


def check_textual_contract(task: dict, result: "ActionResult") -> str | None:
    """Deterministically verify a shell task against its `expected_output`.

    Parses two check forms (both optional, combined with AND):
    - "EXIT CODE: <int>" — must equal result.exit_code;
    - "STDOUT contains '<text>'" / 'STDERR contains "<text>"' — substring
      must appear in the formatted command output.

    Returns None when everything matches (or there is nothing to check),
    otherwise a human/LLM-readable mismatch description. This runs *before*
    the LLM evaluation so silent no-ops (exit 0, wrong output) cannot pass
    as success.
    """
    import re

    expected = (task.get("expected_output") if isinstance(task, dict) else None) or ""
    if not expected or result.kind != "shell":
        return None

    problems = []
    match = re.search(r"EXIT\s*CODE:\s*(-?\d+)", expected)
    if match:
        want = int(match.group(1))
        if result.exit_code != want:
            problems.append(
                f"expected EXIT CODE {want} but got {result.exit_code}."
            )
    for stream in ("STDOUT", "STDERR"):
        for quote in ("'", '"'):
            pattern = re.compile(re.escape(stream) + r"\s+contains\s+" + quote + r"(.*?)" + quote)
            for sub in pattern.findall(expected):
                if sub and sub not in (result.output_text or ""):
                    problems.append(
                        f"expected {stream} to contain {quote}{sub}{quote}, but it was absent. "
                        f"Actual output was:\n{result.output_text or '(empty)'}"
                    )
    if problems:
        return (
            "Textual contract check FAILED for the shell task "
            f"('{task.get('task_description', '?')}'): " + " ".join(problems)
        )
    return None


class Executor:
    """Executes validated tool calls through a Computer instance."""

    def __init__(self, computer, ui=None):
        """
        Args:
            computer: bound Computer instance (mode toolset already loaded).
            ui: optional object exposing is_click_colliding(x, y) and
                relocate_compact_window() for self-occlusion avoidance.
        """
        self.computer = computer
        self.ui = ui

    # -- guards ---------------------------------------------------------

    def _check_shell_safety(self, command: str) -> None:
        lowered = (command or "").lower()
        for pattern in DESTRUCTIVE_SHELL_PATTERNS:
            if pattern in lowered:
                raise ValueError(
                    f"Action 'execute_shell' blocked: command matches destructive pattern '{pattern}'. "
                    f"If this is really intended, ask the user to run it manually."
                )

    def _check_pointer_collision(self, action: dict) -> None:
        if self.ui is None:
            return
        args = action.get("arguments", {}) or {}
        points = []
        name = action.get("name")
        if name in ("click", "double_click"):
            x1, y1, x2, y2 = (args.get(k) for k in ("x1", "y1", "x2", "y2"))
            if all(v is not None for v in (x1, y1, x2, y2)):
                points.append(((x1 + x2) / 2, (y1 + y2) / 2))
        elif name == "drag_and_drop":
            start = [args.get(k) for k in ("x1_start", "y1_start", "x2_start", "y2_start")]
            if all(v is not None for v in start):
                points.append(((start[0] + start[2]) / 2, (start[1] + start[3]) / 2))
            end = [args.get(k) for k in ("x1_end", "y1_end", "x2_end", "y2_end")]
            if all(v is not None for v in end):
                points.append(((end[0] + end[2]) / 2, (end[1] + end[3]) / 2))
        for x, y in points:
            try:
                if self.ui.is_click_colliding(int(x), int(y)):
                    self.ui.relocate_compact_window()
                    time.sleep(0.1)
                    break
            except Exception:
                logger.debug("Collision check failed, proceeding anyway.", exc_info=True)
                break

    # -- execution ------------------------------------------------------

    def execute(self, action: dict, ctx: dict | None = None) -> ActionResult:
        """Execute one validated action. Never raises for tool failures;
        transport-level misuse (unknown tool) raises ValueError with an
        LLM-readable message (structural self-correction)."""
        ctx = ctx or {}
        name = action.get("name", "")
        kwargs = dict(action.get("arguments", {}) or {})
        started = time.perf_counter()

        # Extra context for special tools (same contract VisionHandler used).
        if name == "launch_app":
            kwargs.setdefault("allowed_apps_list", ctx.get("allowed_apps", []))
            if ctx.get("app_launcher") is not None:
                kwargs.setdefault("app_launcher", ctx["app_launcher"])

        if name in POINTER_TOOLS:
            self._check_pointer_collision(action)

        if name == "execute_shell":
            if not ctx.get("shell_allowed", False):
                raise ValueError(
                    "Action 'execute_shell' failed: shell commands in Vision plans are disabled. "
                    "Enable 'allow_shell_commands' in Vision settings to use this tool."
                )
            command = expand_path_tokens(
                kwargs.get("command", ""),
                getattr(self.computer, "working_directory", None),
            )
            kwargs["command"] = command
            self._check_shell_safety(command)

        tool_func = getattr(self.computer, name, None)
        if tool_func is None or not callable(tool_func):
            raise ValueError(
                f"Action '{name}' failed: unknown tool for the current mode. "
                f"Use only tools from the Available Tools list."
            )

        try:
            raw = tool_func(**kwargs)
        except (ValueError, TypeError, KeyError) as e:
            # Instructional errors from tools feed the correction loop.
            return ActionResult(
                ok=False, kind=self._kind_of(name),
                error=str(e), duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as e:  # noqa: BLE001 - must not break the loop
            logger.error(f"Executor: tool '{name}' raised unexpectedly: {e}", exc_info=True)
            return ActionResult(
                ok=False, kind=self._kind_of(name),
                error=f"Tool '{name}' failed with an internal error: {e}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        duration_ms = (time.perf_counter() - started) * 1000
        if name in SHELL_TOOLS and isinstance(raw, dict):
            text = self._format_shell_output(raw)
            return ActionResult(
                ok=True, kind="shell", output_text=text,
                exit_code=raw.get("exit_code"),
                duration_ms=duration_ms, extra={"raw": raw},
            )
        output = ""
        if isinstance(raw, dict):
            output = raw.get("message", "") or ""
        elif isinstance(raw, str):
            output = raw
        return ActionResult(
            ok=True, kind=self._kind_of(name), output_text=output,
            duration_ms=duration_ms, extra={"raw": raw},
        )

    @staticmethod
    def _kind_of(name: str) -> str:
        if name in SHELL_TOOLS:
            return "shell"
        if name in INFO_TOOLS:
            return "info"
        return "ui"

    @staticmethod
    def _format_shell_output(result: dict) -> str:
        parts = []
        stdout = (result.get("stdout") or "").strip()
        stderr = (result.get("stderr") or "").strip()
        exit_code = result.get("exit_code")
        if stdout:
            parts.append(f"STDOUT:\n{stdout[-4000:]}")
        if stderr:
            parts.append(f"STDERR:\n{stderr[-2000:]}")
        parts.append(f"EXIT CODE: {exit_code}")
        return "\n".join(parts) if parts else f"EXIT CODE: {exit_code}"
