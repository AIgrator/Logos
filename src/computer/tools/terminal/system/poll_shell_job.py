from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "poll_shell_job",
        "description": ("Checks a background shell job started with run_in_background=true. Returns status "
            "(running/done/not_found) plus the output produced since the previous poll (capped). "
            "Polling a finished job returns its final result once, then the job is gone. "
            "Never busy-loop polls — always call wait_seconds between them."),
        "arguments": {
            "job_id": {"type": "INTEGER", "description": "The job handle returned by execute_shell.", "required": True}
        },
        "returns": {
            "type": "OBJECT",
            "description": "Job status with new output since the last poll.",
            "properties": {
                "status": {"type": "STRING", "description": "running, done or not_found."},
                "output": {"type": "STRING", "description": "New output since the previous poll."},
                "exit_code": {"type": "INTEGER", "description": "Present when done."},
                "elapsed_seconds": {"type": "NUMBER", "description": "Seconds since the job started."}
            }
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def poll_shell_job(self, job_id: int) -> dict:
    """
    Returns the status and new output of a background shell job.
    """
    from src.computer.shell_jobs import parse_job_id
    manager = getattr(self, "shell_jobs", None)
    if manager is None:
        raise RuntimeError("Background shell jobs are not available.")
    try:
        job_id = parse_job_id(job_id)
    except ValueError as e:
        raise ValueError(f"Action 'poll_shell_job' failed: {e}") from None
    return manager.poll(job_id)
