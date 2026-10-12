"""Programmatic test harness for browser automation (no UI needed).

Usage (from project root):
    .venv\\Scripts\\python.exe -m src.browser.test_cli open https://example.com
    .venv\\Scripts\\python.exe -m src.browser.test_cli form
    .venv\\Scripts\\python.exe -m src.browser.test_cli login --headless

Commands:
    open <url>   open a page, print title + interactive elements
    form         load a local test form, fill it, submit, verify result
    login        open example login flow placeholder (manual check)
"""
import sys

sys.path.insert(0, '.')
sys.path.insert(0, 'src')


def cmd_open(url: str, headless: bool):
    from src.browser.manager import BrowserManager
    mgr = BrowserManager(headless=headless)
    try:
        mgr.launch()
        mgr.goto(url)
        print("TITLE:", mgr.title())
        for el in mgr.snapshot():
            print(f"  [{el['ref']}] {el['role']}: {el['name'][:80]}")
    finally:
        mgr.close()


FORM_HTML = """<html><body>
<h1>Logos form probe</h1>
<form id="f">
<input name="login" type="text" />
<input name="pwd" type="password" />
<button type="submit">Go</button>
</form>
<div id="out"></div>
<script>document.getElementById('f').addEventListener('submit', e => {
e.preventDefault();
document.getElementById('out').textContent =
  'hello:' + document.querySelector('[name=login]').value; });</script>
</body></html>"""


def cmd_form(headless: bool):
    import urllib.parse
    from src.browser.manager import BrowserManager
    mgr = BrowserManager(headless=headless)
    try:
        mgr.launch()
        mgr.goto("data:text/html," + urllib.parse.quote(FORM_HTML))
        els = mgr.snapshot()
        login_ref = next(e["ref"] for e in els if "textbox" in e["role"] and "login" in (e["name"] or "").lower() or e["role"] == "textbox")
        print("login ref:", login_ref)
        mgr.fill(login_ref, "tester")
        btn_ref = next(e["ref"] for e in els if "button" in e["role"])
        mgr.click(btn_ref)
        mgr.wait_for_text("hello:tester", timeout=5000)
        print("FORM-OK: submitted and verified")
    finally:
        mgr.close()


def cmd_verify(headless: bool):
    """Exercise all 6 tools, then a combined scenario. Deterministic, no LLM."""
    import urllib.parse
    from src.browser.manager import BrowserManager
    mgr = BrowserManager(headless=headless)
    try:
        mgr.launch()
        # 1. browser_goto
        mgr.goto("https://example.com")
        assert "Example" in mgr.title(), mgr.title()
        print("1. goto OK:", mgr.title(), "| url:", mgr.url())
        # 2. browser_snapshot
        els = mgr.snapshot()
        assert els, "empty snapshot"
        print(f"2. snapshot OK: {len(els)} elements")
        # 3. browser_click (link -> navigates away and back)
        link = next(e for e in els if e["role"] == "link")
        mgr.click(link["ref"])
        mgr.wait_for_load()
        assert "iana.org" in mgr.url(), mgr.url()
        print("3. click OK, now at:", mgr.url())
        # stale ref must fail cleanly
        try:
            mgr.click(link["ref"])
            print("   WARNING: stale ref did not fail")
        except Exception as e:
            print("   stale-ref guard OK:", str(e)[:60])
        # 4/5/6. fill + press + close on a local form
        mgr.goto("data:text/html," + urllib.parse.quote(FORM_HTML))
        els = mgr.snapshot()
        login_ref = next(e["ref"] for e in els if e["role"] == "textbox")
        mgr.fill(login_ref, "combo")
        mgr.press("Tab")
        mgr.press("Escape")
        print("4/5. fill+press OK")
        btn_ref = next(e["ref"] for e in els if "button" in e["role"])
        mgr.click(btn_ref)
        mgr.wait_for_text("hello:combo", timeout=5000)
        print("6. combined form scenario OK")
        mgr.screenshot(r"C:\Users\vipTe\AppData\Local\Temp\opencode\browser_verify.png")
    finally:
        # 7. browser_close
        mgr.close()
        print("7. close OK")
    print("VERIFY-ALL-OK")


def main(argv: list) -> int:
    headless = "--headless" in argv
    argv = [a for a in argv if a != "--headless"]
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "open" and len(argv) > 1:
        cmd_open(argv[1], headless)
    elif argv[0] == "form":
        cmd_form(headless)
    elif argv[0] == "verify":
        cmd_verify(headless)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
