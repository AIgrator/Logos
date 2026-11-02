"""Chromium runtime for Browser mode: where it lives and how it is installed.

Python Playwright starts its driver with ``PLAYWRIGHT_BROWSERS_PATH=0``
(``playwright/_impl/_transport.py``), which means "the ``.local-browsers``
folder inside the driver package". In a frozen Logos build that folder sits in
the PyInstaller ``_MEI`` temp directory: it is re-created on every launch and
deleted on exit, so a browser downloaded there can never be found again. Logos
pins the variable to a stable per-user folder before the driver starts and
installs Chromium into that same folder, once.

The folder follows Playwright's own default (``%LOCALAPPDATA%\\ms-playwright``
on Windows, ``~/Library/Caches/ms-playwright`` on macOS,
``~/.cache/ms-playwright`` elsewhere), so a browser installed earlier with
``playwright install`` — by this application or by any other project — is
reused instead of downloaded twice. Set ``PLAYWRIGHT_BROWSERS_PATH`` to move
it somewhere else, for example a larger disk.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"

#: Shown in the UI before the download starts: Chromium itself (~198 MB) plus
#: the headless shell Playwright uses for headless mode, ffmpeg and winldd.
DOWNLOAD_SIZE = "~320 MB"
#: Roughly what those archives occupy once unpacked.
DISK_SIZE = "~700 MB"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# "Downloading Chromium 156.0.8078.4 (playwright build v1248) from
# https://cdn.playwright.dev/...": the link is noise in a status label.
_URL_RE = re.compile(r"\s*(?:from\s+)?https?://\S+", re.IGNORECASE)
# The driver draws its own bar with block characters between two pipes, e.g.
# "|████████████      | 20% of 197.5 MiB"; all of it is decoration.
_PROGRESS_RE = re.compile("[\u2588\u2589\u2593\u2592\u2591\u25a0\u2500-\u257f\u00a6|]+")
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
_SIZE_RE = re.compile(r"of\s+([\d.,]+\s*[KMG]i?B)", re.IGNORECASE)


def _default_browsers_dir() -> Path:
    """Playwright's own browser cache for this platform."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "ms-playwright"


def browsers_dir() -> Path:
    """Stable folder holding Playwright browsers (never the frozen temp folder).

    ``PLAYWRIGHT_BROWSERS_PATH=0`` is deliberately not honoured: it means
    "inside the driver package", which for a frozen build is the temporary
    unpack folder that disappears together with the executable.
    """
    configured = (os.environ.get(BROWSERS_PATH_ENV) or "").strip()
    if configured and configured != "0":
        return Path(configured).expanduser().absolute()
    return _default_browsers_dir()


def pin_environment() -> Path:
    """Points Playwright at :func:`browsers_dir` for this process and its drivers.

    Must run before ``sync_playwright().start()`` (the driver inherits the
    environment) and before any install, so lookup and download agree.
    """
    path = browsers_dir()
    os.environ[BROWSERS_PATH_ENV] = str(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(f"Could not create the browser folder {path}: {exc}")
    return path


def _driver_package() -> Path | None:
    """Folder with the bundled Playwright driver (``cli.js`` and ``browsers.json``)."""
    try:
        import playwright
    except Exception:  # pragma: no cover - playwright is a hard dependency
        return None
    return Path(playwright.__file__).parent / "driver" / "package"


def chromium_revision() -> str | None:
    """Revision of the Chromium build this Playwright release expects."""
    package = _driver_package()
    if package is None:
        return None
    try:
        data = json.loads((package / "browsers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in data.get("browsers", []):
        if entry.get("name") == "chromium":
            return str(entry.get("revision") or "") or None
    return None


def _executable_patterns(platform: str) -> tuple[str, ...]:
    if platform == "win32":
        # chrome-win64 since build 1105, chrome-win before that.
        return ("chrome-win*/chrome.exe",)
    if platform == "darwin":
        return ("chrome-mac*/*.app/Contents/MacOS/*",)
    return ("chrome-linux*/chrome",)


def chromium_executable() -> Path | None:
    """Path to the Chromium executable Playwright will launch, or None."""
    revision = chromium_revision()
    if not revision:
        return None
    root = browsers_dir() / f"chromium-{revision}"
    if not root.is_dir():
        return None
    for pattern in _executable_patterns(sys.platform):
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def is_chromium_installed() -> bool:
    """True when Browser mode can start without downloading anything."""
    try:
        return chromium_executable() is not None
    except OSError as exc:
        logger.warning(f"Could not inspect the browser folder: {exc}")
        return False


def install_command() -> list[str] | None:
    """Command that downloads Chromium for this Playwright build."""
    package = _driver_package()
    if package is None:
        return None
    cli = package / "cli.js"
    node = Path(os.environ.get("PLAYWRIGHT_NODEJS_PATH")
                or package.parent / ("node.exe" if sys.platform == "win32" else "node"))
    if not cli.is_file() or not node.is_file():
        return None
    return [str(node), str(cli), "install", "chromium"]


def _clean_line(line: str) -> str:
    """Turns a driver progress line into something worth showing in a label."""
    text = _ANSI_RE.sub("", line)
    text = _URL_RE.sub("", text)
    text = text.replace("\r", " ").replace("\b", "")
    text = _PROGRESS_RE.sub(" ", text)
    return " ".join(text.split()).strip()


def progress_percent(text: str) -> int | None:
    """Percentage carried by a progress line (``20% of 197.5 MiB``), or None."""
    match = _PERCENT_RE.search(text)
    if not match:
        return None
    try:
        return max(0, min(100, int(match.group(1))))
    except ValueError:  # pragma: no cover - regex only matches digits
        return None


def archive_name(text: str) -> str | None:
    """Friendly name of the archive a progress line belongs to.

    ``playwright install chromium`` fetches several archives one after another
    (Chromium, its headless shell, ffmpeg, winldd), each with its own 0-100%
    bar, so the dialog names the current one to explain the resets.
    """
    lowered = text.lower()
    if "winldd" in lowered:
        return "winldd"
    if "ffmpeg" in lowered:
        return "FFmpeg"
    if "headless shell" in lowered:
        return "Chromium headless shell"
    if "chrome for testing" in lowered or "chromium" in lowered:
        return "Chromium"
    return None


def describe_progress(text: str, archive: str = "") -> tuple[int | None, str, str]:
    """Splits one driver line into ``(percent, archive, label)``.

    ``archive`` is the archive seen so far: percentage lines carry no name of
    their own ("20% of 197.5 MiB"), so the caller passes the current one in.
    ``percent`` is None for ordinary status lines (the archive title, the final
    "downloaded to ..." line), so the caller can keep the bar where it is.
    ``label`` is what the dialog shows: the file name and its size while it
    downloads, the name alone when a new file starts, and "" for the lines in
    between (the caller keeps the last label).
    """
    name = archive_name(text) or archive
    percent = progress_percent(text)
    if percent is None:
        return None, name, name if name != archive else ""
    size = _SIZE_RE.search(text)
    if name and size:
        label = f"{name} · {size.group(1)}"
    elif name:
        label = f"{name} · downloading"
    elif size:
        label = f"Downloading · {size.group(1)}"
    else:
        label = text
    return percent, name, label


def _is_progress_line(text: str) -> bool:
    """True for lines a user should see while the download runs."""
    if not text or "://" in text:
        return False
    if "%" in text:
        return True
    return text.startswith(("Chrome for Testing", "Chromium", "FFmpeg", "Downloading", "Installing", "Extracting"))


def _failure_detail(tail: list[str], code: int | None) -> str:
    """Picks the most useful line of the driver output for an error message."""
    for line in reversed(tail):
        lowered = line.lower()
        if "://" in line:
            continue
        if "error" in lowered or "failed" in lowered or "not found" in lowered:
            return line
    for line in reversed(tail):
        if "://" not in line:
            return line
    return f"the installer exited with code {code}"


def install_chromium(on_progress=None, cancel_flag=None, timeout: float = 3600.0) -> tuple[bool, str]:
    """Downloads Chromium into :func:`browsers_dir`.

    ``on_progress`` receives short human-readable lines (percent, then
    "Chromium downloaded"); ``cancel_flag`` is polled while the download runs
    and kills it when it returns True. Returns ``(ok, message)``, where the
    message is "cancelled" when the user stopped it.
    """
    pin_environment()
    command = install_command()
    if not command:
        return False, ("This build is missing the Playwright driver, so Chromium "
                       "cannot be downloaded automatically.")
    env = os.environ.copy()
    env[BROWSERS_PATH_ENV] = str(browsers_dir())
    logger.info(f"Downloading Chromium into {env[BROWSERS_PATH_ENV]} ({' '.join(command)})")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            bufsize=0,
            creationflags=creationflags,
        )
    except OSError as exc:
        return False, f"Could not start the Chromium download: {exc}"

    # The driver writes progress with \r on a terminal and with \n otherwise, so
    # a plain line iterator could sit on a whole progress bar. A reader thread
    # feeds finished lines into a queue; the loop below keeps cancel and timeout
    # responsive even while the network is silent.
    lines: queue.Queue[str | None] = queue.Queue()

    def pump(stream) -> None:
        try:
            pending = b""
            while True:
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                if not chunk:
                    break
                pending += chunk
                parts = re.split(rb"[\r\n]", pending)
                pending = parts.pop()
                for raw in parts:
                    lines.put(raw.decode("utf-8", "replace"))
            if pending:
                lines.put(pending.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - the exit code still tells the story
            logger.debug("Chromium download reader stopped", exc_info=True)
        finally:
            lines.put(None)

    assert process.stdout is not None
    reader = threading.Thread(target=pump, args=(process.stdout,), daemon=True, name="chromium-install")
    reader.start()

    started = time.monotonic()
    tail: list[str] = []
    try:
        while True:
            if cancel_flag is not None and cancel_flag():
                _kill(process)
                return False, "cancelled"
            if time.monotonic() - started > timeout:
                _kill(process)
                return False, "The Chromium download took too long and was stopped."
            try:
                raw = lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if raw is None:
                break
            text = _clean_line(raw)
            if not text:
                continue
            tail = (tail + [text])[-8:]
            if on_progress is not None and _is_progress_line(text):
                on_progress(text)
        code = process.wait(timeout=120)
    except Exception as exc:  # noqa: BLE001 - report, never crash the worker
        _kill(process)
        logger.exception("Chromium download failed")
        return False, f"The Chromium download failed: {exc}"

    if is_chromium_installed():
        return True, f"Chromium is ready in {browsers_dir()}."
    detail = _failure_detail(tail, code)
    logger.warning(f"Chromium download finished without a usable browser: {detail}")
    return False, f"Chromium could not be installed ({detail})."


def _kill(process: subprocess.Popen) -> None:
    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=10)
    except Exception:
        pass
