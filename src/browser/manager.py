"""Browser automation backend (Phase A: standalone, UI-independent).

Owns the Playwright lifecycle and exposes a ref-based grounding contract:
every snapshot assigns stable integer refs to visible interactive elements;
actions address elements by ref, never by coordinates. Designed to later
become the `dom` GroundingBackend without changing callers.
"""
import logging
import time

logger = logging.getLogger(__name__)

_INTERACTIVE_SELECTOR = (
    "a[href], button, input, select, textarea, "
    "[role=button], [role=link], [role=checkbox], [role=radio], "
    "[role=switch], [role=tab], [role=menuitem]"
)

_TAG_TO_ROLE = {
    "a": "link", "button": "button", "input": "textbox",
    "select": "combobox", "textarea": "textbox",
}


class BrowserManager:
    """Synchronous Playwright wrapper with persistent profile."""

    def __init__(self, headless: bool = False, profile_dir: str = "cache/browser_profile",
                 timeout_ms: int = 30000):
        self.headless = headless
        self.profile_dir = profile_dir
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._page = None
        self._refs = {}  # ref -> playwright ElementHandle

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
        self._page = pages[0] if pages else self._browser.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        logger.info(f"Browser launched (headless={self.headless}).")

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

    def _require_page(self):
        if self._page is None:
            raise RuntimeError("Browser is not launched. Call launch() first.")
        return self._page

    def ensure_launched(self) -> None:
        """Launch on first use (idempotent). Tools call this, not launch()."""
        if self._page is None:
            self.launch()

    # -- navigation -----------------------------------------------------

    def goto(self, url: str, wait: str = "domcontentloaded") -> None:
        self.ensure_launched()
        page = self._require_page()
        logger.info(f"Browser goto: {url}")
        self._refs = {}  # refs expire on every navigation
        page.goto(url, wait_until=wait)

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
                role = explicit_role or _TAG_TO_ROLE.get(tag, "") or tag or "element"
                name = (loc.get_attribute("aria-label")
                        or loc.get_attribute("placeholder")
                        or loc.get_attribute("value")
                        or loc.get_attribute("alt")
                        or (loc.inner_text(timeout=500).strip().split("\n")[0] if tag in ("a", "button") else "")
                        or "").strip()[:120]
                ref = len(elements) + 1
                self._refs[ref] = loc
                elements.append({"ref": ref, "role": role, "name": name})
            except Exception:
                continue
        logger.info(f"Browser snapshot: {len(elements)} interactive elements.")
        return elements

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
        loc.click(timeout=self.timeout_ms)
        self._refs = {}  # a click may navigate: force a fresh snapshot

    def fill(self, ref: int, text: str) -> None:
        loc = self._ref(ref)
        logger.info(f"Browser fill ref={ref} ({len(text)} chars)")
        loc.fill(text, timeout=self.timeout_ms)

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

    def screenshot(self, path: str) -> str:
        self._require_page().screenshot(path=path)
        return path

    # -- helpers ------------------------------------------------------------

    def wait_seconds(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))
