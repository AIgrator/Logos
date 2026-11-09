"""Browser automation backend (Phase A: standalone, UI-independent).

Owns the Playwright lifecycle and exposes a ref-based grounding contract:
every snapshot assigns stable integer refs to visible interactive elements;
actions address elements by ref, and by viewport coordinates only where no
ref can exist (canvas, custom widgets, cross-origin frames). Designed to
later become the `dom` GroundingBackend without changing callers.
"""
import json
import logging
import os
import shutil
import time
from collections import deque
from urllib.parse import urlsplit

from .logs import BODY_MAX_CHARS, BODY_READ_LIMIT, BrowserEventLog
from .policy import (BrowserCancelledError, DEFAULT_NETWORK_ACCESS,
                     is_sensitive_text, normalize_network_access,
                     redact_url, validate_url)

logger = logging.getLogger(__name__)

_SVG_SHAPE_ONCLICK = (
    "svg[onclick], circle[onclick], ellipse[onclick], rect[onclick], "
    "polygon[onclick], path[onclick]"
)
_INTERACTIVE_SELECTOR = (
    "a[href], button, input, select, textarea, div[onclick], "
    + _SVG_SHAPE_ONCLICK + ", "
    "[role=button], [role=link], [role=checkbox], [role=radio], "
    "[role=switch], [role=tab], [role=menuitem]"
)

_TAG_TO_ROLE = {
    "a": "link", "button": "button", "input": "textbox",
    "select": "combobox", "textarea": "textbox",
    # Only clickable divs/shapes match the selector, so "button" is honest.
    "div": "button",
    "svg": "button",
    "circle": "button",
    "ellipse": "button",
    "rect": "button",
    "polygon": "button",
    "path": "button",
}


def _frame_host(frame) -> str:
    """Short frame identity for model-facing text ('hidden' when about:blank)."""
    try:
        url = frame.url or ""
    except Exception:
        return "unknown"
    if not url or url == "about:blank":
        return "blank"
    try:
        parts = urlsplit(url)
        return parts.hostname or (parts.scheme or "unknown")
    except Exception:
        return "unknown"


class BrowserManager:
    """Synchronous Playwright wrapper with persistent profile."""

    def __init__(self, headless: bool = False, profile_dir: str = "cache/browser_profile",
                 timeout_ms: int = 30000, block_ads: bool = True,
                 easylist_path: str | None = None, allowed_domains=None,
                 network_access: str = DEFAULT_NETWORK_ACCESS,
                 log_dir: str | None = None, screenshot_dir: str = "screenshots",
                 downloads_dir: str | None = None):
        self.headless = headless
        self.profile_dir = profile_dir
        self.timeout_ms = timeout_ms
        self.block_ads = block_ads
        self._easylist_path = easylist_path
        # Console/network capture: memory rings always, a file when log_dir is
        # set (<project>/.logos/browser_logs). See browser/logs.py.
        self._log_dir = log_dir or ""
        # Where browser_screenshot saves its PNGs (<project>/.logos/screenshots
        # when a project is open; the owner decides, see BrowserService).
        self.screenshot_dir = screenshot_dir or "screenshots"
        # Downloads are staged beside the screenshots, so a file the page
        # offered survives the click and can be read/saved later.
        self.downloads_dir = downloads_dir or ""
        self._downloads = deque(maxlen=100)
        self._download_seq = 0
        self._pending_downloads = deque(maxlen=100)
        self._pending_bodies = deque(maxlen=100)
        self.logs = BrowserEventLog()
        self._diag_pages = set()  # id(page) with console/network listeners
        self.allowed_domains = list(allowed_domains or [])
        self.network_access = normalize_network_access(network_access)
        self._cancel_check = None
        self._ad_engine_obj = None  # lazy EasyListEngine (built on first use)
        self._pw = None
        self._browser = None
        self._page = None
        self._refs = {}  # ref -> playwright Locator
        self._last_snapshot = []  # last snapshot elements (for browser_find)
        self._last_screenshot_path = None  # set by capture, consumed once by next LLM request
        self._pending_dialog = None  # (accept, prompt_text) armed by browser_handle_dialog
        self._pending_tabs_note = None  # new-tab(s) notice, consumed once by click result
        self._last_dialog = None  # last auto-dismissed dialog note
        self._last_dialog_result = None  # last armed-handling outcome note
        self._dialog_pages = set()  # id(page) with the dialog router installed
        # CDP Fetch guard: Chromium/Playwright context.route does NOT see
        # server redirects (verified: a 302 to 127.0.0.1 reached the target).
        # Fetch.requestPaused sees the redirect request itself, so private/
        # metadata targets are aborted before any connection is made.
        self._guard_pages = set()  # id(page) with CDP Fetch enabled
        self._guard_sessions = {}  # id(page) -> CDPSession
        self.last_known_url = ""  # plain-string URL cache, safe to read from any thread
        # A fresh browser pays for its cold start inside the first navigation;
        # that one gets a wider budget and a single retry (see goto).
        self._first_navigation = True

    # -- cancellation ---------------------------------------------------

    def set_cancel_check(self, check) -> None:
        """Installs a callable returning True when the current tool is stale.

        Playwright's sync calls cannot be interrupted safely from another
        thread, so long operations (waits, readiness polling, navigation
        follow-ups) poll this between short slices instead.
        """
        self._cancel_check = check

    def _check_cancel(self) -> None:
        if self._cancel_check is None:
            return
        try:
            stale = bool(self._cancel_check())
        except Exception:
            stale = False
        if stale:
            raise BrowserCancelledError("Browser action cancelled.")

    def _sleep(self, seconds: float, slice_seconds: float = 0.1) -> None:
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            self._check_cancel()
            step = min(slice_seconds, remaining)
            time.sleep(step)
            remaining -= step

    def _wait_ready(self, page, timeout_seconds: float) -> None:
        """Waits for DOMContentLoaded-ish readiness without blocking cancellation."""
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            self._check_cancel()
            try:
                state = page.evaluate("() => document.readyState")
            except Exception:
                state = ""
            if state in ("interactive", "complete"):
                return
            self._sleep(0.1)
        logger.debug("Page readiness wait timed out; continuing.")

    def _validate(self, url: str) -> str:
        return validate_url(
            url,
            allowed_domains=self.allowed_domains,
            network_access=self.network_access,
        )

    # -- lifecycle ------------------------------------------------------

    def launch(self) -> None:
        from .runtime import is_chromium_installed, pin_environment
        # Pin the browser folder before the Playwright driver starts, otherwise
        # it looks inside the (temporary, per-launch) bundled driver package.
        pin_environment()
        if not is_chromium_installed():
            raise RuntimeError(
                "The browser engine (Chromium) is not installed yet. Open "
                "Settings → Browser → Download… (about 320 MB) and retry.")
        from playwright.sync_api import sync_playwright
        import os
        os.makedirs(self.profile_dir, exist_ok=True)
        self.logs.start(self._log_dir)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch_persistent_context(
            user_data_dir=os.path.abspath(self.profile_dir),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        pages = self._browser.pages
        # Popups/new tabs get the CDP guard and diagnostics too, not only the
        # active page.
        try:
            self._browser.on("page", self._attach_page)
        except Exception as e:
            logger.warning(f"Could not hook browser page events for URL guard: {e}")
        self._use_page(pages[0] if pages else self._browser.new_page())
        self._apply_ad_routing()
        logger.info(f"Browser launched (headless={self.headless}, block_ads={self.block_ads}).")

    def _ad_engine(self):
        """Lazily builds the EasyList engine (cached file, background refresh)."""
        if self._ad_engine_obj is None:
            from .adblock import EasyListEngine, DEFAULT_CACHE_FILE
            self._ad_engine_obj = EasyListEngine(
                cache_path=self._easylist_path or DEFAULT_CACHE_FILE)
            self._ad_engine_obj.ensure_loaded()
        return self._ad_engine_obj

    def set_block_ads(self, enabled: bool) -> None:
        """Toggles ad-request blocking live (no browser restart needed)."""
        self.block_ads = bool(enabled)
        if self._browser is not None:
            self._apply_ad_routing()
        logger.info(f"Browser ad blocking {'enabled' if self.block_ads else 'disabled'}.")

    def _apply_ad_routing(self) -> None:
        """Routes all requests through URL policy (and EasyList when enabled).

        Routing is always installed: redirects and subresources must not be
        able to reach loopback/private hosts just because ad blocking is off.
        """
        try:
            self._browser.unroute_all(behavior="wait")
        except Exception:
            pass
        try:
            self._browser.route("**/*", self._route_handler)
            logger.info("Browser request routing installed (policy%s).",
                        " + adblock" if self.block_ads else "")
        except Exception as e:
            logger.warning(f"Could not install request routing: {e}")

    def _install_navigation_guard(self, page) -> None:
        """Blocks policy-forbidden URLs at CDP Fetch level.

        Required for redirects: Playwright's context.route() never fires for
        the redirect target (only page.on("request") sees it, and that event
        cannot abort). Fetch.requestPaused fires before the request leaves
        the browser, including redirect hops, so 302 -> localhost/private IP
        is stopped before any connection is attempted.
        """
        if page is None or self._browser is None or id(page) in self._guard_pages:
            return
        try:
            cdp = self._browser.new_cdp_session(page)
            cdp.send("Fetch.enable", {"patterns": [
                {"urlPattern": "http://*", "requestStage": "Request"},
                {"urlPattern": "https://*", "requestStage": "Request"},
            ]})

            def _paused(event):
                try:
                    request_id = event.get("requestId")
                    url = (event.get("request") or {}).get("url") or ""
                    try:
                        self._validate(url)
                    except Exception as e:
                        logger.warning(
                            "Blocked browser request by URL policy (CDP): %s (%s)",
                            redact_url(url)[:160], e)
                        try:
                            cdp.send("Fetch.failRequest", {
                                "requestId": request_id,
                                "errorReason": "BlockedByClient",
                            })
                        except Exception:
                            pass
                        return
                    try:
                        cdp.send("Fetch.continueRequest",
                                 {"requestId": request_id})
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"CDP request guard failed: {e}")
                    try:
                        cdp.send("Fetch.continueRequest",
                                 {"requestId": event.get("requestId")})
                    except Exception:
                        pass

            cdp.on("Fetch.requestPaused", _paused)
            self._guard_pages.add(id(page))
            self._guard_sessions[id(page)] = cdp

            def _forget_page():
                self._guard_pages.discard(id(page))
                self._guard_sessions.pop(id(page), None)
                self._dialog_pages.discard(id(page))

            try:
                page.on("close", _forget_page)
            except Exception:
                pass
            logger.info("Browser CDP URL guard installed.")
        except Exception as e:
            logger.warning(f"Could not install CDP URL guard: {e}")

    def _route_handler(self, route) -> None:
        request = None
        try:
            request = route.request
            url = request.url or ""
            # Defense in depth: CDP guard handles redirects (route never sees
            # them), while this route still validates every request it does
            # see. Both layers fail closed.
            try:
                if (urlsplit(url).scheme or "").lower() in ("http", "https"):
                    self._validate(url)
            except Exception as e:
                logger.warning("Blocked browser request by URL policy: %s (%s)",
                               redact_url(url)[:160], e)
                try:
                    route.abort()
                except Exception:
                    pass
                return
            if self.block_ads:
                frame_url = ""
                try:
                    frame_url = (request.frame.url or "") if request.frame else ""
                except Exception:
                    pass
                if self._ad_engine().should_block(
                        url, request.resource_type, frame_url):
                    logger.debug(f"Blocked ad request: {redact_url(url)[:160]}")
                    try:
                        route.abort()
                    except Exception:
                        pass
                    return
        except Exception as e:
            logger.warning(f"Request route handler failed: {e}")
        try:
            route.continue_()
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = self._page = self._pw = None
        self._refs = {}
        self._last_snapshot = []
        self._last_screenshot_path = None
        self._pending_dialog = None
        self._pending_tabs_note = None
        self._last_dialog = None
        self._last_dialog_result = None
        self._dialog_pages = set()
        self._guard_pages = set()
        self._guard_sessions = {}
        self._diag_pages = set()
        self._downloads.clear()
        self._download_seq = 0
        self._pending_downloads.clear()
        self._pending_bodies.clear()
        self.logs.close()
        self.last_known_url = ""

    def _require_page(self):
        """Active page, launching (or relaunching after a manual close) as needed.

        Every tool funnels through this, so a browser the user closed by hand is
        transparently brought back here instead of failing on the first action
        that happens not to call ``ensure_launched`` (e.g. browser_read/title).
        """
        self.ensure_launched()
        if self._page is None:
            raise RuntimeError("Browser is not launched. Call launch() first.")
        return self._page

    def ensure_launched(self) -> None:
        """Launch on first use and recover if the browser was closed by hand."""
        if self._page is None:
            self.launch()
            return
        try:
            if not self._page.is_closed():
                return
        except Exception:
            pass
        logger.warning("Browser page/context was closed; relaunching a fresh one.")
        self.close()
        self.launch()

    def _use_page(self, page) -> None:
        """Makes page the active one: timeouts, dialog router, refs expire."""
        self._page = page
        try:
            self.last_known_url = redact_url(page.url or "")
        except Exception:
            pass
        self._page.set_default_timeout(self.timeout_ms)
        self._install_dialog_router(page)
        self._install_navigation_guard(page)
        self._attach_diagnostics(page)
        self._refs = {}
        self._last_snapshot = []
        self._last_screenshot_path = None  # stale image is worse than none

    def _attach_page(self, page) -> None:
        """Applies the per-page hooks a popup needs as well as the main page."""
        self._install_navigation_guard(page)
        self._attach_diagnostics(page)

    def _attach_diagnostics(self, page) -> None:
        """Records console messages and network results (see browser/logs.py).

        Listeners are passive: they never drive the page, never raise into
        Playwright's event loop and only append to bounded buffers.
        """
        if page is None or id(page) in self._diag_pages:
            return
        self._diag_pages.add(id(page))
        logs = self.logs

        def _console(message):
            try:
                location = message.location or {}
            except Exception:
                location = {}
            where = ""
            try:
                url = redact_url(str((location or {}).get("url") or ""))
                line = (location or {}).get("lineNumber")
                if url:
                    where = (f" ({url}:{int(line) + 1})"
                             if isinstance(line, int) and line >= 0
                             else f" ({url})")
            except Exception:
                where = ""
            try:
                logs.console(message.type, message.text, where)
            except Exception:
                pass

        def _page_error(error):
            try:
                logs.console("error", str(error), kind="pageerror")
            except Exception:
                pass

        def _response(response):
            try:
                request = response.request
                duration = None
                try:
                    timing = request.timing or {}
                    start = timing.get("startTime")
                    end = timing.get("responseEnd")
                    if start is not None and end is not None and end >= start:
                        duration = end - start
                except Exception:
                    duration = None
                resource_type = request.resource_type or ""
                entry = logs.network(request.method, redact_url(request.url or ""),
                                     response.status, resource_type,
                                     duration)
                # A 200 with an empty screen is usually a fetch that returned
                # nothing useful. Queue the body read for pump_events, because
                # reading it here would block Playwright's event dispatch.
                if (resource_type in ("xhr", "fetch")
                        and (response.status or 0) < 400):
                    self._pending_bodies.append((entry.get("id"), response))
            except Exception:
                pass

        def _download(download):
            # The event fires after the headers, but the bytes are only
            # available while the page is alive: keep the object and save it
            # in pump_events, which every browser tool calls before reading.
            try:
                self._pending_downloads.append(download)
            except Exception:
                pass

        def _request_failed(request):
            try:
                try:
                    failure = request.failure or ""
                except Exception:
                    failure = ""
                logs.network(request.method, redact_url(request.url or ""),
                             None, request.resource_type or "", None,
                             error=str(failure))
            except Exception:
                pass

        def _navigated(frame):
            try:
                if frame is not page.main_frame:
                    return
                logs.navigation(redact_url(frame.url or ""))
            except Exception:
                pass

        for event, handler in (
                ("console", _console), ("pageerror", _page_error),
                ("response", _response), ("requestfailed", _request_failed),
                ("download", _download), ("framenavigated", _navigated)):
            try:
                page.on(event, handler)
            except Exception as e:
                logger.warning(f"Could not hook page {event} events: {e}")

        def _forget():
            self._diag_pages.discard(id(page))

        try:
            page.on("close", _forget)
        except Exception:
            pass
        logger.info("Browser console/network capture installed.")

    def pump_events(self, milliseconds: int = 50) -> None:
        """Flushes queued page events into the log rings.

        Playwright's sync API dispatches page events only while a protocol call
        is in flight, so ``browser_console``/``browser_network`` would otherwise
        report the state of the previous call. A tiny wait is enough: the queue
        already holds everything that happened in between. Response bodies and
        downloads are consumed here too, because reading them inside the event
        handler would block the dispatcher.
        """
        page = self._page
        if page is None:
            return
        try:
            page.wait_for_timeout(max(1, int(milliseconds)))
        except Exception as e:
            logger.debug(f"Event pump skipped: {e}")
        self._drain_downloads()
        self._drain_response_bodies()

    def _drain_downloads(self) -> None:
        while self._pending_downloads:
            try:
                download = self._pending_downloads.popleft()
            except IndexError:
                return
            self._save_download_object(download)

    def _save_download_object(self, download) -> None:
        """Saves one Playwright download into the staging folder (best effort)."""
        self._download_seq += 1
        entry = {
            "index": self._download_seq,
            "filename": "",
            "url": "",
            "path": "",
            "error": "",
            "at": time.time(),
        }
        try:
            entry["filename"] = os.path.basename(
                str(download.suggested_filename or "download.bin"))
        except Exception:
            entry["filename"] = "download.bin"
        try:
            entry["url"] = redact_url(str(download.url or ""))
        except Exception:
            entry["url"] = ""
        try:
            directory = self._download_dir()
            os.makedirs(directory, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target = os.path.join(directory, f"{stamp}_{entry['filename']}")
            stem, suffix = os.path.splitext(target)
            counter = 2
            while os.path.exists(target):
                target = f"{stem}_{counter}{suffix}"
                counter += 1
            download.save_as(target)
            entry["path"] = target
            logger.info(f"Browser download saved: {target}")
            self._prune_downloads()
        except Exception as e:
            entry["error"] = str(e)[:300]
            logger.warning(f"Could not save a browser download: {e}")
        self._downloads.append(entry)

    def _download_dir(self) -> str:
        """``<project>/.logos/downloads``, or a folder beside the screenshots."""
        if self.downloads_dir:
            return self.downloads_dir
        return os.path.join(
            os.path.dirname(os.path.abspath(self.screenshot_dir)), "downloads")

    def _prune_downloads(self, keep: int = 100) -> None:
        """Keeps the newest staged downloads so a long project stays bounded."""
        try:
            directory = self._download_dir()
            names = [name for name in os.listdir(directory)
                     if os.path.isfile(os.path.join(directory, name))]
            paths = [os.path.join(directory, name) for name in names]
            paths.sort(key=os.path.getmtime, reverse=True)
            for stale in paths[keep:]:
                try:
                    os.remove(stale)
                except OSError:
                    pass
        except OSError as error:
            logger.debug(f"Could not prune downloads: {error}")

    def _drain_response_bodies(self) -> None:
        while self._pending_bodies:
            try:
                entry_id, response = self._pending_bodies.popleft()
            except IndexError:
                return
            self._read_response_body(entry_id, response)

    def _read_response_body(self, entry_id, response) -> None:
        """Reads one XHR/fetch body, bounded, and never raises into the pump."""
        try:
            headers = response.headers or {}
            content_type = headers.get("content-type", "")
            try:
                declared = int(headers.get("content-length") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > BODY_READ_LIMIT:
                self.logs.attach_body(
                    entry_id, "", content_type,
                    error=f"body skipped: {declared} bytes declared")
                return
            raw = response.body()
            if raw is None:
                self.logs.attach_body(entry_id, "", content_type,
                                      error="body was empty")
                return
            text = raw.decode("utf-8", errors="replace")
            self.logs.attach_body(
                entry_id, text, content_type,
                truncated=len(text) > BODY_MAX_CHARS)
        except Exception as e:
            try:
                self.logs.attach_body(entry_id, "", "", error=str(e)[:300])
            except Exception:
                pass

    def _install_dialog_router(self, page) -> None:
        """Records JS dialogs; auto-dismiss preserves default Playwright behavior."""
        try:
            if id(page) in self._dialog_pages:
                return
            manager = self

            def _router(dialog):
                try:
                    pending, manager._pending_dialog = manager._pending_dialog, None
                    if pending is not None:
                        accept, prompt_text = pending
                        if dialog.type == "prompt" and prompt_text is not None:
                            dialog.accept(prompt_text)
                        elif accept:
                            dialog.accept()
                        else:
                            dialog.dismiss()
                        manager._last_dialog_result = (
                            f"Handled {dialog.type} dialog: {dialog.message[:300]}"
                        )
                    else:
                        manager._last_dialog = (
                            f"Auto-dismissed {dialog.type} dialog: {dialog.message[:300]}"
                        )
                        dialog.dismiss()
                except Exception as e:
                    logger.warning(f"Dialog router failed: {e}")

            page.on("dialog", _router)
            self._dialog_pages.add(id(page))
        except Exception as e:
            logger.warning(f"Could not install dialog router: {e}")

    def consume_dialog_note(self) -> str:
        """Returns and clears pending dialog notes (called after page actions)."""
        notes = []
        if self._last_dialog_result:
            notes.append(self._last_dialog_result)
            self._last_dialog_result = None
        if self._last_dialog:
            notes.append(self._last_dialog)
            self._last_dialog = None
        if self._pending_tabs_note:
            notes.append(self._pending_tabs_note)
            self._pending_tabs_note = None
        return " ".join(notes)

    # -- navigation -----------------------------------------------------

    def goto(self, url: str, wait: str = "domcontentloaded") -> None:
        self._check_cancel()
        url = self._validate(url)
        self.ensure_launched()
        page = self._require_page()
        logger.info("Browser goto: %s", redact_url(url))
        self._refs = {}  # refs expire on every navigation
        self._last_snapshot = []
        self._last_screenshot_path = None
        # The first navigation of a fresh browser carries the cold start
        # (process, profile, first request) on top of the page load, so it
        # used to die at the 30 s limit and work when repeated. Give it twice
        # the budget and one retry instead of reporting a failure that is
        # only a slow start.
        first = self._first_navigation
        timeout = self.timeout_ms * 2 if first else self.timeout_ms
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
        except PlaywrightTimeoutError:
            if not first:
                raise
            logger.warning("First browser navigation timed out; retrying once.")
            page.goto(url, wait_until="commit", timeout=self.timeout_ms)
        self._first_navigation = False
        self._check_cancel()
        if wait == "load":
            self._wait_ready(page, self.timeout_ms / 1000)
        elif wait == "networkidle":
            try:
                page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
            except Exception:
                pass
        else:
            self._wait_ready(page, self.timeout_ms / 1000)
        try:
            self.last_known_url = redact_url(page.url or url)
        except Exception:
            self.last_known_url = redact_url(url)

    def title(self) -> str:
        return self._require_page().title()

    def url(self) -> str:
        return self._require_page().url

    def read(self, max_chars: int = 3000) -> str:
        """Page title, URL and visible text (for answering 'what is here')."""
        self._check_cancel()
        page = self._require_page()
        text = self._visible_text(page)
        if not text:
            try:
                text = page.locator("body").inner_text(timeout=5000)
            except Exception:
                text = ""
        text = " ".join(text.split())
        head = (f"Title: {page.title()}\nURL: {redact_url(page.url)}"
                f"\nText: {text[:max_chars]}")
        parts = [head]
        # Cross-origin frames never appear in the top document text, yet
        # they often hold exactly the control the task is about (widgets,
        # captchas, checkout forms). Read them separately instead of
        # claiming the page is empty.
        budget = max_chars
        for index, frame in self._frames()[1:]:
            if budget <= 0:
                break
            try:
                body = frame.evaluate("document.body ? document.body.innerText : ''") or ""
            except Exception:
                continue
            body = " ".join(str(body).split())
            if not body:
                continue
            host = _frame_host(frame)
            chunk = body[:budget]
            budget -= len(chunk)
            parts.append(f"Frame {index} ({host}): {chunk}")
        return "\n".join(parts)

    @staticmethod
    def _visible_text(page) -> str:
        """Text from rendered, non-hidden nodes (skips CSS-hidden/off-canvas).

        inner_text already ignores display:none, but not visibility:hidden,
        opacity:0, aria-hidden or elements parked far off-screen — which is
        exactly where prompt-injection payloads like to hide.
        """
        try:
            return page.evaluate("""() => {
                const out = [];
                const walker = document.createTreeWalker(
                    document.body || document.documentElement,
                    NodeFilter.SHOW_TEXT);
                const hidden = (el) => {
                    for (let p = el, hops = 0; p && hops < 6; p = p.parentElement, hops++) {
                        const st = getComputedStyle(p);
                        if (st.display === 'none' || st.visibility === 'hidden' ||
                            st.visibility === 'collapse' || Number(st.opacity) === 0 ||
                            p.getAttribute('aria-hidden') === 'true') return true;
                        const r = p.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return true;
                        if (r.right < -100 || r.left > 20000 || r.bottom < -100 ||
                            r.top > 100000) return true;
                    }
                    return false;
                };
                let node;
                while ((node = walker.nextNode())) {
                    const t = (node.nodeValue || '').trim();
                    if (!t) continue;
                    const el = node.parentElement;
                    if (!el || hidden(el)) continue;
                    out.push(t);
                }
                return out.join(' ');
            }""") or ""
        except Exception:
            return ""

    # -- grounding (ref snapshot) ----------------------------------------

    def _frames(self) -> list:
        """[(index, frame)] with the main frame at index 0."""
        try:
            frames = list(self._require_page().frames)
        except Exception:
            return []
        return list(enumerate(frames))

    def frames(self) -> list:
        """Frame inventory: [{index, url, name, main}] (main frame = index 0)."""
        out = []
        for index, frame in self._frames():
            try:
                url, name = redact_url(frame.url), (frame.name or "")
            except Exception:
                url, name = "", ""
            out.append({"index": index, "url": url, "name": name, "main": index == 0})
        return out

    def eval_js(self, script: str, frame: int = 0, ref: int | None = None) -> str:
        """Runs one JavaScript expression (or function) and returns its value.

        Needed where the DOM grounding cannot help: reading state a page
        keeps in JS, computing a layout point, or driving a widget that has
        no clickable element at all. With ``ref`` the script runs against
        that one element (``el => el.value``), which removes the need to
        guess a CSS selector for it.
        """
        self._check_cancel()
        script = (script or "").strip()
        if not script:
            raise ValueError("browser_eval needs a JavaScript expression in 'script'.")
        frames = self._frames()
        try:
            index = int(frame or 0)
        except (TypeError, ValueError):
            index = 0
        if not frames:
            raise ValueError("No page is open. Use browser_goto first.")
        if not (0 <= index < len(frames)):
            raise ValueError(f"Frame {index} does not exist; this page has "
                             f"{len(frames)} frame(s) (0 = main).")
        if ref is not None and str(ref).strip() != "":
            target = self._ref(ref)
        else:
            target = frames[index][1]
        try:
            result = target.evaluate(script)
        except Exception as e:
            raise ValueError(f"JavaScript failed: {str(e).splitlines()[0][:300]}") from e
        if isinstance(result, str):
            text = result
        else:
            try:
                text = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(result)
        text = text if text is not None else "undefined"
        limit = 6000
        if len(text) > limit:
            text = text[:limit] + f"... [{len(text) - limit} more characters]"
        return text

    def snapshot(self, max_elements: int = 200) -> list:
        """Visible interactive elements as [{ref, role, name}]. Rebuilds refs.

        Every frame is walked, so controls inside cross-origin iframes get
        refs like any other element; the entry carries its frame index.
        """
        self._check_cancel()
        self._require_page()
        elements = []
        self._refs = {}
        for index, frame in self._frames():
            budget = max_elements - len(elements)
            if budget <= 0:
                break
            try:
                locators = frame.locator(_INTERACTIVE_SELECTOR)
                count = min(locators.count(), budget)
            except Exception:
                continue
            for i in range(count):
                try:
                    loc = locators.nth(i)
                    if not loc.is_visible():
                        continue
                    tag = (loc.evaluate("e => e.tagName.toLowerCase()") or "").strip()
                    explicit_role = (loc.get_attribute("role") or "").strip()
                    input_type = (loc.get_attribute("type") or "").strip().lower() if tag == "input" else ""
                    if tag == "input" and input_type in ("radio", "checkbox"):
                        # Real control type beats the generic "textbox": a bare
                        # "textbox: 1" hid answer radios from the model entirely.
                        role = explicit_role or input_type
                    elif tag == "input" and input_type in ("submit", "button", "image"):
                        role = explicit_role or "button"
                    else:
                        role = explicit_role or _TAG_TO_ROLE.get(tag, "") or tag or "element"
                    label_text = ""
                    if tag in ("input", "select", "textarea"):
                        label_text = self._labeled_text(loc)
                    name = (loc.get_attribute("aria-label")
                            or label_text
                            or loc.get_attribute("placeholder")
                            or loc.get_attribute("value")
                            or loc.get_attribute("alt")
                            or self._element_text(loc, tag)
                            or "").strip()[:120]
                    ref = len(elements) + 1
                    self._refs[ref] = loc
                    entry = {
                        "ref": ref, "role": role, "name": name,
                        "type": input_type,
                        "sensitive": bool(
                            input_type == "password"
                            or is_sensitive_text(name, explicit_role)),
                    }
                    checkable = (
                        (tag == "input" and input_type in ("checkbox", "radio"))
                        or explicit_role in ("checkbox", "radio", "switch",
                                             "menuitemcheckbox", "menuitemradio"))
                    if checkable:
                        # A snapshot that omits the state makes two identical
                        # "checkbox 1" rows indistinguishable, so the model
                        # cannot tell which one to uncheck.
                        entry["checked"] = self._checked_state(loc)
                    if index:
                        entry["frame"] = index
                        entry["frame_host"] = _frame_host(frame)
                    elements.append(entry)
                except Exception:
                    continue
        logger.info(f"Browser snapshot: {len(elements)} interactive elements "
                    f"across {len(self._frames())} frame(s).")
        self._last_snapshot = elements
        return elements

    @staticmethod
    def snapshot_line(element: dict, include_frame: bool = True) -> str:
        """One rendered snapshot row (shared by browser_snapshot and find).

        The checked state is part of the row, not a separate field, because
        the model reads this text: a list of "checkbox: 1" rows with no state
        cannot be acted on.
        """
        where = ""
        if include_frame and element.get("frame"):
            where = (f"(frame {element['frame']}: "
                     f"{element.get('frame_host', '?')}) ")
        line = (f"[{element['ref']}] {where}{element['role']}: "
                f"{element['name'] or '(no label)'}")
        state = element.get("checked")
        if state is True:
            line += " (checked)"
        elif state is False:
            line += " (unchecked)"
        return line

    def heading_context(self, max_headings: int = 3, max_chars: int = 120) -> str:
        """Visible h1/h2 texts: screen identity for quiz/wizard-style pages.

        Snapshots list controls only, so without this the model cannot tell
        which screen it is on (all quiz questions look alike as bare refs).
        """
        try:
            page = self._require_page()
            heads = page.locator("h1, h2")
            out = []
            for i in range(min(heads.count(), max_headings)):
                try:
                    if not heads.nth(i).is_visible():
                        continue
                    text = (heads.nth(i).inner_text(timeout=500) or "").strip()
                    if text:
                        out.append(" ".join(text.split())[:max_chars])
                except Exception:
                    continue
            return " | ".join(out)
        except Exception:
            return ""

    @staticmethod
    def _element_text(loc, tag: str) -> str:
        """First-line text for label-bearing tags, '' otherwise.

        Uses text_content (never throws on SVG nodes, unlike inner_text)
        and never lets a label failure drop the element's ref.
        """
        if tag not in ("a", "button", "div", "svg", "circle", "ellipse",
                       "rect", "polygon", "path"):
            return ""
        try:
            text = loc.text_content(timeout=500) or ""
        except Exception:
            return ""
        return text.strip().split("\n")[0]

    def find_in_snapshot(self, text: str | None = None, pattern: str | None = None,
                         context_lines: int = 2) -> str:
        """Cheap substring/regex search over the latest snapshot (with ref context)."""
        import re
        if not self._last_snapshot:
            raise ValueError("No snapshot taken yet. Call browser_snapshot first.")
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                raise ValueError(f"browser_find: invalid regex '{pattern}': {e}") from e

            def matches(s: str) -> bool:
                return bool(rx.search(s))

            query = pattern
        elif text:
            needle = text.lower()

            def matches(s: str) -> bool:
                return needle in s.lower()

            query = text
        else:
            raise ValueError("browser_find needs one of: 'text', 'pattern'.")
        lines = [self.snapshot_line(e, include_frame=False) for e in self._last_snapshot]
        hits = [i for i, line in enumerate(lines) if matches(line)]
        if not hits:
            return f"No matches for '{query}' in the latest snapshot ({len(lines)} elements)."
        out, shown = [], set()
        for i in hits:
            for j in range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)):
                if j not in shown:
                    shown.add(j)
                    out.append((">> " if j == i else "   ") + lines[j])
        return f"Matches for '{query}':\n" + "\n".join(out)

    @staticmethod
    def _labeled_text(loc) -> str:
        """Visible label for a form control: label[for], wrapping <label>,
        else the text right after the control, else short parent text.

        A bare control is often written as ``<input> option A`` with no
        label element, and several such controls can share one parent. Taking
        the parent's first line then named every checkbox after the first one,
        so the sibling text wins and the parent is used only when it holds a
        single control. Empty string when nothing suitable."""
        try:
            text = loc.evaluate("""e => {
                const t = el => ((el && el.innerText) || '').trim();
                const firstLine = el => t(el).split('\\n')[0].trim();
                if (e.id) {
                    const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
                    const s = t(l);
                    if (s) return s;
                }
                let p = e.parentElement, hops = 0;
                while (p && hops < 3) {
                    if (p.tagName.toLowerCase() === 'label') {
                        const s = t(p);
                        if (s) return s;
                    }
                    p = p.parentElement; hops++;
                }
                // Text that follows the control on the same line.
                let node = e.nextSibling;
                while (node) {
                    if (node.nodeType === 3) {
                        const s = (node.textContent || '').trim();
                        if (s) return s;
                    } else if (node.nodeType === 1) {
                        const s = firstLine(node);
                        if (s) return s;
                    }
                    node = node.nextSibling;
                }
                const parent = e.parentElement;
                if (!parent) return '';
                // One control per parent, or the label belongs to a sibling.
                if (parent.querySelectorAll('input, select, textarea').length > 1) return '';
                const direct = firstLine(parent);
                return direct.length <= 60 ? direct : '';
            }""") or ""
            return text.strip()
        except Exception:
            return ""

    def describe_ref(self, ref) -> dict:
        """Snapshot metadata for a ref (plain data, safe off the page)."""
        try:
            wanted = int(ref)
        except (TypeError, ValueError):
            return {}
        for element in self._last_snapshot:
            if element.get("ref") == wanted:
                return dict(element)
        return {}

    def _ref(self, ref: int):
        try:
            return self._refs[int(ref)]
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(
                f"Unknown element ref '{ref}'. Take a fresh snapshot first: "
                f"refs expire after every navigation."
            ) from e

    # -- actions ----------------------------------------------------------

    def click(self, ref: int) -> None:
        self._check_cancel()
        loc = self._ref(ref)
        logger.info(f"Browser click ref={ref}")
        try:
            opener_url = self._page.url if self._page else ""
        except Exception:
            opener_url = ""
        before = {id(p) for p in self._browser.pages}
        loc.click(timeout=self.timeout_ms)
        self._after_click(opener_url, before)

    def viewport_size(self) -> tuple[int, int]:
        """Visible page size in CSS pixels (0, 0 when unknown)."""
        try:
            size = self._require_page().viewport_size or {}
            return int(size.get("width") or 0), int(size.get("height") or 0)
        except Exception:
            return 0, 0

    def click_at(self, x, y, button: str = "left", clicks: int = 1) -> tuple[int, int]:
        """Clicks a point of the visible page (CSS pixels, top-left origin).

        Needed where a ref cannot exist: canvas/image maps, custom widgets
        that only react to raw mouse events, and controls inside a
        cross-origin frame (such content never appears in a snapshot).
        Returns the viewport size the point was validated against.
        """
        self._check_cancel()
        page = self._require_page()
        try:
            px, py = float(x), float(y)
        except (TypeError, ValueError):
            raise ValueError(
                f"browser_click needs numeric coordinates, got x={x!r}, y={y!r}.") from None
        width, height = self.viewport_size()
        if width and height and not (0 <= px < width and 0 <= py < height):
            raise ValueError(
                f"Point ({px:g}, {py:g}) is outside the visible page: the viewport is "
                f"{width}x{height} CSS pixels, so x must be 0-{width - 1} and y 0-{height - 1}. "
                f"Scroll to the target first, or click it by ref.")
        button = str(button or "left").strip().lower()
        if button not in {"left", "right", "middle"}:
            raise ValueError(f"Unsupported mouse button '{button}'. Use left, right or middle.")
        try:
            count = max(1, min(int(clicks or 1), 3))
        except (TypeError, ValueError):
            count = 1
        logger.info(f"Browser click at ({px:g}, {py:g}) button={button} clicks={count}")
        try:
            opener_url = page.url
        except Exception:
            opener_url = ""
        before = {id(p) for p in self._browser.pages}
        page.mouse.click(px, py, button=button, click_count=count)
        self._after_click(opener_url, before)
        return width, height

    def _after_click(self, opener_url: str, before: set) -> None:
        try:
            after_url = self._page.url if self._page else ""
        except Exception:
            after_url = ""
        if after_url != opener_url:
            # Real navigation: positional refs may point elsewhere now.
            # Same-page JS clicks keep refs (wiping them broke consecutive
            # clicks: every second click died with "unknown ref").
            self._refs = {}
            self._last_snapshot = []
            self._last_screenshot_path = None
            self.last_known_url = redact_url(after_url)
        new_pages = [p for p in self._browser.pages if id(p) not in before]
        if new_pages:
            marks = []
            for p in new_pages:
                try:
                    url, title = p.url, (p.title() or "")
                except Exception:
                    url, title = "", ""
                marks.append(self._tab_mark(url, title, opener_url))
            self._pending_tabs_note = f"New tab(s) opened by this click: {'; '.join(marks)}"

    def _looks_like_ad(self, url: str, title: str = "", opener_url: str = "") -> bool:
        """EasyList $popup match (opener-aware), else offline host-hint fallback."""
        try:
            engine = self._ad_engine()
            if engine.rule_count > 0:
                if engine.matches_popup(url, opener_url):
                    return True
        except Exception as e:
            logger.warning(f"Ad-popup check failed: {e}")
        try:
            from .adblock import EasyListEngine
            if EasyListEngine.fallback_ad_host(url, title):
                return True
        except Exception:
            pass
        return "advertisement" in (title or "").lower()

    def _tab_mark(self, url: str, title: str, opener_url: str = "") -> str:
        tag = " [AD?]" if self._looks_like_ad(url, title, opener_url) else ""
        return f"{redact_url(url)[:120]!r} {title[:60]!r}{tag}"

    def consume_tabs_note(self) -> str:
        """Returns and clears the new-tabs notice (called after actions)."""
        note, self._pending_tabs_note = self._pending_tabs_note, None
        return note

    def fill(self, ref: int, text: str) -> None:
        self._check_cancel()
        loc = self._ref(ref)
        logger.info(f"Browser fill ref={ref} ({len(text)} chars)")
        loc.fill(text, timeout=self.timeout_ms)

    def select_option(self, ref: int, value: str | None = None,
                      label: str | None = None, index: int | None = None) -> str:
        """Selects an option in a <select> dropdown (never use fill/click on selects)."""
        self._check_cancel()
        loc = self._ref(ref)

        def present(candidate):
            if candidate is None:
                return False
            if isinstance(candidate, str) and not candidate.strip():
                return False
            return True

        kwargs = {}
        # Flat schemas can fill every branch with an empty string; a non-empty
        # value must win over a placeholder label/index and vice versa.
        if present(value):
            kwargs["value"] = value
        elif present(label):
            kwargs["label"] = label
        elif present(index):
            try:
                kwargs["index"] = int(index)
            except (TypeError, ValueError) as e:
                raise ValueError(f"browser_select_option: 'index' must be an integer, got '{index}'.") from e
        else:
            raise ValueError("browser_select_option needs one of: 'value', 'label', 'index'.")
        logger.info(f"Browser select_option ref={ref} {kwargs}")
        loc.select_option(timeout=self.timeout_ms, **kwargs)
        self._refs = {}  # a selection may submit/navigate: force a fresh snapshot
        self._last_snapshot = []
        self._last_screenshot_path = None
        return f"Selected option {kwargs} in element ref={ref}."

    def hover(self, ref: int) -> None:
        self._check_cancel()
        loc = self._ref(ref)
        logger.info(f"Browser hover ref={ref}")
        loc.hover(timeout=self.timeout_ms)

    def scroll(self, dx: int = 0, dy: int = 0, ref: int | None = None) -> str:
        """Wheels the page by dx/dy, or scrolls one element into view.

        A real wheel event (not a JS jump) is what virtualised lists and lazy
        image loaders react to, so the content a screenshot is supposed to
        show actually exists by the time it is taken.
        """
        self._check_cancel()
        page = self._require_page()
        if ref is not None and str(ref).strip() != "":
            loc = self._ref(ref)
            logger.info(f"Browser scroll ref={ref} into view")
            loc.scroll_into_view_if_needed(timeout=self.timeout_ms)
            self._last_screenshot_path = None
            return f"Scrolled element ref={ref} into view. {self._scroll_position(page)}"
        try:
            step_x, step_y = int(dx or 0), int(dy or 0)
        except (TypeError, ValueError):
            raise ValueError(
                f"browser_scroll needs integer dx/dy, got dx={dx!r}, dy={dy!r}.") from None
        if not step_x and not step_y:
            raise ValueError(
                "browser_scroll needs a non-zero dy/dx, or a ref to scroll into view.")
        width, height = self.viewport_size()
        if width and height:
            page.mouse.move(width / 2, height / 2)
        logger.info(f"Browser scroll dx={step_x} dy={step_y}")
        page.mouse.wheel(step_x, step_y)
        self._sleep(0.2)  # let scroll-driven loading settle before the next step
        self._last_screenshot_path = None
        return f"Scrolled by ({step_x}, {step_y}). {self._scroll_position(page)}"

    @staticmethod
    def _scroll_position(page) -> str:
        try:
            pos = page.evaluate(
                "() => ({x: Math.round(window.scrollX), y: Math.round(window.scrollY),"
                " h: Math.round(document.documentElement.scrollHeight),"
                " vh: Math.round(window.innerHeight)})")
        except Exception:
            return ""
        at_bottom = pos["y"] + pos["vh"] >= pos["h"] - 2
        return (f"Page position x={pos['x']} y={pos['y']} of {pos['h']} px"
                f"{' (bottom)' if at_bottom else ''}.")

    @staticmethod
    def _checked_state(loc):
        """Checked state of a control, or None when it has none."""
        try:
            return bool(loc.is_checked(timeout=1000))
        except Exception:
            pass
        try:
            aria = (loc.get_attribute("aria-checked") or "").strip().lower()
        except Exception:
            aria = ""
        if aria in ("true", "false"):
            return aria == "true"
        return None

    def set_checked(self, ref: int, checked: bool = True) -> str:
        """Checks or unchecks a checkbox/switch and verifies the final state.

        A plain click can toggle a custom control twice (label plus input), so
        the native check()/uncheck() is preferred and a click is only the
        fallback; the state is read back either way instead of assuming it.
        """
        self._check_cancel()
        loc = self._ref(ref)
        want = bool(checked)
        state = self._checked_state(loc)
        if state == want:
            return f"Element ref={ref} is already {'checked' if want else 'unchecked'}."
        page = self._require_page()
        try:
            opener_url = page.url
        except Exception:
            opener_url = ""
        before_pages = {id(p) for p in self._browser.pages}
        logger.info(f"Browser set_checked ref={ref} checked={want}")
        try:
            if want:
                loc.check(timeout=self.timeout_ms)
            else:
                loc.uncheck(timeout=self.timeout_ms)
        except Exception:
            # Not a native checkbox (div role=checkbox, wrapped label input):
            # click it and verify below instead of guessing.
            loc.click(timeout=self.timeout_ms)
        self._after_click(opener_url, before_pages)
        state = self._checked_state(loc)
        if state is None:
            return (f"Toggled element ref={ref}, but it exposes no checked state "
                    f"afterwards; verify the result with browser_snapshot or "
                    f"browser_read.")
        if state != want:
            raise ValueError(
                f"Element ref={ref} is still {'checked' if state else 'unchecked'} after "
                f"trying to {'check' if want else 'uncheck'} it: the control may not be "
                f"a checkbox or it rejects direct toggling. Take a fresh browser_snapshot.")
        return f"Element ref={ref} is now {'checked' if want else 'unchecked'}."

    def set_files(self, ref: int, paths) -> str:
        """Attaches local files to an <input type=file> (the only way to test uploads)."""
        self._check_cancel()
        loc = self._ref(ref)
        if isinstance(paths, str):
            paths = [paths]
        files = [str(p).strip() for p in (paths or []) if str(p or "").strip()]
        if not files:
            raise ValueError("browser_set_files needs at least one path.")
        missing = [p for p in files if not os.path.isfile(p)]
        if missing:
            shown = ", ".join(missing[:5])
            more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
            raise ValueError(f"These files do not exist: {shown}{more}")
        logger.info(f"Browser set_files ref={ref} files={len(files)}")
        loc.set_input_files(files, timeout=self.timeout_ms)
        names = ", ".join(os.path.basename(p) for p in files)
        return f"Attached {len(files)} file(s) to element ref={ref}: {names}."

    def downloads(self) -> list[dict]:
        """Downloads captured in this browser session (staged copies)."""
        self._drain_downloads()
        return [dict(item) for item in self._downloads]

    def save_download(self, index, destination) -> str:
        """Copies a staged download to a path the user (or the model) chose."""
        self._drain_downloads()
        try:
            number = int(index)
        except (TypeError, ValueError):
            raise ValueError(
                f"browser_downloads needs a numeric index, got {index!r}.") from None
        entry = next(
            (item for item in self._downloads if item.get("index") == number), None)
        if entry is None:
            available = ", ".join(str(item["index"]) for item in self._downloads) or "none"
            raise ValueError(
                f"Download #{number} does not exist. Available indexes: {available}.")
        source = entry.get("path") or ""
        if not source or not os.path.isfile(source):
            reason = entry.get("error") or "the file was not saved"
            raise ValueError(
                f"Download #{number} ({entry.get('filename') or 'file'}) cannot be "
                f"saved: {reason}.")
        target = str(destination or "").strip()
        if not target:
            raise ValueError("browser_downloads save needs a destination path.")
        if os.path.isdir(target) or target.endswith(("/", os.sep)):
            os.makedirs(target, exist_ok=True)
            target = os.path.join(target, entry.get("filename")
                                  or os.path.basename(source))
        else:
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        shutil.copy2(source, target)
        logger.info(f"Browser download #{number} copied to {target}")
        return (f"Saved download #{number} ({entry.get('filename') or 'file'}) "
                f"to {target}.")

    def drag(self, source_ref: int, target_ref: int) -> str:
        """Drags one snapshot element onto another (kanban, sorting, sliders)."""
        self._check_cancel()
        source = self._ref(source_ref)
        target = self._ref(target_ref)
        logger.info(f"Browser drag ref={source_ref} -> ref={target_ref}")
        source.drag_to(target, timeout=self.timeout_ms)
        self._last_screenshot_path = None
        return f"Dragged element ref={source_ref} onto ref={target_ref}."

    def navigate_back(self) -> str:
        self._check_cancel()
        page = self._require_page()
        logger.info("Browser navigate back")
        response = page.go_back(wait_until="commit")
        self._check_cancel()
        if response is None:
            return "There is no previous page in history. Take a fresh browser_snapshot to see where you are."
        self._wait_ready(page, self.timeout_ms / 1000)
        return self._after_history_navigation(page, "Went back")

    def navigate_forward(self) -> str:
        self._check_cancel()
        page = self._require_page()
        logger.info("Browser navigate forward")
        response = page.go_forward(wait_until="commit")
        self._check_cancel()
        if response is None:
            return "There is no next page in history. Take a fresh browser_snapshot to see where you are."
        self._wait_ready(page, self.timeout_ms / 1000)
        return self._after_history_navigation(page, "Went forward")

    def reload(self, wait: str = "domcontentloaded") -> str:
        """Reloads the current page, keeping its URL and history entry.

        ``goto`` cannot do this: re-entering a URL that came from a form POST
        loses the POST body (and the scroll position), while a reload repeats
        the request the page actually made.
        """
        self._check_cancel()
        page = self._require_page()
        logger.info("Browser reload: %s", redact_url(page.url or ""))
        page.reload(wait_until="commit")
        self._check_cancel()
        if wait == "load":
            self._wait_ready(page, self.timeout_ms / 1000)
        elif wait == "networkidle":
            try:
                page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
            except Exception:
                pass
        else:
            self._wait_ready(page, self.timeout_ms / 1000)
        return self._after_history_navigation(page, "Reloaded")

    def _after_history_navigation(self, page, verb: str) -> str:
        """Expires refs after back/forward/reload and names the new URL."""
        self._refs = {}
        self._last_snapshot = []
        self._last_screenshot_path = None
        try:
            current = redact_url(page.url or "")
            self.last_known_url = current
        except Exception:
            current = ""
        return f"{verb}. URL: {current}. Take a fresh browser_snapshot before acting."

    def wait_for(self, seconds: float | None = None, text: str | None = None,
                 text_gone: str | None = None) -> str:
        """Waits for time (max 30s), text to appear, or text to disappear."""
        self._check_cancel()
        page = self._require_page()

        def present(candidate):
            if candidate is None:
                return False
            return not (isinstance(candidate, str) and not candidate.strip())

        text = text if present(text) else None
        text_gone = text_gone if present(text_gone) else None
        # Flat strict schemas emit time=0 plus one text branch; the text
        # branch is the real intent, so it must win over the time default.
        if text or text_gone:
            pass
        elif seconds is not None:
            try:
                wait = max(0.0, min(float(seconds), 30.0))
            except (TypeError, ValueError) as e:
                raise ValueError(f"browser_wait_for: 'time' must be a number, got '{seconds}'.") from e
            logger.info(f"Browser wait {wait}s")
            self._sleep(wait)
            return f"Waited {wait}s."
        else:
            raise ValueError("browser_wait_for needs one of: 'time', 'text', 'text_gone'.")

        deadline = time.monotonic() + self.timeout_ms / 1000
        if text:
            logger.info("Browser wait for text: %r", text[:120])
            while time.monotonic() < deadline:
                self._check_cancel()
                try:
                    loc = page.get_by_text(text).first
                    if loc.is_visible():
                        return f"Text appeared: '{text}'."
                except Exception:
                    # Execution context can be destroyed by a navigation in
                    # flight; retry until the deadline instead of failing.
                    pass
                self._sleep(0.1)
            raise TimeoutError(f"Timed out waiting for text: '{text}'.")
        logger.info("Browser wait for text to disappear: %r", text_gone[:120])
        while time.monotonic() < deadline:
            self._check_cancel()
            try:
                loc = page.get_by_text(text_gone).first
                if not loc.is_visible():
                    return f"Text disappeared: '{text_gone}'."
            except Exception:
                return f"Text disappeared: '{text_gone}'."
            self._sleep(0.1)
        raise TimeoutError(f"Timed out waiting for text to disappear: '{text_gone}'.")

    def handle_dialog(self, accept: bool = True, prompt_text: str | None = None) -> str:
        """Arms policy for the next JS dialog; reports recently seen ones."""
        self.ensure_launched()
        recent = self._last_dialog_result or self._last_dialog or "none yet"
        self._last_dialog_result = None
        self._last_dialog = None
        self._pending_dialog = (bool(accept), prompt_text)
        what = "accept" if accept else "dismiss"
        extra = " with prompt text" if prompt_text is not None else ""
        logger.info(f"Browser dialog policy armed: {what}{extra}")
        return f"Dialog policy armed: will {what} the next dialog{extra}. Recent dialog: {recent}."

    def tabs(self, action: str = "list", index: int | None = None,
             url: str | None = None) -> str:
        """Lists, opens, selects, or closes browser tabs. The model manages tabs itself."""
        self._check_cancel()
        self.ensure_launched()
        action = (action or "list").lower()
        pages = list(self._browser.pages)
        if action == "list":
            if not pages:
                return "No open tabs."
            try:
                opener_url = self._page.url if self._page else ""
            except Exception:
                opener_url = ""
            lines = []
            for i, p in enumerate(pages):
                try:
                    purl, title = p.url, (p.title() or "")
                except Exception:
                    purl, title = "", ""
                ad_tag = " [AD?]" if self._looks_like_ad(purl, title, opener_url) else ""
                mark = " (current)" if p == self._page else ""
                lines.append(f"[{i}] {title} — {redact_url(purl)}{ad_tag}{mark}")
            return "Open tabs:\n" + "\n".join(lines)
        if action == "new":
            target_url = self._validate(url) if url else None
            page = self._browser.new_page()
            self._install_navigation_guard(page)
            if target_url:
                page.goto(target_url, wait_until="commit", timeout=self.timeout_ms)
                self._check_cancel()
                self._wait_ready(page, self.timeout_ms / 1000)
            self._use_page(page)
            return (f"Opened new tab. URL: {redact_url(self._page.url)}. "
                    f"Take a fresh browser_snapshot before acting.")
        if action == "select":
            if index is None:
                raise ValueError("browser_tabs select requires 'index'. Call action=list first.")
            try:
                page = pages[int(index)]
            except (IndexError, ValueError, TypeError) as e:
                raise ValueError(f"Unknown tab index '{index}'. Call action=list first.") from e
            self._use_page(page)
            return (f"Switched to tab [{int(index)}]. Title: '{page.title()}'. "
                    f"URL: {redact_url(page.url)}. Take a fresh browser_snapshot before acting.")
        if action == "close":
            if index is None:
                target = self._page
            else:
                try:
                    target = pages[int(index)]
                except (IndexError, ValueError, TypeError) as e:
                    raise ValueError(f"Unknown tab index '{index}'. Call action=list first.") from e
            target.close()
            remaining = list(self._browser.pages)
            if not remaining:
                self._use_page(self._browser.new_page())
                return "Closed the tab. No tabs left, opened a blank one."
            self._use_page(remaining[-1])
            return (f"Closed tab. Now on: {redact_url(self._page.url)}. "
                    f"Take a fresh browser_snapshot before acting.")
        raise ValueError(f"Unknown browser_tabs action '{action}'. Use list/new/select/close.")

    def press(self, key: str) -> None:
        self._check_cancel()
        page = self._require_page()
        logger.info(f"Browser press key={key}")
        try:
            before = page.url
        except Exception:
            before = ""
        page.keyboard.press(key)
        self._check_cancel()
        try:
            after = page.url
        except Exception:
            after = ""
        if after != before or (key or "").strip().lower() == "enter":
            # Enter can submit a form without changing the URL; refs and the
            # screenshot describe the pre-submit page and are now unsafe.
            self._refs = {}
            self._last_snapshot = []
            self._last_screenshot_path = None
            self.last_known_url = redact_url(after or before)
            if after != before:
                self._wait_ready(page, self.timeout_ms / 1000)

    def type_text(self, text: str) -> None:
        self._check_cancel()
        logger.info(f"Browser type ({len(text)} chars)")
        self._require_page().keyboard.type(text)

    def wait_for_text(self, text: str, timeout: int | None = None) -> None:
        self._require_page().get_by_text(text).first.wait_for(
            timeout=timeout or self.timeout_ms)

    def wait_for_load(self, state: str = "domcontentloaded") -> None:
        self._require_page().wait_for_load_state(state, timeout=self.timeout_ms)

    def _overlay_ref_badges(self) -> None:
        """Draws numbered badges over snapshot refs (removed after capture).

        Bridges vision to refs: the screenshot alone never says which circle
        is ref 2, and the snapshot text never says where ref 2 is. Badges do.
        """
        try:
            page = self._require_page()
            items = []
            for ref, loc in list(self._refs.items())[:50]:
                try:
                    box = loc.bounding_box(timeout=500)
                except Exception:
                    continue
                if not box:
                    continue
                items.append([ref, int(box["x"]), int(box["y"])])
            if not items:
                return
            page.evaluate(
                """(items) => {
                  const old = document.getElementById('__logos_ref_badges__');
                  if (old) old.remove();
                  const layer = document.createElement('div');
                  layer.id = '__logos_ref_badges__';
                  layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
                  for (const [ref, x, y] of items) {
                    const b = document.createElement('div');
                    b.textContent = String(ref);
                    b.style.cssText = 'position:absolute;left:' + Math.max(0, x) + 'px;top:' + Math.max(0, y) +
                      'px;background:#d00;color:#fff;font:bold 14px sans-serif;' +
                      'border-radius:50%;width:22px;height:22px;line-height:22px;text-align:center;';
                    layer.appendChild(b);
                  }
                  document.documentElement.appendChild(layer);
                }""",
                items,
            )
        except Exception as e:
            logger.debug(f"Ref badge overlay failed (non-fatal): {e}")

    def _remove_ref_badges(self) -> None:
        try:
            self._require_page().evaluate(
                """() => { const old = document.getElementById('__logos_ref_badges__');
                           if (old) old.remove(); }""")
        except Exception:
            pass

    def _selector_locator(self, selector: str):
        """The first element matching a CSS selector, or a readable error.

        Selectors are the semantic way to capture a chart, table or card that
        has no snapshot ref: refs exist only for interactive elements.
        """
        text = str(selector or "").strip()
        if not text:
            raise ValueError("browser_screenshot needs a non-empty selector.")
        page = self._require_page()
        try:
            locator = page.locator(text)
            count = locator.count()
        except Exception as e:
            raise ValueError(
                f"Invalid selector {text!r}: {e}") from None
        if not count:
            raise ValueError(
                f"No element matches selector {text!r}. Check the page HTML or "
                f"take a browser_snapshot first.")
        return locator.first, count

    def selector_count(self, selector: str) -> int:
        """How many elements match a selector (0 when invalid or none)."""
        try:
            return int(self._selector_locator(selector)[1])
        except Exception:
            return 0

    def screenshot(self, path: str, full_page: bool = False, ref: int | None = None,
                   clip: dict | None = None, selector: str | None = None) -> str:
        self._check_cancel()
        page = self._require_page()
        if ref is not None and str(ref).strip() != "":
            # One element: badges of other refs would only add noise.
            self._ref(ref).screenshot(path=path, timeout=self.timeout_ms)
            return path
        if selector is not None and str(selector).strip() != "":
            locator, _count = self._selector_locator(selector)
            locator.screenshot(path=path, timeout=self.timeout_ms)
            return path
        if clip:
            self._check_clip(clip)
            page.screenshot(path=path, clip=clip)
            return path
        if full_page:
            # Ref badges are fixed-position overlays; on a full-page capture
            # they land in the wrong place, so only a viewport shot gets them.
            page.screenshot(path=path, full_page=True)
            return path
        self._overlay_ref_badges()
        try:
            page.screenshot(path=path)
        finally:
            self._remove_ref_badges()
        return path

    @staticmethod
    def _check_clip(clip: dict) -> None:
        """Validates a clip rectangle before Playwright sees it."""
        if not isinstance(clip, dict):
            raise ValueError("browser_screenshot clip must be an object.")
        missing = [key for key in ("x", "y", "width", "height")
                   if clip.get(key) is None]
        if missing:
            raise ValueError(
                "browser_screenshot clip needs x, y, width and height; missing: "
                + ", ".join(missing) + ".")
        try:
            width = int(clip["width"])
            height = int(clip["height"])
            int(clip["x"])
            int(clip["y"])
        except (TypeError, ValueError):
            raise ValueError(
                "browser_screenshot clip values must be integers.") from None
        if width <= 0 or height <= 0:
            raise ValueError(
                "browser_screenshot clip width and height must be positive.")

    def capture_screenshot(self, prefix: str = "browser", full_page: bool = False,
                           ref: int | None = None, clip: dict | None = None,
                           selector: str | None = None) -> str:
        """Saves a screenshot, remembered once for the next LLM request."""
        self._check_cancel()
        from datetime import datetime
        self.ensure_launched()
        os.makedirs(self.screenshot_dir, exist_ok=True)
        path = os.path.join(
            self.screenshot_dir,
            f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png")
        self.screenshot(path, full_page=full_page, ref=ref, clip=clip,
                        selector=selector)
        self._prune_screenshots()
        self._last_screenshot_path = path
        logger.info(f"Browser screenshot saved: {path}")
        return path

    def _prune_screenshots(self, keep: int = 200) -> None:
        """Keeps the newest N shots so a long project never grows forever."""
        try:
            names = [name for name in os.listdir(self.screenshot_dir)
                     if name.lower().endswith(".png")]
            paths = [os.path.join(self.screenshot_dir, name) for name in names]
            paths.sort(key=os.path.getmtime, reverse=True)
            for stale in paths[keep:]:
                try:
                    os.remove(stale)
                except OSError:
                    pass
        except OSError as error:
            logger.debug(f"Could not prune screenshots: {error}")

    def consume_screenshot_path(self) -> str | None:
        """Returns the pending screenshot path once (then clears it)."""
        path, self._last_screenshot_path = self._last_screenshot_path, None
        return path

    def peek_screenshot_path(self) -> str | None:
        """Returns the pending screenshot path WITHOUT clearing it.

        Screenshots ride along every request until navigation, tab switch
        or close clears them (those already reset _last_screenshot_path):
        one-shot vision made the model ask humans instead of seeing.
        """
        return self._last_screenshot_path

    # -- helpers ------------------------------------------------------------

    def wait_seconds(self, seconds: float) -> None:
        self._sleep(max(0.0, seconds))
