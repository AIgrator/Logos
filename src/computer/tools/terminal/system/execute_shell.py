from src.computer.computer import tool_meta
from src.computer.shell_jobs import (
    resolve_inactivity_timeout,
    resolve_output_cap,
    resolve_shell_timeout,
    run_shell_command,
)
from src.computer.tools.constants import SHELL_COMMAND_EXAMPLE

@tool_meta(
    tool={
        "name": "execute_shell",
        "description": ("Executes a command in the system's default shell (cmd.exe on Windows, sh/bash elsewhere). "
            "Write plain shell syntax for THAT shell: on Windows use cmd commands (mkdir, dir, echo) or "
            "'powershell -Command \"...\"' for cmdlets. Never hardcode user profile paths (C:\\Users\\...): use portable "
            "tokens {DESKTOP}, {HOME}, {WORKDIR}, {DOCUMENTS}, {DOWNLOADS}, expanded by the executor. "
            "Foreground commands stream live progress and are killed by the shell timeout. For anything that may run "
            "longer than ~30 seconds (large copies, builds, downloads) set run_in_background=true: it returns a job_id "
            "at once, then alternate wait_seconds and poll_shell_job until status is done. Returns merged stdout+stderr, "
            "exit code and timing. Huge output is kept as head+tail; the full text is saved to a file and its path is returned."),
        "arguments": {
            "command": {"type": "STRING", "description": f"The command line to execute (e.g., {SHELL_COMMAND_EXAMPLE}).", "required": True},
            "run_in_background": {"type": "BOOLEAN", "description": "Set true for long-running commands. Returns {job_id, status} immediately instead of blocking.", "required": False}
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
                "output_file": {"type": "STRING", "description": "Path to the full output when it exceeded the inline cap."},
                "exit_note": {"type": "STRING", "description": "Non-fatal note, e.g. exit 1 from grep/diff."},
                "job_id": {"type": "INTEGER", "description": "Background job handle (only when run_in_background is true)."},
                "status": {"type": "STRING", "description": "Background job status (only when run_in_background is true)."}
            }
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def execute_shell(self, command: str, run_in_background: bool = False) -> dict:
    """
    Executes a shell command in the foreground (streaming, timeout, cancel)
    or launches it as a background job (returns a job_id at once).
    """
    if not command:
        raise ValueError("Action 'execute_shell' failed: Missing required 'command' argument.")

    settings_manager = getattr(self, "settings_manager", None)
    timeout = resolve_shell_timeout(settings_manager)
    output_limit = resolve_output_cap(settings_manager)
    inactivity_timeout = resolve_inactivity_timeout(settings_manager)
    manager = getattr(self, "shell_jobs", None)
    if run_in_background:
        if manager is None:
            raise RuntimeError("Background shell jobs are not available.")
        return manager.launch(
            command, self.working_directory, timeout,
            output_limit=output_limit,
            inactivity_timeout=inactivity_timeout)

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
            self.working_directory,
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
        self.working_directory,
        timeout,
        cancel_event=getattr(self, "cancel_event", None),
        progress_cb=getattr(self, "progress_callback", None),
        output_limit=output_limit,
        inactivity_timeout=inactivity_timeout,
    )
