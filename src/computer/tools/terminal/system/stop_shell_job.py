from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "stop_shell_job",
        "description": ("Aborts a background shell job started with run_in_background=true. "
            "Kills the whole process tree. Returns the job's final status."),
        "arguments": {
            "job_id": {"type": "INTEGER", "description": "The job handle returned by execute_shell.", "required": True}
        },
        "returns": {
            "type": "OBJECT",
            "description": "Final job status.",
            "properties": {
                "status": {"type": "STRING", "description": "done, stopped or not_found."}
            }
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def stop_shell_job(self, job_id: int) -> dict:
    """
    Aborts a background shell job and reaps it.
    """
    from src.computer.shell_jobs import parse_job_id
    manager = getattr(self, "shell_jobs", None)
    if manager is None:
        raise RuntimeError("Background shell jobs are not available.")
    try:
        job_id = parse_job_id(job_id)
    except ValueError as e:
        raise ValueError(f"Action 'stop_shell_job' failed: {e}") from None
    return manager.stop(job_id)
