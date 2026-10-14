"""Browser automation backend (Phase A: standalone, UI-independent).

Owns the Playwright lifecycle and exposes a ref-based grounding contract:
every snapshot assigns stable integer refs to visible interactive elements;
actions address elements by ref, never by coordinates. Designed to later
become the `dom` GroundingBackend without changing callers.
"""
import logging
import time

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


class BrowserManager:
    """Synchronous Playwright wrapper with persistent profile."""

    def __init__(self, headless: bool = False, profile_dir: str = "cache/browser_profile",
                 timeout_ms: int = 30000, block_ads: bool = True,
                 easylist_path: str | None = None):
        self.headless = headless
        self.profile_dir = profile_dir
        self.timeout_ms = timeout_ms
        self.block_ads = block_ads
        self._easylist_path = easylist_path
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
        self.last_known_url = ""  # plain-string URL cache, safe to read from any thread

    # -- lifecycle ------------------------------------------------------

    def launch(self) -> None:
        from playwright.sync_api import sync_playwright
        import os
        os.makedirs(self.profile_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch_persistent_context(
            user_data_dir=os.path.abspath(self.profile_dir),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        pages = self._browser.pages
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
        """Routes all requests through the EasyList decision (or removes routing)."""
        try:
            self._browser.unroute_all(behavior="wait")
        except Exception:
            pass
        if not self.block_ads:
            return
        try:
            self._browser.route("**/*", self._route_handler)
            logger.info("Browser ad-request routing installed.")
        except Exception as e:
            logger.warning(f"Could not install ad-request routing: {e}")

    def _route_handler(self, route) -> None:
        try:
            request = route.request
            frame_url = ""
            try:
                frame_url = (request.frame.url or "") if request.frame else ""
            except Exception:
                pass
            if self._ad_engine().should_block(
                    request.url, request.resource_type, frame_url):
                logger.debug(f"Blocked ad request: {request.url[:160]}")
                route.abort()
                return
        except Exception as e:
            logger.warning(f"Ad route handler failed: {e}")
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

    def _require_page(self):
        if self._page is None:
            raise RuntimeError("Browser is not launched. Call launch() first.")
        return self._page

    def ensure_launched(self) -> None:
        """Launch on first use (idempotent). Tools call this, not launch()."""
        if self._page is None:
            self.launch()

    def _use_page(self, page) -> None:
        """Makes page the active one: timeouts, dialog router, refs expire."""
        self._page = page
        try:
            self.last_known_url = page.url or ""
        except Exception:
            pass
        self._page.set_default_timeout(self.timeout_ms)
        self._install_dialog_router(page)
        self._refs = {}
        self._last_snapshot = []
        self._last_screenshot_path = None  # stale image is worse than none

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
        self.ensure_launched()
        page = self._require_page()
        logger.info(f"Browser goto: {url}")
        self._refs = {}  # refs expire on every navigation
        self._last_snapshot = []
        self._last_screenshot_path = None
        page.goto(url, wait_until=wait)
        self.last_known_url = url

    def title(self) -> str:
        return self._require_page().title()

    def url(self) -> str:
        return self._require_page().url

    def read(self, max_chars: int = 3000) -> str:
        """Page title, URL and visible text (for answering 'what is here')."""
        page = self._require_page()
        try:
            text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            text = ""
        text = " ".join(text.split())
        return f"Title: {page.title()}\nURL: {page.url}\nText: {text[:max_chars]}"

    # -- grounding (ref snapshot) ----------------------------------------

    def snapshot(self, max_elements: int = 200) -> list:
        """Visible interactive elements as [{ref, role, name}]. Rebuilds refs."""
        page = self._require_page()
        locators = page.locator(_INTERACTIVE_SELECTOR)
        count = min(locators.count(), max_elements)
        elements = []
        self._refs = {}
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
                elements.append({"ref": ref, "role": role, "name": name})
            except Exception:
                continue
        logger.info(f"Browser snapshot: {len(elements)} interactive elements.")
        self._last_snapshot = elements
        return elements

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
        lines = [f"[{e['ref']}] {e['role']}: {e['name'] or '(no label)'}" for e in self._last_snapshot]
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
        else short parent text. Empty string when nothing suitable."""
        try:
            text = loc.evaluate("""e => {
                const t = el => ((el && el.innerText) || '').trim();
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
                const direct = e.parentElement ? t(e.parentElement).split('\\n')[0] : '';
                return direct.length <= 60 ? direct : '';
            }""") or ""
            return text.strip()
        except Exception:
            return ""

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
        loc = self._ref(ref)
        logger.info(f"Browser click ref={ref}")
        try:
            opener_url = self._page.url if self._page else ""
        except Exception:
            opener_url = ""
        before = {id(p) for p in self._browser.pages}
        loc.click(timeout=self.timeout_ms)
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
        return f"{url[:120]!r} {title[:60]!r}{tag}"

    def consume_tabs_note(self) -> str:
        """Returns and clears the new-tabs notice (called after actions)."""
        note, self._pending_tabs_note = self._pending_tabs_note, None
        return note

    def fill(self, ref: int, text: str) -> None:
        loc = self._ref(ref)
        logger.info(f"Browser fill ref={ref} ({len(text)} chars)")
        loc.fill(text, timeout=self.timeout_ms)

    def select_option(self, ref: int, value: str | None = None,
                      label: str | None = None, index: int | None = None) -> str:
        """Selects an option in a <select> dropdown (never use fill/click on selects)."""
        loc = self._ref(ref)
        kwargs = {}
        if value is not None:
            kwargs["value"] = value
        elif label is not None:
            kwargs["label"] = label
        elif index is not None:
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
        return f"Selected option {kwargs} in element ref={ref}."

    def hover(self, ref: int) -> None:
        loc = self._ref(ref)
        logger.info(f"Browser hover ref={ref}")
        loc.hover(timeout=self.timeout_ms)

    def navigate_back(self) -> str:
        page = self._require_page()
        logger.info("Browser navigate back")
        page.go_back(wait_until="domcontentloaded")
        self._refs = {}
        self._last_snapshot = []
        self._last_screenshot_path = None
        try:
            self.last_known_url = page.url or ""
        except Exception:
            pass
        return f"Went back. URL: {page.url}. Take a fresh browser_snapshot before acting."

    def wait_for(self, seconds: float | None = None, text: str | None = None,
                 text_gone: str | None = None) -> str:
        """Waits for time (max 30s), text to appear, or text to disappear."""
        page = self._require_page()
        if seconds is not None:
            try:
                wait = max(0.0, min(float(seconds), 30.0))
            except (TypeError, ValueError) as e:
                raise ValueError(f"browser_wait_for: 'time' must be a number, got '{seconds}'.") from e
            logger.info(f"Browser wait {wait}s")
            time.sleep(wait)
            return f"Waited {wait}s."
        if text:
            page.get_by_text(text).first.wait_for(timeout=self.timeout_ms)
            return f"Text appeared: '{text}'."
        if text_gone:
            page.get_by_text(text_gone).first.wait_for(state="hidden", timeout=self.timeout_ms)
            return f"Text disappeared: '{text_gone}'."
        raise ValueError("browser_wait_for needs one of: 'time', 'text', 'text_gone'.")

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
                lines.append(f"[{i}] {title} — {purl}{ad_tag}{mark}")
            return "Open tabs:\n" + "\n".join(lines)
        if action == "new":
            page = self._browser.new_page()
            if url:
                page.goto(url, wait_until="domcontentloaded")
            self._use_page(page)
            return f"Opened new tab. URL: {self._page.url}. Take a fresh browser_snapshot before acting."
        if action == "select":
            if index is None:
                raise ValueError("browser_tabs select requires 'index'. Call action=list first.")
            try:
                page = pages[int(index)]
            except (IndexError, ValueError, TypeError) as e:
                raise ValueError(f"Unknown tab index '{index}'. Call action=list first.") from e
            self._use_page(page)
            return (f"Switched to tab [{int(index)}]. Title: '{page.title()}'. "
                    f"URL: {page.url}. Take a fresh browser_snapshot before acting.")
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
            return f"Closed tab. Now on: {self._page.url}. Take a fresh browser_snapshot before acting."
        raise ValueError(f"Unknown browser_tabs action '{action}'. Use list/new/select/close.")

    def press(self, key: str) -> None:
        logger.info(f"Browser press key={key}")
        self._require_page().keyboard.press(key)

    def type_text(self, text: str) -> None:
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

    def screenshot(self, path: str) -> str:
        self._require_page()
        self._overlay_ref_badges()
        try:
            self._require_page().screenshot(path=path)
        finally:
            self._remove_ref_badges()
        return path

    def capture_screenshot(self, prefix: str = "browser") -> str:
        """Saves a viewport screenshot, remembered once for the next LLM request."""
        import os
        from datetime import datetime
        self.ensure_launched()
        os.makedirs("screenshots", exist_ok=True)
        path = os.path.join("screenshots", f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        self.screenshot(path)
        self._last_screenshot_path = path
        logger.info(f"Browser screenshot saved: {path}")
        return path

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
        time.sleep(max(0.0, seconds))
