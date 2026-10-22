"""Small HTTP readiness probe for background servers.

A fixed sleep after launching a dev server is either too short (the model
checks too early) or too long (wasted turns). The probe polls the URL until
the server answers with a non-5xx HTTP response or the deadline expires.
"""
import logging
import time
import urllib.error
import urllib.request


def _normalize_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        raise ValueError("ready_url is empty.")
    if "://" not in url:
        url = "http://" + url
    return url


def wait_for_http(url: str, timeout: float = 10.0, interval: float = 0.5,
                  cancel_event=None) -> dict:
    """Poll an HTTP URL until it answers, the deadline expires, or cancelled.

    Any response below 500 counts as ready: 401/403/404 still prove that the
    HTTP server is listening and routing. 5xx is retried because many dev
    servers return it while their app is still booting.
    """
    target = _normalize_url(url)
    timeout = max(0.0, float(timeout))
    interval = max(0.05, float(interval))
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    last_error = None
    status_code = None
    cancelled = False

    while True:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        attempts += 1
        remaining = deadline - time.monotonic()
        request_timeout = max(0.5, min(2.0, remaining)) if timeout else 1.0
        try:
            request = urllib.request.Request(
                target, headers={"User-Agent": "LogosReadinessProbe/1.0"})
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                status_code = int(getattr(response, "status", 0) or 0)
                try:
                    response.read(1)
                except Exception:
                    pass
                if status_code < 500:
                    return {
                        "ready": True,
                        "url": target,
                        "status_code": status_code,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                        "attempts": attempts,
                        "last_error": None,
                        "cancelled": False,
                    }
                last_error = f"HTTP {status_code}"
        except urllib.error.HTTPError as e:
            status_code = int(getattr(e, "code", 0) or 0)
            if status_code < 500:
                return {
                    "ready": True,
                    "url": target,
                    "status_code": status_code,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "attempts": attempts,
                    "last_error": None,
                    "cancelled": False,
                }
            last_error = f"HTTP {status_code}"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    elapsed = round(time.monotonic() - started, 2)
    if last_error:
        logging.info(f"Readiness probe failed for {target} after {elapsed}s: "
                     f"{last_error}")
    return {
        "ready": False,
        "url": target,
        "status_code": status_code,
        "elapsed_seconds": elapsed,
        "attempts": attempts,
        "last_error": last_error,
        "cancelled": cancelled,
    }
