import os

from src.computer.computer import tool_meta
from src.computer.http_probe import wait_for_http
from src.computer.shell_jobs import (
    resolve_inactivity_timeout,
    resolve_output_cap,
    resolve_shell_timeout,
    run_shell_command,
)
from src.computer.tools.constants import SHELL_COMMAND_EXAMPLE
from src.computer.tools.locations import expand_path_tokens

@tool_meta(
    tool={
        "name": "execute_shell",
        "description": ("Executes a command in the system's default shell (cmd.exe on Windows, sh/bash elsewhere). "
            "Write plain shell syntax for THAT shell: on Windows use cmd commands (mkdir, dir, echo) or "
            "'powershell -Command \"...\"' for cmdlets. Never hardcode user profile paths (C:\\Users\\...): use portable "
            "tokens {DESKTOP}, {HOME}, {WORKDIR}, {DOCUMENTS}, {DOWNLOADS}, expanded by the executor. "
            "Use the optional cwd argument instead of embedding cd/quoting in the command. "
            "Foreground commands stream live progress and are killed by the shell timeout. For long finite work "
            "(large copies, builds, downloads) set run_in_background=true and poll until done. For servers/daemons "
            "(http.server, npm start, dev servers) also set run_in_background=true and provide ready_url; the call "
            "returns as soon as that URL answers (or reports it is not ready yet). Never wait for a server to exit; "
            "stop it with stop_shell_job. Do NOT wrap a server in start/nohup; run it directly so the job owns and "
            "can stop its process tree. "
            "Returns merged stdout+stderr, exit code and timing. Huge output is kept as head+tail; the full text is "
            "saved to a file and its path is returned. Background jobs always stream their full output to output_file "
            "so long-running logs do not have to stay in memory."),
        "arguments": {
            "command": {"type": "STRING", "description": f"The command line to execute (e.g., {SHELL_COMMAND_EXAMPLE}).", "required": True},
            "run_in_background": {"type": "BOOLEAN", "description": "Set true for long-running commands and servers. Returns {job_id, status} immediately, unless ready_url is supplied (then it waits up to ready_timeout for the probe).", "required": False},
            "ready_url": {"type": "STRING", "description": "Optional URL to probe after starting a background server, e.g. http://127.0.0.1:8000/. Returns ready=true when the server answers, or ready=false with the last error. Only valid with run_in_background=true.", "required": False},
            "ready_timeout": {"type": "NUMBER", "description": "Maximum seconds to wait for ready_url. Default 10, max 120.", "required": False},
            "cwd": {"type": "STRING", "description": "Working directory for this command. Defaults to the terminal's current directory.", "required": False}
        },
        "returns": {
            "type": "OBJECT",
            "description": "A dictionary containing the command's output (foreground) or the background job handle.",
            "properties": {
                "stdout": {"type": "STRING", "description": "Merged stdout+stderr, with head+tail truncation for huge output."},
                "stderr": {"type": "STRING", "description": "System diagnostics only (timeout, cancellation, input blocking)."},
                "exit_code": {"type": "INTEGER", "description": "The exit code of the process. 0 typically means success."},
                "wall_time": {"type": "NUMBER", "description": "Elapsed seconds."},
                "total_lines": {"type": "INTEGER", "description": "Line count before truncation."},
                "total_chars": {"type": "INTEGER", "description": "Character count before truncation."},
                "output_file": {"type": "STRING", "description": "Path to the full output; background jobs always provide a streaming log path."},
                "exit_note": {"type": "STRING", "description": "Non-fatal note, e.g. exit 1 from grep/diff."},
                "job_id": {"type": "INTEGER", "description": "Background job handle (only when run_in_background is true)."},
                "status": {"type": "STRING", "description": "Background job status (only when run_in_background is true)."},
                "ready": {"type": "BOOLEAN", "description": "Present when ready_url was supplied: whether the URL answered before the timeout."},
                "readiness_checked": {"type": "BOOLEAN", "description": "True when a ready_url probe was performed."},
                "ready_url": {"type": "STRING", "description": "Normalized URL that was probed."},
                "status_code": {"type": "INTEGER", "description": "HTTP status code from the last readiness attempt, if any."},
                "readiness_elapsed": {"type": "NUMBER", "description": "Seconds spent on the readiness probe."},
                "readiness_error": {"type": "STRING", "description": "Last transport/HTTP error when ready is false."}
            }
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def execute_shell(self, command: str, run_in_background: bool = False,
                  cwd: str | None = None, ready_url: str | None = None,
                  ready_timeout: float = 10.0) -> dict:
    """
    Executes a shell command in the foreground (streaming, timeout, cancel)
    or launches it as a background job (returns a job_id at once).
    """
    if not command:
        raise ValueError("Action 'execute_shell' failed: Missing required 'command' argument.")
    if ready_url and not run_in_background:
        raise ValueError(
            "Action 'execute_shell' failed: ready_url requires run_in_background=true.")
    if ready_url:
        ready_url = str(ready_url).strip()
        if not ready_url:
            raise ValueError(
                "Action 'execute_shell' failed: ready_url must not be empty.")
        try:
            ready_timeout = float(ready_timeout)
        except (TypeError, ValueError):
            raise ValueError(
                "Action 'execute_shell' failed: ready_timeout must be a number.") from None
        ready_timeout = max(0.5, min(ready_timeout, 120.0))

    working_directory = getattr(self, "working_directory", None)
    if cwd:
        expanded_cwd = expand_path_tokens(str(cwd), working_directory)
        working_directory = self._resolve_path(expanded_cwd)
        if not os.path.isdir(working_directory):
            raise ValueError(
                f"Action 'execute_shell' failed: cwd '{cwd}' is not a directory.")

    settings_manager = getattr(self, "settings_manager", None)
    timeout = resolve_shell_timeout(settings_manager)
    output_limit = resolve_output_cap(settings_manager)
    inactivity_timeout = resolve_inactivity_timeout(settings_manager)
    manager = getattr(self, "shell_jobs", None)
    if run_in_background:
        if manager is None:
            raise RuntimeError("Background shell jobs are not available.")
        # Background jobs are deliberately not bound to the foreground shell
        # timeout or inactivity limit: a server can run indefinitely and is
        # stopped explicitly with stop_shell_job.
        result = manager.launch(
            command, working_directory, 0,
            output_limit=output_limit,
            inactivity_timeout=0)
        if ready_url:
            probe = wait_for_http(
                ready_url,
                timeout=ready_timeout,
                cancel_event=getattr(self, "cancel_event", None))
            job_id = result.get("job_id")
            result.update({
                "ready": probe["ready"],
                "readiness_checked": True,
                "ready_url": probe["url"],
                "status_code": probe["status_code"],
                "readiness_elapsed": probe["elapsed_seconds"],
                "readiness_error": probe["last_error"],
            })
            if probe["ready"]:
                result["hint"] = (
                    f"Background job #{job_id} is running and {probe['url']} "
                    f"answered with HTTP {probe['status_code']} after "
                    f"{probe['elapsed_seconds']}s.")
            else:
                result["hint"] = (
                    f"Background job #{job_id} is still running, but {probe['url']} "
                    f"did not answer within {ready_timeout:g}s. Last error: "
                    f"{probe['last_error'] or 'none'}. Check poll_shell_job and "
                    f"output_file before deciding; do not relaunch blindly.")
        return result

    if manager is not None:
        # Safety net for misjudged durations: a foreground call that runs
        # longer than detach_after transparently becomes a background job
        # instead of blocking (or dying at the timeout).
        detach_setting = 25
        try:
            if settings_manager is not None:
                detach_setting = int((settings_manager.get("terminal", {}) or {}).get(
                    "shell_detach_seconds", 25) or 25)
        except Exception:
            detach_setting = 25
        detach_after = max(1, min(detach_setting, timeout))
        return manager.launch_and_wait(
            command,
            working_directory,
            timeout,
            detach_after,
            cancel_event=getattr(self, "cancel_event", None),
            # Captured once: a newer run may replace the shared callback, but this
            # run keeps reporting under its own ticket (stale ones are dropped).
            progress_cb=getattr(self, "progress_callback", None),
            output_limit=output_limit,
            inactivity_timeout=inactivity_timeout,
        )

    return run_shell_command(
        command,
        working_directory,
        timeout,
        cancel_event=getattr(self, "cancel_event", None),
        progress_cb=getattr(self, "progress_callback", None),
        output_limit=output_limit,
        inactivity_timeout=inactivity_timeout,
    )
