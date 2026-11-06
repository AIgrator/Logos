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
from collections import deque
from itertools import islice

POLL_OUTPUT_CAP = 20000
OUTPUT_FILE_LIMIT = 64 * 1024 * 1024
OUTPUT_MEMORY_LIMIT = 2 * 1024 * 1024
DEFAULT_INACTIVITY_TIMEOUT = 300
JOB_LOG_RETENTION_DAYS = 7
JOB_LOG_TOTAL_LIMIT = 512 * 1024 * 1024

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
    # Piped Python output defaults to the ANSI code page, which the line
    # decoder cannot distinguish from OEM. UTF-8 is unambiguous.
    "PYTHONIOENCODING": "utf-8",
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


_PS_EXE_RE = re.compile(r"^\s*(powershell(?:\.exe)?|pwsh(?:\.exe)?)\b",
                        re.IGNORECASE)
_PS_ARG_RE = re.compile(r"\s+(-\w+(?::\S+)?)(?:\s+((?!-)\S+))?")


def _parse_powershell(command: str):
    """Splits `powershell [flags] -Command <script>` into (exe, flags, script).

    Flags such as -NoProfile or -ExecutionPolicy Bypass may sit between the
    executable and -Command; exact-prefix matching missed those calls, so
    they ran without the UTF-8 console wrapper and lost non-ASCII output.
    """
    match = _PS_EXE_RE.match(command)
    if not match:
        return None
    exe = match.group(1)
    flags: list[str] = []
    pos = match.end()
    while True:
        arg = _PS_ARG_RE.match(command, pos)
        if not arg:
            return None
        name, value = arg.group(1), arg.group(2)
        if name.lower() == "-command":
            return exe, flags, command[arg.end(1):].strip()
        flags.append(name)
        if value:
            flags.append(value)
        pos = arg.end()


def build_shell_command(command: str):
    """Builds (argv, encoding, extra_popen_kwargs) for a shell command.

    `encoding` is 'auto' on Windows cmd: stdout stays binary and every line
    is decoded UTF-8-first, OEM second (see `_decode_output_line`).
    """
    parsed = _parse_powershell(command)
    if parsed is not None:
        exe, flags, inner = parsed
        if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in ('"', "'"):
            inner = inner[1:-1]
        known = {flag.split(":")[0].lower() for flag in flags
                 if flag.startswith("-")}
        for forced in ("-NoProfile", "-NonInteractive"):
            if forced.lower() not in known:
                flags.append(forced)

    extra: dict = {"env": {**os.environ, **_NONINTERACTIVE_ENV}}
    if sys.platform == "win32":
        # Own process group so Ctrl-Break on stop reaches grandchildren too.
        extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        extra["start_new_session"] = True

    if parsed is not None:
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
                 "; $__ok = $?; "
                 "$__code = $LASTEXITCODE; if ($null -eq $__code) { $__code = 0 }; "
                 # Trust the final command's success flag, not $Error.Count:
                 # a caught exception (try/catch -> Write-Output 'CLOSED') still
                 # leaves an entry in $Error and made successful probes look
                 # like failures. Non-terminating errors on the last command
                 # still set $? to false, so real failures keep their exit 1.
                 "if ((-not $__ok) -and ($__code -eq 0)) { $__code = 1 }; "
                 # Use $host.SetShouldExit, NOT `exit`: PowerShell renders object
                 # output (tables) on an async output thread, and `exit` tears the
                 # runspace down before it flushes — so `Get-ChildItem | Select-Object`
                 # came back as a single blank line and the agent concluded a
                 # non-empty folder was empty. SetShouldExit requests the same exit
                 # code but lets the pipeline drain first.
                 "$host.SetShouldExit($__code)")
        return ([exe, *flags, '-Command', inner],
                'utf-8', extra)
    if sys.platform == "win32":
        # Pass the raw command line to shell=True. The list form
        # ['cmd', '/c', command] is re-quoted by subprocess.list2cmdline with
        # backslashes that cmd.exe does not understand, so inner quotes reached
        # the child mangled ("python -c "print(...)" became an unterminated
        # string).
        extra["shell"] = True
        return (command, 'auto', extra)
    extra["shell"] = True
    return (command, 'utf-8', extra)


def _decode_output_line(raw: bytes) -> str:
    """Decodes one line of captured shell output.

    cmd.exe builtins and native tools write the OEM code page while Python,
    git and node write UTF-8; decoding everything as OEM turned Russian text
    into mojibake. UTF-8 is tried first (it never silently accepts OEM
    Cyrillic), OEM is the fallback for the console's own output.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("oem", errors="replace")


def _spawn_shell(argv, encoding, working_directory, extra):
    """Starts the shell; 'auto' keeps stdout binary for per-line decoding."""
    options = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=working_directory,
        **extra,
    )
    if encoding == "auto":
        return subprocess.Popen(argv, **options)
    return subprocess.Popen(argv, text=True, encoding=encoding,
                            errors="ignore", bufsize=1, **options)


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


def _drain_stream(stream, sink, lock=None, activity=None, log_writer=None):
    try:
        while True:
            line = stream.readline()
            if not line:
                break
            if isinstance(line, bytes):
                line = _decode_output_line(line)
            if activity is not None:
                activity[0] = time.monotonic()
            line = _ANSI_RE.sub("", line)
            if log_writer is not None:
                try:
                    log_writer.write(line)
                except Exception:
                    pass
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


def _shell_temp_dir() -> str:
    directory = os.path.join(tempfile.gettempdir(), "logos-shell")
    os.makedirs(directory, exist_ok=True)
    return directory


def _new_shell_output_path(prefix: str, suffix: str = ".txt") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{prefix}-{stamp}-{os.getpid()}-{time.time_ns() % 1_000_000}{suffix}"
    return os.path.join(_shell_temp_dir(), name)


def _new_job_log_path(job_id: int) -> str:
    return _new_shell_output_path(f"job-{job_id}", ".log")


def cleanup_old_job_logs(max_age_days: int = JOB_LOG_RETENTION_DAYS,
                         max_total_bytes: int = JOB_LOG_TOTAL_LIMIT) -> int:
    """Delete stale per-job logs, keeping this process's active logs.

    Background jobs now always write a full log. Without a cap the temp
    directory grows forever, so prune old files and then the oldest survivors
    until the total fits. Files from the current process are skipped: another
    manager in the same process (tests, mode switches) may still be writing.
    """
    directory = _shell_temp_dir()
    try:
        with os.scandir(directory) as scan:
            entries = list(scan)
    except OSError:
        return 0

    now = time.time()
    current_pid = f"-{os.getpid()}-"
    kept = []
    removed = 0
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if not entry.name.startswith("job-") or not entry.name.endswith(".log"):
                continue
            if current_pid in entry.name:
                continue
            stat = entry.stat()
            if max_age_days > 0 and now - stat.st_mtime > max_age_days * 86400:
                os.remove(entry.path)
                removed += 1
                continue
            kept.append((stat.st_mtime, stat.st_size, entry.path))
        except OSError:
            continue

    if max_total_bytes <= 0:
        return removed
    total = sum(size for _, size, _ in kept)
    for _, size, path in sorted(kept):
        if total <= max_total_bytes:
            break
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            continue
    if removed:
        logging.info(f"Shell job log retention removed {removed} old log file(s).")
    return removed


class JobOutputBuffer:
    """Bounded in-memory tail of a job's output.

    Long-running servers can log for hours; keeping every line in RAM was an
    unbounded leak. The full stream goes to the per-job log file instead, so
    memory keeps only a recent tail while every consumer still sees the true
    ``total_chars``/``total_lines``.
    """

    def __init__(self, max_chars: int = OUTPUT_MEMORY_LIMIT):
        self.max_chars = max(1000, max_chars)
        self._parts = deque()
        self._chars = 0
        self.total_chars = 0
        self.total_lines = 0
        self.dropped_chars = 0
        self.dropped_lines = 0

    def append(self, line: str) -> None:
        self._parts.append(line)
        size = len(line)
        self._chars += size
        self.total_chars += size
        self.total_lines += 1
        while self._chars > self.max_chars and len(self._parts) > 1:
            removed = self._parts.popleft()
            removed_size = len(removed)
            self._chars -= removed_size
            self.dropped_chars += removed_size
            self.dropped_lines += 1

    def __iter__(self):
        return iter(self._parts)

    def __len__(self) -> int:
        return len(self._parts)

    def text(self) -> str:
        return "".join(self._parts)

    def render(self) -> str:
        """Tail text with a note when older lines were dropped from memory."""
        text = self.text()
        if not self.dropped_chars:
            return text
        return (f"[... {self.dropped_chars} earlier chars omitted from memory; "
                "the full output is in the job log ...]\n" + text)

    def read_since(self, absolute_line_offset: int) -> tuple:
        """Returns (new_text, new_absolute_line_offset) for polling."""
        start = max(0, absolute_line_offset - self.dropped_lines)
        chunk = "".join(islice(self._parts, start, None))
        if absolute_line_offset < self.dropped_lines:
            omitted = self.dropped_lines - absolute_line_offset
            chunk = (f"[... {omitted} earlier lines omitted from memory; see "
                     "the job log for the full output ...]\n" + chunk)
        return chunk, self.dropped_lines + len(self._parts)


class JobLogWriter:
    """Line-buffered, size-capped log file for one background job."""

    def __init__(self, path: str, limit: int = OUTPUT_FILE_LIMIT):
        self.path = path
        self.limit = limit
        self.written = 0
        self.truncated = False
        self._fh = open(path, "w", encoding="utf-8", errors="replace",
                        buffering=1)

    def header(self, command: str, working_directory) -> None:
        text = (f"$ {command}\n"
                f"[cwd: {working_directory or os.getcwd()}]\n"
                f"[started: {time.strftime('%Y-%m-%d %H:%M:%S')}]\n\n")
        self._fh.write(text)
        self.written += len(text)

    def write(self, line: str) -> None:
        if self.truncated:
            return
        remaining = self.limit - self.written
        if len(line) > remaining:
            if remaining > 0:
                self._fh.write(line[:remaining])
            self._fh.write(f"\n[log truncated at {self.limit} chars]\n")
            self.truncated = True
        else:
            self._fh.write(line)
            self.written += len(line)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def _persist_output(text: str) -> str | None:
    """Writes the full output under %TEMP%/logos-shell and returns the path."""
    try:
        path = _new_shell_output_path("shell")
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


def _expected_exit1(command: str) -> str | None:
    """The pipeline stage whose exit 1 means "no match", if any."""
    for segment in re.split(r"\|\||&&|\|", command.lower()):
        segment = " ".join(segment.strip().split())
        for prefix in _EXIT1_NORMAL:
            if segment == prefix or segment.startswith(prefix + " "):
                return prefix
    return None


def _exit_code_note(command: str, exit_code: int) -> str | None:
    lowered = " ".join(command.strip().lower().split())
    if lowered == "robocopy" or lowered.startswith("robocopy "):
        # Robocopy uses bits: 0-7 are success variants (1 = files copied,
        # 2 = extra files seen, 3 = both), 8+ is a real failure. Quiet
        # invocations produce no output, so exit 1 otherwise looks like a
        # crash even though the copy succeeded.
        if 0 <= exit_code < 8:
            return (f"Exit code {exit_code} from robocopy is success "
                    "(0-7 = no failure; 8+ = failure).")
        return None
    if exit_code != 1:
        return None
    prefix = _expected_exit1(command)
    if prefix:
        return (f"Exit code 1 from {prefix} usually means no matches / "
                "no differences / condition false - not a tool failure.")
    return None


def _build_result(command: str, exit_code: int, full_output: str,
                  system_note: str, wall_time: float, output_limit: int,
                  timed_out: bool = False, cancelled: bool = False,
                  inactive: bool = False, log_path: str | None = None,
                  total_chars: int | None = None,
                  total_lines: int | None = None) -> dict:
    if total_chars is None:
        total_chars = len(full_output)
    if total_lines is None:
        total_lines = full_output.count("\n") + (
            1 if full_output and not full_output.endswith("\n") else 0)
    if log_path:
        # Background jobs stream their full output to a log file, so the
        # in-memory tail is never re-persisted and the log path is always
        # reported even when the inline preview fit under the cap.
        output_file = log_path
        stdout = _cap(full_output, output_limit) if full_output else "(no output)"
        if total_chars > output_limit:
            stdout += f"\n\n[Full output saved to: {log_path}]"
    else:
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


JOB_STATES = ("starting", "running", "ready", "finished", "failed", "stopped")


def job_state_from_result(result: dict | None) -> str:
    """Map a final job result to the user-facing job state."""
    result = result or {}
    if result.get("cancelled"):
        return "stopped"
    if result.get("timed_out") or result.get("inactivity_timeout"):
        return "failed"
    if result.get("exit_code") == 0 or result.get("exit_note"):
        return "finished"
    return "failed"


def _running_job_state(job: "ShellJob") -> str:
    return "ready" if job.ready else "running"


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
               progress_cb, out_sink, lock=None,
               inactivity_timeout: float = 0, log_writer=None):
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
                         kwargs={"log_writer": log_writer},
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

    # stop() sets the cancel event and then kills the tree immediately. The
    # process can be gone before the loop observes the flag, so check it once
    # more: without this a user-stopped job looked like a crash/exit instead
    # of "Stopped by user." in poll/stop results and the UI.
    if cancel_event is not None and cancel_event.is_set():
        cancelled = True
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
            proc = _spawn_shell(argv, encoding, working_directory, extra)
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
        self.stdout_parts = JobOutputBuffer()
        self.stderr_parts = JobOutputBuffer()
        self.stdout_offset = 0
        self.stderr_offset = 0
        self.log_path = ""
        self.proc = None
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        # A foreground command waits on the job inside launch_and_wait; the user
        # can move it into the background (link in the step row / Ctrl+B), so
        # the wait loop watches this flag as well as its own detach budget.
        self.detach_event = threading.Event()
        self.foreground = False
        self.result = None
        self.started = time.monotonic()
        self.ready = False
        self.ready_url = None
        self.ready_status_code = None


class ShellJobManager:
    """Registry of background shell jobs (launch/poll/stop/kill_all)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._counter = 0
        self._listeners_lock = threading.Lock()
        self._listeners = []
        try:
            cleanup_old_job_logs()
        except Exception as e:
            logging.warning(f"Shell job log retention failed: {e}")

    def add_listener(self, callback):
        """Register a job-event callback; returns it for later removal."""
        if callback is None:
            return None
        with self._listeners_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)
        return callback

    def remove_listener(self, callback):
        with self._listeners_lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def _emit(self, event: dict):
        with self._listeners_lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(event)
            except Exception as e:
                logging.warning(f"Shell job listener failed: {e}")

    def _emit_finished(self, job: ShellJob):
        result = job.result or {
            "stdout": "",
            "stderr": NO_RESULT_TEXT,
            "exit_code": -1,
        }
        elapsed = round(time.monotonic() - job.started, 1)
        event_type = "stopped" if result.get("cancelled") else "finished"
        self._emit({
            "type": event_type,
            "job_id": job.job_id,
            "command": job.command,
            "status": "done",
            "state": job_state_from_result(result),
            "exit_code": result.get("exit_code", -1),
            "elapsed_seconds": elapsed,
            "output_file": result.get("output_file") or job.log_path,
            "cancelled": bool(result.get("cancelled")),
            "result": dict(result),
        })

    def _register(self, command: str, timeout: float, cancel_event=None,
                  output_limit: int = POLL_OUTPUT_CAP,
                  inactivity_timeout: float = 0) -> ShellJob:
        with self._lock:
            self._counter += 1
            job_id = self._counter
            job = ShellJob(job_id, command, timeout, output_limit,
                           inactivity_timeout)
            job.log_path = _new_job_log_path(job_id)
            if cancel_event is not None:
                job.cancel_event = cancel_event
            self._jobs[job_id] = job
        return job

    def _start_job_thread(self, job: ShellJob, working_directory):
        command = job.command
        timeout = job.timeout

        def _target():
            log_writer = None
            try:
                try:
                    log_writer = JobLogWriter(job.log_path)
                    log_writer.header(command, working_directory)
                except Exception as e:
                    logging.warning(f"Could not open log for job #{job.job_id}: {e}")
                    log_writer = None
                argv, encoding, extra = build_shell_command(command)
                logging.info(f"Background shell job #{job.job_id} started: {command}")
                try:
                    proc = _spawn_shell(argv, encoding, working_directory, extra)
                except FileNotFoundError:
                    job.result = {"stdout": "",
                                  "stderr": f"Error: Command not found: {command.split()[0]}",
                                  "exit_code": 127,
                                  "output_file": job.log_path}
                    return
                with job.lock:
                    job.proc = proc
                self._emit({
                    "type": "started",
                    "job_id": job.job_id,
                    "command": command,
                    "status": "running",
                    "state": "starting",
                    "elapsed_seconds": 0,
                    "output_file": job.log_path,
                })
                timed_out, cancelled, inactive = pump_shell(
                    proc, timeout, job.cancel_event, None,
                    job.stdout_parts, job.lock, job.inactivity_timeout,
                    log_writer=log_writer)
                full_output = job.stdout_parts.render()
                exit_code = proc.returncode if proc.returncode is not None else -1
                wall_time = time.monotonic() - job.started

                def _result(code, note, **kwargs):
                    return _build_result(
                        command, code, full_output, note, wall_time,
                        job.output_limit, log_path=job.log_path,
                        total_chars=job.stdout_parts.total_chars,
                        total_lines=job.stdout_parts.total_lines, **kwargs)

                if cancelled:
                    job.result = _result(-2, "Stopped by user.", cancelled=True)
                elif inactive:
                    note = (f"Error: job produced no output for "
                            f"{job.inactivity_timeout:.0f} seconds and was "
                            "TERMINATED as likely waiting for interactive input.")
                    job.result = _result(-1, note, inactive=True)
                elif timed_out:
                    note = (f"Error: job timed out after {timeout} seconds and "
                            "was TERMINATED. One varied retry at most — a second "
                            "timeout death means the limit is the wall: report "
                            "to the user.")
                    job.result = _result(-1, note, timed_out=True)
                else:
                    note = _mute_diagnostic(exit_code, full_output, "",
                                            wall_time, timeout)
                    if _exit_code_note(command, exit_code):
                        note = ""
                    job.result = _result(exit_code, note)
                logging.info(f"Background shell job #{job.job_id} finished with exit code {exit_code}.")
            except Exception as e:
                logging.error(f"Background shell job #{job.job_id} failed: {e}", exc_info=True)
                job.result = {"stdout": "", "stderr": f"Job failed: {e}",
                              "exit_code": -1, "output_file": job.log_path}
            finally:
                if log_writer is not None:
                    log_writer.close()
                job.done.set()
                try:
                    self._emit_finished(job)
                except Exception as e:
                    logging.warning(f"Could not emit finish event for job "
                                    f"#{job.job_id}: {e}")

        threading.Thread(target=_target, daemon=True,
                         name=f"shell-job-{job.job_id}").start()

    def mark_ready(self, job_id: int, url: str, status_code: int | None = None) -> bool:
        """Remember that a server job answered its readiness probe."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.done.is_set():
            return False
        job.ready = True
        job.ready_url = url
        job.ready_status_code = status_code
        return True

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
                "state": "starting",
                "output_file": job.log_path,
                "hint": "Use poll_shell_job to check progress, stop_shell_job to abort. "
                        "Full output streams to output_file."}

    def launch_and_wait(self, command: str, working_directory, timeout: float,
                        detach_after: float, cancel_event=None,
                        progress_cb=None, output_limit: int = POLL_OUTPUT_CAP,
                        inactivity_timeout: float = 0) -> dict:
        """Runs a command, auto-detaching into a background job when slow.

        Returns the full result if the command finishes within detach_after
        seconds, otherwise a live job handle (same shape as launch) while
        the process keeps running under the original timeout. Either way
        the caller never blocks longer than detach_after. Live progress
        flows through progress_cb during the wait phase. `request_detach`
        ends the wait early — that is the manual "move to background" path.
        """
        job = self._register(command, timeout, cancel_event,
                             output_limit=output_limit,
                             inactivity_timeout=inactivity_timeout)
        job.foreground = True
        self._start_job_thread(job, working_directory)
        start = time.monotonic()
        try:
            while not job.done.is_set():
                elapsed = time.monotonic() - start
                if job.detach_event.is_set():
                    logging.info(f"Foreground command moved to background job "
                                 f"#{job.job_id} by the user after {elapsed:.1f}s.")
                    return {"job_id": job.job_id, "status": "running",
                            "detached": True, "detached_by_user": True,
                            "state": "starting",
                            "output_file": job.log_path,
                            "hint": ("The user moved this command to the "
                                     "background. It keeps running; continue with "
                                     "the rest of the task and use poll_shell_job "
                                     "to check it, stop_shell_job to abort.")}
                if elapsed >= detach_after:
                    logging.info(f"Foreground command detached into background job "
                                 f"#{job.job_id} after {elapsed:.1f}s.")
                    return {"job_id": job.job_id, "status": "running", "detached": True,
                            "detached_by_time": True,
                            "state": "starting",
                            "output_file": job.log_path,
                            "hint": ("The foreground command was still running "
                                     f"after {elapsed:.0f}s, so it was moved to the "
                                     "background automatically. It is the same command, "
                                     "not a background launch you asked for, so do not "
                                     "relaunch it. Tell the user it keeps running in the "
                                     "background strip and ask before stopping it. Check "
                                     "it with poll_shell_job, pacing polls with "
                                     "wait_seconds; full output streams to output_file.")}
                if progress_cb is not None:
                    with job.lock:
                        snapshot = list(job.stdout_parts)
                    try:
                        progress_cb(elapsed, _last_useful_line(snapshot))
                    except Exception:
                        pass
                time.sleep(0.2)
            return self._take_finished(job.job_id)
        finally:
            job.foreground = False

    def foreground_job_id(self) -> int | None:
        """The job a foreground command is still waiting on, if any.

        Only one foreground command runs at a time (one exec thread), so the
        newest waiting job is the one a "move to background" click means.
        """
        with self._lock:
            waiting = [job for job in self._jobs.values()
                       if job.foreground and not job.done.is_set()]
        if not waiting:
            return None
        return max(waiting, key=lambda job: job.job_id).job_id

    def request_detach(self, job_id: int) -> dict:
        """Ends a foreground command's wait; the process becomes a live job."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "status": "not_found"}
        if job.done.is_set():
            return {"job_id": job_id, "status": "done"}
        job.detach_event.set()
        return {"job_id": job_id, "status": "running", "state": "starting"}


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
        result.setdefault("state", job_state_from_result(res))
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
            new_out, job.stdout_offset = job.stdout_parts.read_since(
                job.stdout_offset)
            new_err, job.stderr_offset = job.stderr_parts.read_since(
                job.stderr_offset)
            total_out = job.stdout_parts.total_chars
        return new_out, new_err, total_out

    def status(self, job_id: int) -> dict:
        """Lightweight state check: no output consumed, job kept."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "status": "not_found"}
        if job.done.is_set():
            status = "done"
            job_state = job_state_from_result(job.result)
        else:
            status = "running"
            job_state = _running_job_state(job)
        return {"job_id": job_id, "status": status, "state": job_state,
                "elapsed_seconds": round(time.monotonic() - job.started, 1),
                "output_file": job.log_path}

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
            if res.get("output_file") and total > job.output_limit:
                new_out += f"\n[Full output saved to: {res['output_file']}]"
            return {"job_id": job_id, "status": "done",
                    "state": job_state_from_result(res),
                    "exit_code": res.get("exit_code", -1),
                    "elapsed_seconds": elapsed,
                    "wall_time": res.get("wall_time", elapsed),
                    "total_lines": res.get("total_lines"),
                    "total_chars": res.get("total_chars"),
                    "timed_out": res.get("timed_out", False),
                    "cancelled": res.get("cancelled", False),
                    "inactivity_timeout": res.get("inactivity_timeout", False),
                    "exit_note": res.get("exit_note"),
                    "output_file": res.get("output_file") or job.log_path,
                    "output": _cap(new_out + (("\n" + new_err) if new_err else ""),
                                   job.output_limit),
                    "total_stdout_chars": total}
        return {"job_id": job_id, "status": "running",
                "state": _running_job_state(job),
                "elapsed_seconds": elapsed,
                "output_file": job.log_path,
                "output": _cap(new_out + (("\n" + new_err) if new_err else ""),
                               job.output_limit)}

    def request_stop(self, job_id: int) -> dict:
        """Ask a job to stop without blocking the caller (for the UI).

        The pump sees the cancel event within ~200 ms and terminates the
        process tree itself; a later ``finished``/``stopped`` event carries
        the result. ``stop`` remains the synchronous model-tool variant.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "status": "not_found"}
        if job.done.is_set():
            return {"job_id": job_id, "status": "done",
                    "state": job_state_from_result(job.result)}
        job.cancel_event.set()
        return {"job_id": job_id, "status": "stopping",
                "state": _running_job_state(job)}

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
