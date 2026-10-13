import logging
import signal
import subprocess
import sys
import threading
import time

from src.computer.computer import tool_meta
from src.computer.tools.constants import SHELL_COMMAND_EXAMPLE


def _terminate_tree(proc: subprocess.Popen):
    """Stops the process and its children (best effort, then kill).

    Killing only the direct child is not enough on Windows: grandchildren
    (ping, robocopy) survive, keep the output pipes open, and the drain
    threads hang. taskkill /T takes the whole tree down.
    """
    try:
        if sys.platform == "win32":
            # Process group (set at spawn): Ctrl-Break reaches children too.
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
                if proc.poll() is not None:
                    return
            except Exception:
                pass
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        else:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
                if proc.poll() is not None:
                    return
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


def _last_useful_line(lines: list) -> str:
    for raw in reversed(lines):
        for part in str(raw).replace("\r", "\n").split("\n"):
            text = part.strip()
            if text:
                return text
    return ""


@tool_meta(
    tool={
        "name": "execute_shell",
        "description": ("Executes a command in the system's default shell (cmd.exe on Windows, sh/bash elsewhere). "
            "Write plain shell syntax for THAT shell: on Windows use cmd commands (mkdir, dir, echo) or "
            "'powershell -Command \"...\"' for cmdlets. Never hardcode user profile paths (C:\\Users\\...): use portable "
            "tokens {DESKTOP}, {HOME}, {WORKDIR}, {DOCUMENTS}, {DOWNLOADS}, expanded by the executor. "
            "Long-running commands are allowed and stream live progress. Returns stdout, stderr and exit code."),
        "arguments": {
            "command": {"type": "STRING", "description": f"The command line to execute (e.g., {SHELL_COMMAND_EXAMPLE}).", "required": True}
        },
        "returns": {
            "type": "OBJECT",
            "description": "A dictionary containing the command's output.",
            "properties": {
                "stdout": {"type": "STRING", "description": "The standard output of the command."},
                "stderr": {"type": "STRING", "description": "The standard error output. Empty if no errors."},
                "exit_code": {"type": "INTEGER", "description": "The exit code of the process. 0 typically means success."}
            }
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def execute_shell(self, command: str) -> dict:
    """
    Executes a shell command, streaming stdout live via progress_callback
    and honoring cancel_event / the configured timeout. Always reaps the
    process (no orphans on timeout or user Stop).
    """
    if not command:
        raise ValueError("Action 'execute_shell' failed: Missing required 'command' argument.")

    timeout = 60
    try:
        sm = getattr(self, "settings_manager", None)
        if sm is not None:
            timeout = int((sm.get("terminal", {}) or {}).get("shell_timeout_seconds", 60) or 60)
    except Exception:
        timeout = 60
    timeout = max(1, timeout)
    cancel_event = getattr(self, "cancel_event", None)
    # Captured once: a newer run may replace the shared callback, but this
    # run keeps reporting under its own ticket (stale ones are dropped).
    progress_cb = getattr(self, "progress_callback", None)

    # On Windows, a `powershell -Command "..."` wrapper routed through
    # cmd.exe mangles nested quotes (the inner command can degrade into
    # a quoted string that PowerShell merely echoes, exiting 0 while
    # doing nothing). Run such commands via powershell.exe directly.
    lowered = command.strip()
    ps_prefixes = ("powershell -command", "powershell.exe -command", "pwsh -command", "pwsh.exe -command")
    inner = None
    for prefix in ps_prefixes:
        if lowered.lower().startswith(prefix):
            inner = command.strip()[len(prefix):].strip()
            # Strip one layer of surrounding quotes if present.
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in ('"', "'"):
                inner = inner[1:-1]
            break

    spawn_kwargs: dict = {"cwd": self.working_directory}
    if sys.platform == "win32":
        # Own process group so Ctrl-Break on stop reaches grandchildren too.
        spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        spawn_kwargs["start_new_session"] = True

    try:
        logging.info(f"Executing shell command: {command} (timeout {timeout}s)")

        if inner is not None:
            # Make cmdlet errors terminating: by default powershell.exe
            # returns exit code 0 even when a cmdlet fails (e.g. removing
            # a non-existent path), which would fake a success. With
            # $ErrorActionPreference='Stop' failures surface as exit code 1
            # and the textual contract check catches them.
            inner = "$ErrorActionPreference = 'Stop'; " + inner
            argv = ['powershell', '-NoProfile', '-NonInteractive', '-Command', inner]
            encoding = 'utf-8'
        elif sys.platform == "win32":
            # Using shell=True on Windows is common, but splitting the command
            # with cmd /c is a bit safer.
            # Specify 'oem' encoding for Windows console output
            argv = ['cmd', '/c', command]
            encoding = 'oem'
        else:
            argv = command
            if isinstance(argv, str):
                spawn_kwargs["shell"] = True
            encoding = 'utf-8'

        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=encoding,
                errors='ignore',
                bufsize=1,
                **spawn_kwargs
            )
        except FileNotFoundError:
            logging.error(f"Command '{command.split()[0]}' not found.")
            return {
                "stdout": "",
                "stderr": f"Error: Command not found: {command.split()[0]}",
                "exit_code": 127  # Standard exit code for command not found
            }

        out_lines: list = []
        err_lines: list = []

        def _drain(stream, sink: list):
            try:
                for line in iter(stream.readline, ""):
                    sink.append(line)
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, out_lines), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err_lines), daemon=True),
        ]
        for t in readers:
            t.start()

        start = time.monotonic()
        timed_out = False
        cancelled = False
        while proc.poll() is None:
            elapsed = time.monotonic() - start
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if elapsed >= timeout:
                timed_out = True
                break
            if progress_cb is not None:
                try:
                    progress_cb(elapsed, _last_useful_line(out_lines))
                except Exception:
                    pass
            time.sleep(0.2)

        if timed_out or cancelled:
            _terminate_tree(proc)
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for t in readers:
            t.join(timeout=3)

        stdout = "".join(out_lines)
        stderr = "".join(err_lines)
        exit_code = proc.returncode if proc.returncode is not None else -1
        logging.info(f"Command finished with exit code {exit_code}.")

        if cancelled:
            note = "Stopped by user."
            return {
                "stdout": stdout,
                "stderr": (stderr + "\n" + note).strip(),
                "exit_code": -2
            }
        if timed_out:
            return {
                "stdout": stdout,
                "stderr": (stderr + f"\nError: Command timed out after {timeout} seconds. "
                                    f"Partial output kept above.").strip(),
                "exit_code": -1
            }
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code
        }

    except Exception as e:
        logging.error(f"An unexpected error occurred while executing command '{command}': {e}", exc_info=True)
        return {
            "stdout": "",
            "stderr": f"An unexpected error occurred: {e}",
            "exit_code": -1
        }
