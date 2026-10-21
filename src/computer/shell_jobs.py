"""Shared shell-execution machinery plus background job management.

Foreground (`execute_shell`) and background jobs run the same pump; jobs
add a registry so long operations (multi-GB copies, builds) survive across
agent turns: launch returns at once, `poll`/`stop` observe or abort later.
All tool calls still execute on worker threads — the registry is lock
guarded because guidance-during-run can overlap two runners.
"""
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

POLL_OUTPUT_CAP = 20000
OUTPUT_FILE_LIMIT = 64 * 1024 * 1024
DEFAULT_INACTIVITY_TIMEOUT = 300

_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC ... BEL/ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"               # CSI
    r"|\x1b[@-_]"                              # other 2-char escapes
)

_EXIT1_NORMAL = (
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr",
    "find", "diff", "fc", "test",
    "git diff", "git grep", "git --no-pager diff",
)

# Never let child processes prompt for input: the GUI has no terminal to
# answer them, so they would block until the shell timeout kills the run.
_NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "SSH_ASKPASS": "echo",
    "PIP_NO_INPUT": "1",
    "npm_config_yes": "true",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    "DEBIAN_FRONTEND": "noninteractive",
}


def parse_job_id(value) -> int:
    """Tolerant job-id parsing: 2, '2', '#2', 'job 2', 'job #2' all work.

    The model sees 'job #N' everywhere (history, hints), so it naturally
    echoes that back. Rejecting it burned turns and hid a live job once.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        for prefix in ("job #", "job#", "job ", "#"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
        if cleaned.isdigit():
            return int(cleaned)
    raise ValueError(f"Invalid job_id '{value}': use the number, e.g. 2.")


def resolve_shell_timeout(settings_manager, default: int = 60) -> int:
    timeout = default
    try:
        if settings_manager is not None:
            timeout = int((settings_manager.get("terminal", {}) or {}).get(
                "shell_timeout_seconds", default) or default)
    except Exception:
        timeout = default
    return max(1, timeout)


def resolve_output_cap(settings_manager, default: int = POLL_OUTPUT_CAP) -> int:
    try:
        if settings_manager is not None:
            cap = int((settings_manager.get("terminal", {}) or {}).get(
                "output_cap_chars", default) or default)
        else:
            cap = default
    except Exception:
        cap = default
    return max(1000, cap)


def resolve_inactivity_timeout(settings_manager,
                               default: int = DEFAULT_INACTIVITY_TIMEOUT) -> int:
    try:
        if settings_manager is not None:
            value = int((settings_manager.get("terminal", {}) or {}).get(
                "inactivity_timeout_seconds", default) or 0)
        else:
            value = default
    except Exception:
        value = default
    return max(0, value)


def build_shell_command(command: str):
    """Builds (argv, encoding, extra_popen_kwargs) for a shell command."""
    lowered = command.strip()
    ps_prefixes = ("powershell -command", "powershell.exe -command",
                   "pwsh -command", "pwsh.exe -command")
    inner = None
    for prefix in ps_prefixes:
        if lowered.lower().startswith(prefix):
            inner = command.strip()[len(prefix):].strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in ('"', "'"):
                inner = inner[1:-1]
            break

    extra: dict = {"env": {**os.environ, **_NONINTERACTIVE_ENV}}
    if sys.platform == "win32":
        # Own process group so Ctrl-Break on stop reaches grandchildren too.
        extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        extra["start_new_session"] = True

    if inner is not None:
        # Surface cmdlet failures WITHOUT killing bulk work on the first
        # warning: $ErrorActionPreference='Stop' turned any per-file warning
        # (inevitable on a live tree — open logs, loaded binaries) into an
        # instant death before anything was done. Instead count session
        # errors around the command: warnings stay visible AND fail the run
        # at the end, while native exit codes pass through untouched.
        # Pin console encoding to UTF-8 as well: powershell inherits whatever
        # codepage the parent console uses (often 65001), while the reader
        # side must know exactly one codec — otherwise Russian error text
        # arrives as mojibake.
        inner = ("$Error.Clear(); "
                 "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                 "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                 + inner +
                 "; $__code = $LASTEXITCODE; if ($null -eq $__code) { $__code = 0 }; "
                 "if (($Error.Count -gt 0) -and ($__code -eq 0)) { $__code = 1 }; "
                 # Use $host.SetShouldExit, NOT `exit`: PowerShell renders object
                 # output (tables) on an async output thread, and `exit` tears the
                 # runspace down before it flushes — so `Get-ChildItem | Select-Object`
                 # came back as a single blank line and the agent concluded a
                 # non-empty folder was empty. SetShouldExit requests the same exit
                 # code but lets the pipeline drain first.
                 "$host.SetShouldExit($__code)")
        return (['powershell', '-NoProfile', '-NonInteractive', '-Command', inner],
                'utf-8', extra)
    if sys.platform == "win32":
        return (['cmd', '/c', command], 'oem', extra)
    extra["shell"] = True
    return (command, 'utf-8', extra)


def _terminate_tree(proc: subprocess.Popen):
    """Stops the process and its children (best effort, then kill).

    Killing only the direct child is not enough on Windows: grandchildren
    (ping, robocopy) survive, keep the output pipes open, and drain threads
    hang. taskkill /T takes the whole tree down.
    """
    try:
        if sys.platform == "win32":
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


def _drain_stream(stream, sink: list, lock=None, activity=None):
    try:
        for line in iter(stream.readline, ""):
            if activity is not None:
                activity[0] = time.monotonic()
            line = _ANSI_RE.sub("", line)
            if lock is not None:
                with lock:
                    sink.append(line)
            else:
                sink.append(line)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _last_useful_line(lines: list) -> str:
    for raw in reversed(lines):
        for part in str(raw).replace("\r", "\n").split("\n"):
            text = part.strip()
            if text:
                return text
    return ""


def _cap(text: str, limit: int = POLL_OUTPUT_CAP) -> str:
    """Keeps the head and the tail: final errors/test summaries live there."""
    if limit <= 0 or len(text) <= limit:
        return text
    budget = max(0, limit - 60)
    head = budget // 2
    tail = budget - head
    omitted = len(text) - head - tail
    if omitted <= 0:
        return text
    return (text[:head]
            + f"\n… [{omitted} chars omitted] …\n"
            + text[-tail:])


def _persist_output(text: str) -> str | None:
    """Writes the full output under %TEMP%/logos-shell and returns the path."""
    try:
        directory = os.path.join(tempfile.gettempdir(), "logos-shell")
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"shell-{stamp}-{os.getpid()}-{time.time_ns() % 1_000_000}.txt"
        path = os.path.join(directory, name)
        data = text
        if len(data) > OUTPUT_FILE_LIMIT:
            data = data[:OUTPUT_FILE_LIMIT] + (
                f"\n\n[truncated at {OUTPUT_FILE_LIMIT} chars]\n")
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(data)
        return path
    except Exception as e:
        logging.warning(f"Could not persist shell output: {e}")
        return None


def _format_output(text: str, limit: int) -> tuple:
    """Returns (inline_preview, full_output_path_or_None)."""
    if not text:
        return "(no output)", None
    if limit <= 0 or len(text) <= limit:
        return text, None
    path = _persist_output(text)
    body = _cap(text, limit)
    if path:
        body += f"\n\n[Full output saved to: {path}]"
    return body, path


def _exit_code_note(command: str, exit_code: int) -> str | None:
    if exit_code != 1:
        return None
    lowered = " ".join(command.strip().lower().split())
    for prefix in _EXIT1_NORMAL:
        if lowered == prefix or lowered.startswith(prefix + " "):
            return (f"Exit code 1 from {prefix} usually means no matches / "
                    "no differences / condition false - not a tool failure.")
    return None


def _build_result(command: str, exit_code: int, full_output: str,
                  system_note: str, wall_time: float, output_limit: int,
                  timed_out: bool = False, cancelled: bool = False,
                  inactive: bool = False) -> dict:
    total_chars = len(full_output)
    total_lines = full_output.count("\n") + (
        1 if full_output and not full_output.endswith("\n") else 0)
    stdout, output_file = _format_output(full_output, output_limit)
    result = {
        "stdout": stdout,
        "stderr": (system_note or "").strip(),
        "exit_code": exit_code,
        "wall_time": round(wall_time, 2),
        "total_lines": total_lines,
        "total_chars": total_chars,
        "timed_out": bool(timed_out),
        "cancelled": bool(cancelled),
        "inactivity_timeout": bool(inactive),
    }
    note = _exit_code_note(command, exit_code)
    if note:
        result["exit_note"] = note
    if output_file:
        result["output_file"] = output_file
    return result


def _mute_diagnostic(exit_code: int, stdout: str, stderr: str,
                     elapsed: float, timeout: float) -> str:
    """Explains an abnormal end with no output (otherwise unreadable).

    A mute abnormal death (timeout text lost, external kill, silent
    crash/OOM) must still tell the user what is known: how long it ran
    against the allowed limit.
    """
    if exit_code == 0 or stdout.strip() or stderr.strip():
        return stderr
    limit = f"{timeout:.0f}s allowed" if timeout and timeout > 0 else "no timeout"
    near_timeout = bool(timeout and timeout > 0 and elapsed >= timeout * 0.9)
    if near_timeout:
        verdict = (f"\n(Process exited with code {exit_code} after {elapsed:.0f}s "
                   f"with {limit} and no output at all. It may have been killed "
                   f"at the timeout or crashed silently.)")
    else:
        verdict = (f"\n(Process exited with code {exit_code} after {elapsed:.0f}s "
                   f"with {limit} and produced no output.)")
    return (stderr + verdict).strip()


NO_RESULT_TEXT = ("No result was recorded (the worker ended without storing one). "
                  "Process state unknown — it may have been killed externally or "
                  "crashed silently.")


def pump_shell(proc: subprocess.Popen, timeout: float, cancel_event,
               progress_cb, out_sink: list, lock=None,
               inactivity_timeout: float = 0):
    """Runs the pump loop; returns (timed_out, cancelled, inactive).

    stdout carries stderr too (`stderr=STDOUT`), so a single reader keeps
    the true chronology. `inactive` means the process produced no output
    for `inactivity_timeout` seconds and was killed as likely waiting for
    input.
    """
    activity = [time.monotonic()]
    readers = [
        threading.Thread(target=_drain_stream,
                         args=(proc.stdout, out_sink, lock, activity),
                         daemon=True),
    ]
    for t in readers:
        t.start()

    def _snapshot():
        if lock is not None:
            with lock:
                return list(out_sink)
        return out_sink

    start = time.monotonic()
    timed_out = False
    cancelled = False
    inactive = False
    while proc.poll() is None:
        elapsed = time.monotonic() - start
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        # timeout <= 0 means "no shell timeout" (background jobs). They are
        # stopped explicitly via stop_shell_job instead of being killed just
        # because they are long-lived, e.g. a dev server.
        if timeout and timeout > 0 and elapsed >= timeout:
            timed_out = True
            break
        if (inactivity_timeout > 0
                and time.monotonic() - activity[0] >= inactivity_timeout):
            inactive = True
            break
        if progress_cb is not None:
            try:
                progress_cb(elapsed, _last_useful_line(_snapshot()))
            except Exception:
                pass
        time.sleep(0.2)

    if timed_out or cancelled or inactive:
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
    return timed_out, cancelled, inactive


def run_shell_command(command: str, working_directory, timeout: float,
                      cancel_event=None, progress_cb=None,
                      output_limit: int = POLL_OUTPUT_CAP,
                      inactivity_timeout: float = 0) -> dict:
    """Synchronous shell run with streaming, timeout and cancellation."""
    try:
        argv, encoding, extra = build_shell_command(command)
        logging.info(f"Executing shell command: {command} (timeout {timeout}s)")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding=encoding,
                errors='ignore',
                bufsize=1,
                cwd=working_directory,
                **extra
            )
        except FileNotFoundError:
            logging.error(f"Command '{command.split()[0]}' not found.")
            return {
                "stdout": "",
                "stderr": f"Error: Command not found: {command.split()[0]}",
                "exit_code": 127
            }

        out_lines: list = []
        run_start = time.monotonic()
        timed_out, cancelled, inactive = pump_shell(
            proc, timeout, cancel_event, progress_cb, out_lines,
            inactivity_timeout=inactivity_timeout)
        run_elapsed = time.monotonic() - run_start

        full_output = "".join(out_lines)
        exit_code = proc.returncode if proc.returncode is not None else -1
        logging.info(f"Command finished with exit code {exit_code}.")

        if cancelled:
            note = "Stopped by user."
            return _build_result(command, -2, full_output, note, run_elapsed,
                                 output_limit, cancelled=True)
        if inactive:
            note = (f"Error: no output for {inactivity_timeout:.0f} seconds — the "
                    "process was TERMINATED as likely waiting for interactive "
                    "input. Provide input via command flags/env or split the task.")
            return _build_result(command, -1, full_output, note, run_elapsed,
                                 output_limit, inactive=True)
        if timed_out:
            note = (f"Error: Command timed out after {timeout} seconds and was "
                    "TERMINATED (output above is partial). Do NOT keep retrying "
                    "it as-is — one varied retry at most; if it dies by timeout "
                    "again, stop and report to the user (or split the work into "
                    "smaller pieces).")
            return _build_result(command, -1, full_output, note, run_elapsed,
                                 output_limit, timed_out=True)
        note = _mute_diagnostic(exit_code, full_output, "", run_elapsed, timeout)
        if _exit_code_note(command, exit_code):
            # Expected non-zero (grep/findstr no match, diff differences) is
            # not a silent crash; the normal-exit note below explains it.
            note = ""
        return _build_result(command, exit_code, full_output, note,
                             run_elapsed, output_limit)
    except Exception as e:
        logging.error(f"An unexpected error occurred while executing command '{command}': {e}",
                      exc_info=True)
        return {"stdout": "", "stderr": f"An unexpected error occurred: {e}", "exit_code": -1}


class ShellJob:
    def __init__(self, job_id: int, command: str, timeout: float,
                 output_limit: int = POLL_OUTPUT_CAP,
                 inactivity_timeout: float = 0):
        self.job_id = job_id
        self.command = command
        self.timeout = timeout
        self.output_limit = output_limit
        self.inactivity_timeout = inactivity_timeout
        self.lock = threading.Lock()
        self.stdout_parts: list = []
        self.stderr_parts: list = []
        self.stdout_offset = 0
        self.stderr_offset = 0
        self.proc = None
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.result = None
        self.started = time.monotonic()


class ShellJobManager:
    """Registry of background shell jobs (launch/poll/stop/kill_all)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._counter = 0

    def _register(self, command: str, timeout: float, cancel_event=None,
                  output_limit: int = POLL_OUTPUT_CAP,
                  inactivity_timeout: float = 0) -> ShellJob:
        with self._lock:
            self._counter += 1
            job_id = self._counter
            job = ShellJob(job_id, command, timeout, output_limit,
                           inactivity_timeout)
            if cancel_event is not None:
                job.cancel_event = cancel_event
            self._jobs[job_id] = job
        return job

    def _start_job_thread(self, job: ShellJob, working_directory):
        command = job.command
        timeout = job.timeout

        def _target():
            try:
                argv, encoding, extra = build_shell_command(command)
                logging.info(f"Background shell job #{job.job_id} started: {command}")
                try:
                    proc = subprocess.Popen(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding=encoding,
                        errors='ignore',
                        bufsize=1,
                        cwd=working_directory,
                        **extra
                    )
                except FileNotFoundError:
                    job.result = {"stdout": "",
                                  "stderr": f"Error: Command not found: {command.split()[0]}",
                                  "exit_code": 127}
                    job.done.set()
                    return
                with job.lock:
                    job.proc = proc
                timed_out, cancelled, inactive = pump_shell(
                    proc, timeout, job.cancel_event, None,
                    job.stdout_parts, job.lock, job.inactivity_timeout)
                full_output = "".join(job.stdout_parts)
                exit_code = proc.returncode if proc.returncode is not None else -1
                wall_time = time.monotonic() - job.started
                if cancelled:
                    note = "Stopped by user."
                    job.result = _build_result(
                        command, -2, full_output, note, wall_time,
                        job.output_limit, cancelled=True)
                elif inactive:
                    note = (f"Error: job produced no output for "
                            f"{job.inactivity_timeout:.0f} seconds and was "
                            "TERMINATED as likely waiting for interactive input.")
                    job.result = _build_result(
                        command, -1, full_output, note, wall_time,
                        job.output_limit, inactive=True)
                elif timed_out:
                    note = (f"Error: job timed out after {timeout} seconds and "
                            "was TERMINATED. One varied retry at most — a second "
                            "timeout death means the limit is the wall: report "
                            "to the user.")
                    job.result = _build_result(
                        command, -1, full_output, note, wall_time,
                        job.output_limit, timed_out=True)
                else:
                    note = _mute_diagnostic(exit_code, full_output, "",
                                            wall_time, timeout)
                    if _exit_code_note(command, exit_code):
                        note = ""
                    job.result = _build_result(
                        command, exit_code, full_output, note, wall_time,
                        job.output_limit)
                logging.info(f"Background shell job #{job.job_id} finished with exit code {exit_code}.")
            except Exception as e:
                logging.error(f"Background shell job #{job.job_id} failed: {e}", exc_info=True)
                job.result = {"stdout": "", "stderr": f"Job failed: {e}", "exit_code": -1}
            finally:
                job.done.set()

        threading.Thread(target=_target, daemon=True,
                         name=f"shell-job-{job.job_id}").start()

    def launch(self, command: str, working_directory, timeout: float,
               output_limit: int = POLL_OUTPUT_CAP,
               inactivity_timeout: float = 0) -> dict:
        # A background job is meant to outlive the shell timeout. Do not pass
        # the foreground timeout/inactivity limit here: a dev server produces
        # no output after startup and would otherwise be killed as "idle".
        job = self._register(command, 0, output_limit=output_limit,
                             inactivity_timeout=0)
        self._start_job_thread(job, working_directory)
        return {"job_id": job.job_id, "status": "running",
                "hint": "Use poll_shell_job to check progress, stop_shell_job to abort."}

    def launch_and_wait(self, command: str, working_directory, timeout: float,
                        detach_after: float, cancel_event=None,
                        progress_cb=None, output_limit: int = POLL_OUTPUT_CAP,
                        inactivity_timeout: float = 0) -> dict:
        """Runs a command, auto-detaching into a background job when slow.

        Returns the full result if the command finishes within detach_after
        seconds, otherwise a live job handle (same shape as launch) while
        the process keeps running under the original timeout. Either way
        the caller never blocks longer than detach_after. Live progress
        flows through progress_cb during the wait phase.
        """
        job = self._register(command, timeout, cancel_event,
                             output_limit=output_limit,
                             inactivity_timeout=inactivity_timeout)
        self._start_job_thread(job, working_directory)
        start = time.monotonic()
        while not job.done.is_set():
            elapsed = time.monotonic() - start
            if elapsed >= detach_after:
                logging.info(f"Foreground command detached into background job "
                             f"#{job.job_id} after {elapsed:.1f}s.")
                return {"job_id": job.job_id, "status": "running", "detached": True,
                        "hint": "Still running in background. Use poll_shell_job to check, "
                                "stop_shell_job to abort."}
            if progress_cb is not None:
                with job.lock:
                    snapshot = list(job.stdout_parts)
                try:
                    progress_cb(elapsed, _last_useful_line(snapshot))
                except Exception:
                    pass
            time.sleep(0.2)
        return self._take_finished(job.job_id)

    def _take_finished(self, job_id: int) -> dict:
        """Removes a finished job and returns its FULL result dict."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return {"job_id": job_id, "status": "not_found"}
        res = job.result or {"stdout": "", "stderr": NO_RESULT_TEXT,
                             "exit_code": -1}
        result = dict(res)
        result.setdefault("wall_time", round(time.monotonic() - job.started, 2))
        return result

    def describe_live(self) -> str:
        """History line about currently running jobs for every planning turn.

        The model forgets in-flight work between turns and relaunches it
        (ten archive attempts over one live Compress). Seeing this every
        turn — including guidance-steered ones — keeps it from duplicating.
        """
        with self._lock:
            jobs = list(self._jobs.values())
        live = [j for j in jobs if not j.done.is_set()]
        if not live:
            return ""
        parts = []
        for j in live:
            cmd = j.command if len(j.command) <= 80 else j.command[:77] + "..."
            parts.append(f"#{j.job_id} (`{cmd}`, running {int(time.monotonic() - j.started)}s)")
        return ("**Live background jobs (already running — do NOT relaunch this work, "
                "poll it instead):** " + "; ".join(parts) + ".")

    def describe_running(self) -> str:
        """One history line about jobs that outlived their task (or '')."""
        with self._lock:
            jobs = list(self._jobs.values())
        live = [j for j in jobs if not j.done.is_set()]
        if not live:
            return ""
        parts = []
        for j in live:
            cmd = j.command if len(j.command) <= 60 else j.command[:57] + "..."
            parts.append(f"#{j.job_id} ({cmd}, {int(time.monotonic() - j.started)}s)")
        return ("**Background jobs still running from before:** " + "; ".join(parts) +
                ". Poll them with poll_shell_job if relevant to the new task.")

    def _pop_new_output(self, job: ShellJob):
        with job.lock:
            new_out = "".join(job.stdout_parts[job.stdout_offset:])
            new_err = "".join(job.stderr_parts[job.stderr_offset:])
            job.stdout_offset = len(job.stdout_parts)
            job.stderr_offset = len(job.stderr_parts)
            total_out = sum(len(p) for p in job.stdout_parts)
        return new_out, new_err, total_out

    def status(self, job_id: int) -> dict:
        """Lightweight state check: no output consumed, job kept."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "status": "not_found"}
        state = "done" if job.done.is_set() else "running"
        return {"job_id": job_id, "status": state,
                "elapsed_seconds": round(time.monotonic() - job.started, 1)}

    def poll(self, job_id: int) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "status": "not_found",
                    "hint": "Unknown job — it may have already been reaped after finishing."}
        new_out, new_err, total = self._pop_new_output(job)
        elapsed = round(time.monotonic() - job.started, 1)
        if job.done.is_set():
            with self._lock:
                self._jobs.pop(job_id, None)
            if job.result is None:
                # Should be impossible (result is stored before done.set()):
                # canary for a result-visibility race seen once in prod.
                logging.warning(f"poll: job #{job_id} done with no result stored.")
            res = job.result or {"stdout": "", "stderr": NO_RESULT_TEXT,
                                 "exit_code": -1}
            # The kill verdict lives in the stored result, but polls return
            # raw stream buffers — without this note a timeout death looks
            # like a plain failure downstream (loop rules, her assessment).
            stored_err = res.get("stderr", "")
            if res.get("inactivity_timeout"):
                new_out += ("\n[system: this job was killed for producing no "
                            "output — it likely waited for interactive input]")
            elif "timed out after" in stored_err:
                m = re.search(r"timed out after (\d+) seconds", stored_err)
                new_out += (f"\n[system: this job was killed by the shell timeout — "
                            f"timed out after {m.group(1) if m else '?'}s, TERMINATED]")
            elif "Stopped by user." in stored_err:
                new_out += "\n[system: this job was stopped by the user]"
            if res.get("output_file"):
                new_out += f"\n[Full output saved to: {res['output_file']}]"
            return {"job_id": job_id, "status": "done",
                    "exit_code": res.get("exit_code", -1),
                    "elapsed_seconds": elapsed,
                    "wall_time": res.get("wall_time", elapsed),
                    "total_lines": res.get("total_lines"),
                    "total_chars": res.get("total_chars"),
                    "timed_out": res.get("timed_out", False),
                    "cancelled": res.get("cancelled", False),
                    "inactivity_timeout": res.get("inactivity_timeout", False),
                    "exit_note": res.get("exit_note"),
                    "output_file": res.get("output_file"),
                    "output": _cap(new_out + (("\n" + new_err) if new_err else ""),
                                   job.output_limit),
                    "total_stdout_chars": total}
        return {"job_id": job_id, "status": "running",
                "elapsed_seconds": elapsed,
                "output": _cap(new_out + (("\n" + new_err) if new_err else ""),
                               job.output_limit)}

    def stop(self, job_id: int) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "status": "not_found"}
        job.cancel_event.set()
        with job.lock:
            proc = job.proc
        if proc is not None:
            _terminate_tree(proc)
        job.done.wait(timeout=10)
        return self.poll(job_id)

    def kill_all(self, per_job_timeout: float = 3.0):
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            try:
                job.cancel_event.set()
                with job.lock:
                    proc = job.proc
                if proc is not None:
                    _terminate_tree(proc)
            except Exception as e:
                logging.warning(f"kill_all failed for job #{job.job_id}: {e}")
        for job in jobs:
            job.done.wait(timeout=per_job_timeout)
        with self._lock:
            self._jobs.clear()
