"""Programmatic test harness for browser automation (no UI needed).

Usage (from project root):
    .venv\\Scripts\\python.exe -m src.browser.test_cli open https://example.com
    .venv\\Scripts\\python.exe -m src.browser.test_cli form
    .venv\\Scripts\\python.exe -m src.browser.test_cli login --headless
    .venv\\Scripts\\python.exe -m src.browser.test_cli slice1 --headless
    .venv\\Scripts\\python.exe -m src.browser.test_cli slice2 --headless

Commands:
    open <url>   open a page, print title + interactive elements
    form         load a local test form, fill it, submit, verify result
    verify       exercise goto/snapshot/click/fill/press/close end to end
    slice1       exercise screenshot scopes, reload, forward, scroll,
                 check/uncheck, eval by ref and set_files on a local page
    slice2       exercise selector/clip screenshots, response bodies,
                 downloads and drag on a local HTTP page
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


def _local_page(name: str, html: str) -> str:
    """Writes a probe page to a temp file and returns its file:// URL.

    `data:` URLs are refused by the navigation policy (only http/https/file),
    so the harness has to use a real local file like any other page.
    """
    import tempfile
    from pathlib import Path
    root = Path(tempfile.mkdtemp(prefix="logos_probe_"))
    path = root / name
    path.write_text(html, encoding="utf-8")
    return path.as_uri()


def cmd_form(headless: bool):
    from src.browser.manager import BrowserManager
    mgr = BrowserManager(headless=headless)
    try:
        mgr.launch()
        mgr.goto(_local_page("form.html", FORM_HTML))
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
    """Exercise the core tools, then a combined scenario. Deterministic, no LLM."""
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
        mgr.goto(_local_page("form.html", FORM_HTML))
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


SLICE1_HTML = """<!doctype html><html><body>
<h1>Slice 1 probe</h1>
<label>Name <input id="name" value="Alice" aria-label="name field"></label>
<label><input type="checkbox" id="agree" aria-label="agree box"> Agree</label>
<div role="checkbox" aria-checked="false" id="custom" aria-label="custom switch"
     style="width:40px;height:40px;border:1px solid #333"
     onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked')==='true'?'false':'true')">C</div>
<input type="file" id="upload" aria-label="upload file">
<p>Navigation type: <span id="navtype">?</span></p>
<a href="page2.html" aria-label="go page two">Next page</a>
<div style="height:2400px"></div>
<button id="bottom" aria-label="bottom button"
        onclick="document.title='bottom clicked'">Bottom</button>
<script>
document.getElementById('navtype').textContent =
  (performance.getEntriesByType('navigation')[0] || {}).type || 'unknown';
</script>
</body></html>"""


def _png_size(path: str) -> tuple[int, int]:
    """Width/height straight from a PNG's IHDR chunk."""
    with open(path, "rb") as handle:
        head = handle.read(33)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return (int.from_bytes(head[16:20], "big"),
            int.from_bytes(head[20:24], "big"))


def cmd_slice1(headless: bool):
    """Verifies the slice-1 browser surface against a deterministic local page."""
    import tempfile
    from pathlib import Path
    from src.browser.manager import BrowserManager

    root = Path(tempfile.mkdtemp(prefix="logos_slice1_"))
    page1, page2 = root / "page1.html", root / "page2.html"
    upload = root / "upload.txt"
    upload.write_text("logos upload probe\n", encoding="utf-8")
    page2.write_text("<html><body><h1>Page two</h1></body></html>", encoding="utf-8")
    page1.write_text(SLICE1_HTML, encoding="utf-8")

    mgr = BrowserManager(headless=headless)
    mgr.screenshot_dir = str(root / "shots")
    try:
        mgr.goto(page1.as_uri())
        print("1. goto OK:", mgr.title())
        refs = {e["name"]: e["ref"] for e in mgr.snapshot()}

        full = mgr.capture_screenshot("probe_full", full_page=True)
        width, height = _png_size(full)
        view_width, view_height = mgr.viewport_size()
        assert height > view_height, (height, view_height)
        print(f"2. full_page OK: {width}x{height} (viewport {view_width}x{view_height})")

        bottom_ref = refs["bottom button"]
        element = mgr.capture_screenshot("probe_element", ref=bottom_ref)
        element_width, element_height = _png_size(element)
        assert 0 < element_height < height, (element_height, height)
        print(f"3. element screenshot OK: {element_width}x{element_height}")

        print("4. scroll wheel:", mgr.scroll(0, 600))
        assert float(mgr.eval_js("() => window.scrollY")) > 0
        mgr.eval_js("() => window.scrollTo(0, 0)")
        print("5. scroll into view:", mgr.scroll(ref=bottom_ref))
        assert float(mgr.eval_js("() => window.scrollY")) > 0

        mgr.reload()
        nav_type = mgr.eval_js(
            "() => document.getElementById('navtype').textContent")
        assert nav_type == "reload", nav_type
        print("6. reload OK:", nav_type)
        refs = {e["name"]: e["ref"] for e in mgr.snapshot()}

        agree_ref = refs["agree box"]
        mgr.set_checked(agree_ref, True)
        assert mgr.eval_js("el => el.checked", ref=agree_ref) == "true"
        mgr.set_checked(agree_ref, False)
        assert mgr.eval_js("el => el.checked", ref=agree_ref) == "false"
        print("7. check/uncheck OK")

        custom_ref = refs["custom switch"]
        mgr.set_checked(custom_ref, True)
        assert mgr.eval_js(
            "el => el.getAttribute('aria-checked')", ref=custom_ref) == "true"
        print("8. custom role=checkbox OK")

        name_ref = refs["name field"]
        assert mgr.eval_js("el => el.value", ref=name_ref) == "Alice"
        print("9. eval by ref OK")

        print("10. set_files:", mgr.set_files(refs["upload file"], [str(upload)]))
        assert mgr.eval_js(
            "el => el.files.length", ref=refs["upload file"]) == "1"

        mgr.click(refs["go page two"])
        mgr.wait_for_load()
        assert "page2" in mgr.url(), mgr.url()
        print("11. back:", mgr.navigate_back())
        assert "page1" in mgr.url(), mgr.url()
        print("    forward:", mgr.navigate_forward())
        assert "page2" in mgr.url(), mgr.url()
        print("12. history forward/back OK")
    finally:
        mgr.close()
    print("SLICE1-OK")


SLICE2_HTML = """<!doctype html><html><body>
<h1>Slice 2 probe</h1>
<div id="panel" style="width:240px;height:120px;background:#ddd">Panel text</div>
<div id="drag" draggable="true" role="button" aria-label="drag source"
     ondragstart="event.dataTransfer.setData('text/plain','x')">Drag me</div>
<div id="drop" role="button" aria-label="drop target"
     ondragover="event.preventDefault()"
     ondrop="event.preventDefault(); this.textContent='dropped'">Drop here</div>
<a id="dl" href="download.txt" download="probe.txt" aria-label="download file">Download</a>
<p id="state">waiting</p>
<script>
fetch('api').then(r => r.json()).then(d => {
  document.getElementById('state').textContent = 'loaded:' + d.message;
}).catch(e => { document.getElementById('state').textContent = 'error:' + e; });
</script>
</body></html>"""


def cmd_slice2(headless: bool):
    """Verifies the slice-2 surface: selector/clip, bodies, downloads, drag."""
    import json
    import tempfile
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from src.browser.manager import BrowserManager

    root = Path(tempfile.mkdtemp(prefix="logos_slice2_"))
    (root / "download.txt").write_text(
        "downloaded by logos probe\n", encoding="utf-8")
    (root / "page.html").write_text(SLICE2_HTML, encoding="utf-8")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            if self.path.split("?")[0] == "/api":
                payload = json.dumps({"message": "hello-body"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    mgr = BrowserManager(headless=headless)
    mgr.screenshot_dir = str(root / "shots")
    mgr.downloads_dir = str(root / "downloads")
    try:
        mgr.goto(f"http://127.0.0.1:{port}/page.html")
        mgr.wait_for_text("loaded:hello-body", timeout=5000)
        mgr.pump_events(300)
        network = mgr.logs.render_network(limit=30)
        assert "hello-body" in network, network
        print("1. response body OK")

        selector_shot = mgr.capture_screenshot("probe_selector", selector="#panel")
        width, height = _png_size(selector_shot)
        assert width == 240 and 100 <= height <= 140, (width, height)
        print(f"2. selector screenshot OK: {width}x{height}")

        clip_shot = mgr.capture_screenshot(
            "probe_clip", clip={"x": 0, "y": 0, "width": 100, "height": 50})
        width, height = _png_size(clip_shot)
        assert (width, height) == (100, 50), (width, height)
        print(f"3. clip screenshot OK: {width}x{height}")

        refs = {e["name"]: e["ref"] for e in mgr.snapshot()}
        mgr.click(refs["download file"])
        mgr.pump_events(500)
        downloads = mgr.downloads()
        assert downloads and downloads[0]["path"], downloads
        staged = Path(downloads[0]["path"])
        assert staged.is_file()
        assert "downloaded by logos probe" in staged.read_text(encoding="utf-8")
        print("4. download staged:", downloads[0]["filename"])

        saved = root / "saved-download.txt"
        print("   save:", mgr.save_download(1, str(saved)))
        assert saved.read_text(encoding="utf-8") == "downloaded by logos probe\n"
        print("5. download save OK")

        refs = {e["name"]: e["ref"] for e in mgr.snapshot()}
        print("6. drag:", mgr.drag(refs["drag source"], refs["drop target"]))
        mgr.wait_for_text("dropped", timeout=5000)
        print("7. drag OK")
    finally:
        mgr.close()
        server.shutdown()
        server.server_close()
    print("SLICE2-OK")


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
    elif argv[0] == "slice1":
        cmd_slice1(headless)
    elif argv[0] == "slice2":
        cmd_slice2(headless)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
