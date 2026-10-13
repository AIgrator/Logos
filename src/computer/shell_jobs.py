"""Shared shell-execution machinery plus background job management.

Foreground (`execute_shell`) and background jobs run the same pump; jobs
add a registry so long operations (multi-GB copies, builds) survive across
agent turns: launch returns at once, `poll`/`stop` observe or abort later.
All tool calls still execute on worker threads — the registry is lock
guarded because guidance-during-run can overlap two runners.
"""
import logging
import re
import signal
import subprocess
import sys
import threading
import time

POLL_OUTPUT_CAP = 20000


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

    extra: dict = {}
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
                 "exit $__code")
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


def _drain_stream(stream, sink: list, lock=None):
    try:
        for line in iter(stream.readline, ""):
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
    if len(text) > limit:
        return text[:limit] + f"\n… [truncated, {len(text) - limit} chars hidden]"
    return text


def _mute_diagnostic(exit_code: int, stdout: str, stderr: str,
                     elapsed: float, timeout: float) -> str:
    """Explains an abnormal end with no output (otherwise unreadable).

    A mute abnormal death (timeout text lost, external kill, silent
    crash/OOM) must still tell the user what is known: how long it ran
    against the allowed limit.
    """
    if exit_code == 0 or stdout.strip() or stderr.strip():
        return stderr
    verdict = (f"\n(Process exited with code {exit_code} after {elapsed:.0f}s "
               f"of {timeout:.0f}s allowed with no output at all. Likely: it was "
               f"killed right at the timeout, terminated externally, ran out of "
               f"memory, or crashed silently.)")
    return (stderr + verdict).strip()


NO_RESULT_TEXT = ("No result was recorded (the worker ended without storing one). "
                  "Process state unknown — it may have been killed externally or "
                  "crashed silently.")


def pump_shell(proc: subprocess.Popen, timeout: float, cancel_event,
               progress_cb, out_sink: list, err_sink: list, lock=None):
    """Runs the pump loop; returns (timed_out, cancelled)."""
    readers = [
        threading.Thread(target=_drain_stream, args=(proc.stdout, out_sink, lock),
                         daemon=True),
        threading.Thread(target=_drain_stream, args=(proc.stderr, err_sink, lock),
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
                progress_cb(elapsed, _last_useful_line(_snapshot()))
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
    return timed_out, cancelled


def run_shell_command(command: str, working_directory, timeout: float,
                      cancel_event=None, progress_cb=None) -> dict:
    """Synchronous shell run with streaming, timeout and cancellation."""
    try:
        argv, encoding, extra = build_shell_command(command)
        logging.info(f"Executing shell command: {command} (timeout {timeout}s)")
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
        err_lines: list = []
        run_start = time.monotonic()
        timed_out, cancelled = pump_shell(
            proc, timeout, cancel_event, progress_cb, out_lines, err_lines)
        run_elapsed = time.monotonic() - run_start

        stdout = "".join(out_lines)
        stderr = "".join(err_lines)
        exit_code = proc.returncode if proc.returncode is not None else -1
        logging.info(f"Command finished with exit code {exit_code}.")

        if cancelled:
            return {
                "stdout": stdout,
                "stderr": (stderr + "\nStopped by user.").strip(),
                "exit_code": -2
            }
        if timed_out:
            return {
                "stdout": stdout,
                "stderr": (stderr + f"\nError: Command timed out after {timeout} seconds and was "
                                    f"TERMINATED (output above is partial). Do NOT keep retrying it as-is — "
                                    f"one varied retry at most; if it dies by timeout again, stop and report "
                                    f"to the user (or split the work into smaller pieces).").strip(),
                "exit_code": -1
            }
        return {"stdout": stdout,
                "stderr": _mute_diagnostic(exit_code, stdout, stderr,
                                           run_elapsed, timeout),
                "exit_code": exit_code}
    except Exception as e:
        logging.error(f"An unexpected error occurred while executing command '{command}': {e}",
                      exc_info=True)
        return {"stdout": "", "stderr": f"An unexpected error occurred: {e}", "exit_code": -1}


class ShellJob:
    def __init__(self, job_id: int, command: str, timeout: float):
        self.job_id = job_id
        self.command = command
        self.timeout = timeout
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

    def _register(self, command: str, timeout: float, cancel_event=None) -> ShellJob:
        with self._lock:
            self._counter += 1
            job_id = self._counter
            job = ShellJob(job_id, command, timeout)
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
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
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
                timed_out, cancelled = pump_shell(
                    proc, timeout, job.cancel_event, None,
                    job.stdout_parts, job.stderr_parts, job.lock)
                stdout = "".join(job.stdout_parts)
                stderr = "".join(job.stderr_parts)
                exit_code = proc.returncode if proc.returncode is not None else -1
                if cancelled:
                    job.result = {"stdout": stdout,
                                  "stderr": (stderr + "\nStopped by user.").strip(),
                                  "exit_code": -2}
                elif timed_out:
                    job.result = {"stdout": stdout,
                                  "stderr": (stderr + f"\nError: job timed out after {timeout} "
                                                      f"seconds and was TERMINATED. One varied retry at most — "
                                                      f"a second timeout death means the limit is the wall: "
                                                      f"report to the user.").strip(),
                                  "exit_code": -1}
                else:
                    job.result = {
                        "stdout": stdout,
                        "stderr": _mute_diagnostic(
                            exit_code, stdout, stderr,
                            time.monotonic() - job.started, timeout),
                        "exit_code": exit_code}
                logging.info(f"Background shell job #{job.job_id} finished with exit code {exit_code}.")
            except Exception as e:
                logging.error(f"Background shell job #{job.job_id} failed: {e}", exc_info=True)
                job.result = {"stdout": "", "stderr": f"Job failed: {e}", "exit_code": -1}
            finally:
                job.done.set()

        threading.Thread(target=_target, daemon=True,
                         name=f"shell-job-{job.job_id}").start()

    def launch(self, command: str, working_directory, timeout: float) -> dict:
        job = self._register(command, timeout)
        self._start_job_thread(job, working_directory)
        return {"job_id": job.job_id, "status": "running",
                "hint": "Use poll_shell_job to check progress, stop_shell_job to abort."}

    def launch_and_wait(self, command: str, working_directory, timeout: float,
                        detach_after: float, cancel_event=None,
                        progress_cb=None) -> dict:
        """Runs a command, auto-detaching into a background job when slow.

        Returns the full result if the command finishes within detach_after
        seconds, otherwise a live job handle (same shape as launch) while
        the process keeps running under the original timeout. Either way
        the caller never blocks longer than detach_after. Live progress
        flows through progress_cb during the wait phase.
        """
        job = self._register(command, timeout, cancel_event)
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
        return {"stdout": res.get("stdout", ""), "stderr": res.get("stderr", ""),
                "exit_code": res.get("exit_code", -1)}

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
            if "timed out after" in stored_err:
                m = re.search(r"timed out after (\d+) seconds", stored_err)
                new_out += (f"\n[system: this job was killed by the shell timeout — "
                            f"timed out after {m.group(1) if m else '?'}s, TERMINATED]")
            elif "Stopped by user." in stored_err:
                new_out += "\n[system: this job was stopped by the user]"
            return {"job_id": job_id, "status": "done",
                    "exit_code": res.get("exit_code", -1),
                    "elapsed_seconds": elapsed,
                    "output": _cap(new_out + (("\n" + new_err) if new_err else "")),
                    "total_stdout_chars": total}
        return {"job_id": job_id, "status": "running",
                "elapsed_seconds": elapsed,
                "output": _cap(new_out + (("\n" + new_err) if new_err else ""))}

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
