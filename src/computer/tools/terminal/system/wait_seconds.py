import time

from src.computer.computer import tool_meta

MAX_WAIT_SECONDS = 600


@tool_meta(
    tool={
        "name": "wait_seconds",
        "description": ("Waits the given number of seconds without blocking the chat, letting a background "
            "shell job progress. Use between poll_shell_job calls (e.g. launch in background, wait 60, "
            "poll, repeat). Interruptible via Stop. Max 600 seconds per call — chain calls for longer waits."),
        "arguments": {
            "seconds": {"type": "INTEGER", "description": "How many seconds to wait (1-600).", "required": False}
        },
        "returns": {
            "type": "OBJECT",
            "description": "How long was actually waited and whether it completed.",
            "properties": {
                "waited_seconds": {"type": "NUMBER", "description": "Seconds actually waited."},
                "completed": {"type": "BOOLEAN", "description": "True unless interrupted via Stop."}
            }
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def wait_seconds(self, seconds: int = 30) -> dict:
    """
    Sleeps cooperatively (cancellable, GUI stays responsive).
    """
    try:
        seconds = int(seconds or 0)
    except Exception:
        seconds = 0
    seconds = max(1, min(seconds, MAX_WAIT_SECONDS))
    cancel_event = getattr(self, "cancel_event", None)
    progress_cb = getattr(self, "progress_callback", None)

    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= seconds:
            return {"waited_seconds": float(seconds), "completed": True, "cancelled": False}
        if cancel_event is not None and cancel_event.is_set():
            return {"waited_seconds": round(elapsed, 1), "completed": False, "cancelled": True}
        if progress_cb is not None:
            try:
                progress_cb(elapsed, f"waiting… {int(elapsed)}/{seconds}s")
            except Exception:
                pass
        time.sleep(0.2)
