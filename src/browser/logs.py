"""Console and network capture for the controlled browser.

A page produces hundreds of console lines and requests per navigation, so
nothing here is pasted into the transcript on its own: entries land in bounded
in-memory rings for a quick look (`browser_console`, `browser_network`) and are
appended to a per-launch log file under `<project>/.logos/browser_logs/` that
the model reads in slices with `read_files` or greps with
`search_file_content`. Browser tools in Cursor work the same way, and it is why
a noisy single-page app does not eat the context window.

The capture is deliberately passive: it never blocks, never raises into the
Playwright event loop and keeps only a bounded preview of XHR/fetch response
bodies. A status alone cannot explain the typical SPA failure ("200, but the
screen is empty"), so the model needs the JSON/text the server actually sent.
Bodies are read by the manager after the event (see `BrowserManager`), capped
here, and never stored for documents or assets.
"""
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)

LOG_FILE_LIMIT = 8 * 1024 * 1024  # one launch, then the tail note
LOG_KEEP_FILES = 50
CONSOLE_MAX = 1000
NETWORK_MAX = 3000
ENTRY_MAX_CHARS = 2000
RENDER_MAX_CHARS = 7000
# A response body is evidence, not a document: keep the first part of it and
# say so. The full body would blow up the ring, the log file and the transcript.
BODY_MAX_CHARS = 4000
BODY_PREVIEW_CHARS = 600
BODY_READ_LIMIT = 256 * 1024

# level=… argument of browser_console -> accepted message levels.
_LEVEL_FILTERS = {
    "": None,
    "all": None,
    "any": None,
    "error": ("error",),
    "errors": ("error",),
    "warning": ("warning",),
    "warn": ("warning",),
    "warnings": ("warning",),
    "log": ("log",),
    "info": ("info",),
    "debug": ("debug",),
}


def resolve_log_dir(project_root: str) -> str:
    """``<project>/.logos/browser_logs`` for a project, otherwise no file."""
    root = str(project_root or "").strip()
    if not root or not os.path.isdir(root):
        return ""
    try:
        from src.project_manager import PROJECT_DIRNAME
    except Exception:
        PROJECT_DIRNAME = ".logos"
    return os.path.join(root, PROJECT_DIRNAME, "browser_logs")


def _one_line(text) -> str:
    """Log files are line oriented; keep multi-line messages readable."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace(
        "\n", " \u23ce ").strip()


def level_filter(value) -> tuple | None:
    """Maps the tool argument to accepted levels; unknown values mean 'all'."""
    return _LEVEL_FILTERS.get(str(value or "").strip().lower(), None)


class BrowserEventLog:
    """Bounded console/network rings plus an append-only per-launch log file."""

    def __init__(self, directory: str = "", file_limit: int = LOG_FILE_LIMIT,
                 console_max: int = CONSOLE_MAX, network_max: int = NETWORK_MAX):
        self.directory = directory or ""
        self.file_limit = max(64 * 1024, int(file_limit))
        self._console = deque(maxlen=max(10, int(console_max)))
        self._network = deque(maxlen=max(10, int(network_max)))
        self._lock = threading.Lock()
        self._last_time = 0.0
        self._file = None
        self.path = ""
        self._written = 0
        self._truncated = False
        self._console_total = 0
        self._console_errors = 0
        self._console_warnings = 0
        self._network_total = 0
        self._network_problems = 0
        self._network_seq = 0

    # -- lifecycle ------------------------------------------------------

    def start(self, directory: str | None = None) -> None:
        """Starts a fresh session (one file per browser launch)."""
        if directory is not None:
            self.directory = directory or ""
        self.close()
        if not self.directory:
            return
        try:
            os.makedirs(self.directory, exist_ok=True)
            self._prune()
            name = "browser_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
            path = os.path.join(self.directory, name)
            self._file = open(path, "w", encoding="utf-8", errors="replace",
                              buffering=1)
            self.path = path
            self._write_locked(
                f"# Logos browser log · {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        except Exception as e:
            logger.warning(f"Could not open the browser log file: {e}")
            self._file = None
            self.path = ""

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._file = None
            self.path = ""
            self._written = 0
            self._truncated = False
            self._console.clear()
            self._network.clear()
            self._console_total = 0
            self._console_errors = 0
            self._console_warnings = 0
            self._network_total = 0
            self._network_problems = 0
            self._network_seq = 0

    def _prune(self) -> None:
        """Keeps the newest files; the current session is not written yet."""
        try:
            names = [name for name in os.listdir(self.directory)
                     if name.startswith("browser_") and name.endswith(".log")]
            if len(names) <= LOG_KEEP_FILES:
                return
            paths = [os.path.join(self.directory, name) for name in names]
            paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for stale in paths[LOG_KEEP_FILES:]:
                try:
                    os.remove(stale)
                except OSError:
                    pass
        except Exception as e:
            logger.debug(f"Browser log prune skipped: {e}")

    def _write_locked(self, line: str) -> None:
        if self._file is None or self._truncated:
            return
        try:
            if self._written + len(line) > self.file_limit:
                self._file.write(
                    f"\n[log truncated at {self.file_limit} characters]\n")
                self._truncated = True
                return
            self._file.write(line)
            self._written += len(line)
        except Exception as e:
            logger.debug(f"Browser log write failed: {e}")
            self._truncated = True

    # -- capture --------------------------------------------------------

    def _next_stamp(self) -> str:
        """Monotonic stamp: events queued by Playwright keep their order."""
        with self._lock:
            now = time.time()
            if now <= self._last_time:
                now = self._last_time + 0.001
            self._last_time = now
            millis = int((now % 1) * 1000)
            return time.strftime("%H:%M:%S", time.localtime(now)) + f".{millis:03d}"

    def navigation(self, url: str) -> None:
        stamp = self._next_stamp()
        entry = {"t": stamp, "kind": "nav", "level": "info",
                 "text": f"navigated to {url}", "where": ""}
        with self._lock:
            self._console.append(entry)
            self._write_locked(f"[{stamp}] nav: {url}\n")

    def console(self, level: str, text: str, where: str = "",
                kind: str = "console") -> None:
        level = str(level or "log").lower()
        if level not in ("error", "warning", "log", "info", "debug"):
            level = "log"
        text = _one_line(text)[:ENTRY_MAX_CHARS]
        stamp = self._next_stamp()
        entry = {"t": stamp, "kind": kind, "level": level, "text": text,
                 "where": where}
        with self._lock:
            self._console.append(entry)
            self._console_total += 1
            if level == "error":
                self._console_errors += 1
            elif level == "warning":
                self._console_warnings += 1
            self._write_locked(f"[{stamp}] {kind} {level}: {text}{where}\n")

    def network(self, method: str, url: str, status=None,
                resource_type: str = "", duration_ms=None, error: str = "") -> dict:
        stamp = self._next_stamp()
        failed = bool(error) or status is None
        try:
            code = int(status) if status is not None else None
        except (TypeError, ValueError):
            code = None
        problem = failed or (code is not None and code >= 400)
        entry = {
            "t": stamp, "method": str(method or "GET").upper(),
            "url": _one_line(url)[:1000], "status": code,
            "type": str(resource_type or ""),
            "ms": round(float(duration_ms), 1) if duration_ms is not None else None,
            "failed": failed, "error": _one_line(error)[:300],
            "body": None, "body_type": "", "body_truncated": False,
            "body_error": "",
        }
        with self._lock:
            self._network_seq += 1
            entry["id"] = self._network_seq
            self._network.append(entry)
            self._network_total += 1
            if problem:
                self._network_problems += 1
            self._write_locked(self._network_line(entry) + "\n")
        return entry

    def attach_body(self, entry_id: int, text: str, content_type: str = "",
                    truncated: bool = False, error: str = "") -> None:
        """Attaches a bounded XHR/fetch body to the response entry it belongs to.

        Called after the event, never inside it: reading a body is a protocol
        round-trip, and doing that while Playwright dispatches events can stall
        the page. The newest matching entry wins, so a duplicate id cannot
        overwrite an older response.
        """
        body = "" if error else _one_line(text)[:BODY_MAX_CHARS]
        with self._lock:
            entry = None
            for candidate in reversed(self._network):
                if candidate.get("id") == entry_id:
                    entry = candidate
                    break
            if entry is None:
                return
            entry["body"] = body
            entry["body_type"] = _one_line(content_type)[:120]
            entry["body_truncated"] = bool(truncated)
            entry["body_error"] = _one_line(error)[:300]
            if entry["body_error"]:
                self._write_locked(
                    f"  body: <unavailable: {entry['body_error']}>\n")
            elif body:
                suffix = " …" if truncated else ""
                self._write_locked(f"  body: {body}{suffix}\n")

    @staticmethod
    def _network_line(entry: dict) -> str:
        status = entry.get("status")
        outcome = "FAILED" if entry.get("failed") else status
        head = f"[{entry['t']}] net {outcome} {entry['method']} {entry['url']}"
        extras = []
        if entry.get("type"):
            extras.append(entry["type"])
        if entry.get("ms") is not None:
            extras.append(f"{entry['ms']:g} ms")
        if entry.get("error"):
            extras.append(entry["error"])
        return head + (f" ({', '.join(extras)})" if extras else "")

    @staticmethod
    def _console_line(entry: dict) -> str:
        if entry.get("kind") == "nav":
            return f"[{entry['t']}] nav: {entry['text']}"
        label = entry.get("kind") if entry.get("kind") != "console" else entry["level"]
        return f"[{entry['t']}] {label}: {entry['text']}{entry.get('where') or ''}"

    # -- reading --------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "console": self._console_total,
                "console_errors": self._console_errors,
                "console_warnings": self._console_warnings,
                "network": self._network_total,
                "network_problems": self._network_problems,
                "truncated": self._truncated,
            }

    def render_console(self, limit: int = 40, level: str = "all") -> str:
        levels = level_filter(level)
        with self._lock:
            entries = list(self._console)
        if levels:
            entries = [e for e in entries if e["level"] in levels]
        entries = entries[-max(1, int(limit)):]
        return _clipped("\n".join(self._console_line(e) for e in entries))

    def render_network(self, limit: int = 40, only_problems: bool = False,
                       include_body: bool = True) -> str:
        with self._lock:
            entries = list(self._network)
        if only_problems:
            entries = [e for e in entries
                       if e["failed"] or (e["status"] or 0) >= 400]
        entries = entries[-max(1, int(limit)):]
        lines = []
        for entry in entries:
            lines.append(self._network_line(entry))
            if include_body and entry.get("body_error"):
                lines.append(f"    body: <unavailable: {entry['body_error']}>")
            elif include_body and entry.get("body"):
                body = entry["body"]
                if len(body) > BODY_PREVIEW_CHARS:
                    body = body[:BODY_PREVIEW_CHARS] + "…"
                suffix = " (truncated)" if entry.get("body_truncated") else ""
                lines.append(f"    body{suffix}: {body}")
        return _clipped("\n".join(lines))

    def read_hint(self) -> str:
        if not self.path:
            return ""
        capped = (" The file reached its size cap and stopped growing; the newest "
                  "entries are still in memory.") if self._truncated else ""
        return (f"Full log: {self.path} (read it in slices with read_files, "
                f"search it with search_file_content).{capped}")


def _clipped(text: str, limit: int = RENDER_MAX_CHARS) -> str:
    """Keeps the newest part: recent failures matter more than old noise."""
    if len(text) <= limit:
        return text
    return "(showing the tail)\n" + text[-limit:]
