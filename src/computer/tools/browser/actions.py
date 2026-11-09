import json

from src.browser.policy import redact_url
from src.computer.computer import tool_meta


def _manager(self):
    mgr = getattr(self, "browser_manager", None)
    if mgr is None:
        raise ValueError(
            "Action failed: browser is not started. The plan must open the browser first "
            "(browser_goto starts it automatically)."
        )
    return mgr


def _with_dialog_note(mgr, text: str) -> str:
    """Appends auto-dismissed/handled JS dialog and new-tab notes."""
    note = mgr.consume_dialog_note()
    if note:
        text = f"{text} [{note}]"
    return text


def _int_arg(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _bool_arg(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _ref_arg(value):
    """Optional element ref: blank/None means 'no ref', anything else an int."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        raise ValueError(
            f"Expected an element ref from browser_snapshot, got '{value}'.") from None


def _list_arg(value) -> list:
    """A list argument that may arrive as a real list or a JSON string."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except ValueError:
            return [text]
        value = decoded if isinstance(decoded, list) else [decoded]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


@tool_meta(
    tool={
        "name": "browser_goto",
        "strategic": "Opens a URL in the controlled browser (starts it if needed).",
        "description": "Navigates the controlled browser to the given URL and waits for the page to load.",
        "arguments": {
            "description": {"type": "STRING", "description": "What this navigation is for.", "required": True},
            "url": {"type": "STRING", "description": "Full URL including https://. A file:// URL works only for a file inside the project directory.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_goto(self, **kwargs):
    url = kwargs.get("url")
    if not url:
        raise ValueError("Action 'browser_goto' failed: Missing required 'url' argument.")
    mgr = getattr(self, "browser_manager", None)
    if mgr is None:
        # Lazy start: the harness/handler injects a factory instead.
        factory = getattr(self, "browser_factory", None)
        if factory is None:
            raise ValueError("Action 'browser_goto' failed: no browser attached to the Computer.")
        mgr = factory()
        self.browser_manager = mgr
    mgr.goto(url)
    opened = redact_url(mgr.last_known_url or url)
    return _with_dialog_note(mgr, f"Opened {opened}. Page title: '{mgr.title()}'. Take a fresh browser_snapshot before acting.")


@tool_meta(
    tool={
        "name": "browser_snapshot",
        "strategic": "Lists visible interactive elements with refs.",
        "description": "Returns visible links/buttons/inputs as a numbered ref list. Refs expire after every navigation or page change — take a fresh snapshot before acting.",
        "arguments": {
            "description": {"type": "STRING", "description": "Why the snapshot is needed.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_snapshot(self, **kwargs):
    mgr = _manager(self)
    elements = mgr.snapshot()
    try:
        context = mgr.heading_context()
    except Exception:
        context = ""
    head = f"Page context: {context}\n" if context else ""
    if not elements:
        return (head + "No interactive elements found on this page. "
                "The page may still be loading, or you may need a different "
                "screen — take a screenshot to see the current state, check "
                "browser_frames, or click by x/y coordinates before "
                "deciding your next step.")
    lines = []
    for e in elements:
        where = f"(frame {e['frame']}: {e.get('frame_host', '?')}) " if e.get("frame") else ""
        lines.append(f"[{e['ref']}] {where}{e['role']}: {e['name'] or '(no label)'}")
    note = ""
    if any(e.get("frame") for e in elements):
        note = ("\nElements marked (frame N: host) live inside an iframe: click, fill and "
                "hover them by ref exactly like the rest.\n")
    return head + note + "Interactive elements:\n" + "\n".join(lines)


@tool_meta(
    tool={
        "name": "browser_click",
        "strategic": "Clicks an element by ref, or a point by coordinates.",
        "description": (
            "Clicks the element with the given ref from the latest snapshot. When the "
            "target has no ref - canvas, image map, a custom widget that reacts only to "
            "raw mouse events, or a control inside a cross-origin frame - pass x and y "
            "instead: CSS pixels counted from the top-left corner of the visible page, "
            "read off browser_screenshot. Coordinates are for the current scroll "
            "position only."),
        "arguments": {
            "description": {"type": "STRING", "description": "What is being clicked.", "required": True},
            "ref": {"type": "INTEGER", "description": "Element ref from browser_snapshot. Leave out when clicking by coordinates.", "required": False},
            "x": {"type": "INTEGER", "description": "Horizontal point in CSS pixels of the visible page.", "required": False},
            "y": {"type": "INTEGER", "description": "Vertical point in CSS pixels of the visible page.", "required": False},
            "button": {"type": "STRING", "description": "left (default), right or middle. Coordinates only.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_click(self, **kwargs):
    ref = kwargs.get("ref")
    x, y = kwargs.get("x"), kwargs.get("y")
    if x is not None and y is not None:
        mgr = _manager(self)
        try:
            px, py = float(x), float(y)
        except (TypeError, ValueError):
            raise ValueError(f"browser_click needs numeric x/y, got x={x!r}, y={y!r}.") from None
        width, height = mgr.click_at(px, py, button=kwargs.get("button") or "left")
        note = f" (viewport {width}x{height})" if width else ""
        return _with_dialog_note(
            mgr,
            f"Clicked at ({px:g}, {py:g}){note}. A coordinate click cannot confirm what "
            f"was under it - take browser_snapshot or browser_screenshot to verify.")
    if ref is None:
        raise ValueError(
            "Action 'browser_click' failed: missing 'ref', or 'x' and 'y' for a "
            "coordinate click. Take browser_snapshot first.")
    mgr = _manager(self)
    mgr.click(ref)
    return _with_dialog_note(mgr, f"Clicked element ref={ref}.")


@tool_meta(
    tool={
        "name": "browser_frames",
        "strategic": "Lists the iframes of the current page.",
        "description": (
            "Lists this page's frames with their index, URL and name. Index 0 is the top "
            "document. Controls inside a frame are already clickable by ref; the index is "
            "only needed to run browser_eval inside that frame."),
        "arguments": {
            "description": {"type": "STRING", "description": "Why the frame list is needed.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_frames(self, **kwargs):
    mgr = _manager(self)
    frames = mgr.frames()
    lines = []
    for frame in frames:
        line = f"[{frame['index']}] {frame['url'] or '(not loaded yet)'}"
        if frame["name"]:
            line += f" name={frame['name']}"
        if frame["main"]:
            line += " (top document)"
        lines.append(line)
    return f"Frames ({len(frames)}):\n" + "\n".join(lines)


@tool_meta(
    tool={
        "name": "browser_eval",
        "strategic": "Runs one JavaScript expression in the page.",
        "description": (
            "Evaluates one JavaScript expression or function in the page and returns what "
            "it evaluates to as text. Use it only where clicks and snapshots cannot help: "
            "reading a value the page keeps in JS state, measuring where an element is, or "
            "driving a widget that has no clickable element. It cannot navigate the browser "
            "itself, and it must never be used to read credentials, cookies or storage, or "
            "to fetch another site with the page's session. Pass frame (see browser_frames) "
            "to run inside an iframe; the default is the top document. Pass ref to run the "
            "script against one snapshot element (el => el.value) instead of guessing a "
            "CSS selector."),
        "arguments": {
            "description": {"type": "STRING", "description": "What the script is for.", "required": True},
            "script": {"type": "STRING", "description": "JavaScript expression or function source, e.g. document.title or () => window.innerWidth.", "required": True},
            "frame": {"type": "INTEGER", "description": "Frame index from browser_frames; omit for the top document.", "required": False},
            "ref": {"type": "INTEGER", "description": "Run against this element from browser_snapshot; the script receives it as its argument.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_eval(self, **kwargs):
    script = kwargs.get("script")
    if not script:
        raise ValueError("Action 'browser_eval' failed: missing 'script' with the JavaScript to run.")
    mgr = _manager(self)
    result = mgr.eval_js(script, frame=kwargs.get("frame") or 0,
                         ref=_ref_arg(kwargs.get("ref")))
    return _with_dialog_note(mgr, f"JavaScript returned: {result}")


@tool_meta(
    tool={
        "name": "browser_fill",
        "strategic": "Fills an input by its ref.",
        "description": "Types text into the input/textarea with the given ref. Never paste secrets you were not explicitly given for this task.",
        "arguments": {
            "description": {"type": "STRING", "description": "What is being filled.", "required": True},
            "ref": {"type": "INTEGER", "description": "Element ref from browser_snapshot.", "required": True},
            "text": {"type": "STRING", "description": "Text to type.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_fill(self, **kwargs):
    ref, text = kwargs.get("ref"), kwargs.get("text")
    if ref is None or text is None:
        raise ValueError("Action 'browser_fill' failed: Missing required 'ref' or 'text'. Take browser_snapshot first.")
    mgr = _manager(self)
    mgr.fill(ref, text)
    return _with_dialog_note(mgr, f"Filled element ref={ref}.")


@tool_meta(
    tool={
        "name": "browser_press",
        "strategic": "Presses a keyboard key in the page.",
        "description": "Presses a key like Enter, Escape, Tab, ArrowDown.",
        "arguments": {
            "description": {"type": "STRING", "description": "Why the key is pressed.", "required": True},
            "key": {"type": "STRING", "description": "Key name, e.g. Enter.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_press(self, **kwargs):
    key = kwargs.get("key")
    if not key:
        raise ValueError("Action 'browser_press' failed: Missing required 'key' argument.")
    mgr = _manager(self)
    mgr.press(key)
    return _with_dialog_note(mgr, f"Pressed {key}.")


@tool_meta(
    tool={
        "name": "browser_select_option",
        "strategic": "Selects an option in a dropdown.",
        "description": "Selects an option in a <select> dropdown by ref. Never use browser_fill/browser_click on selects. Pass one of: value (option value), label (visible option text, see browser_read), index (0-based option index).",
        "arguments": {
            "description": {"type": "STRING", "description": "Which dropdown option is chosen.", "required": True},
            "ref": {"type": "INTEGER", "description": "Element ref from browser_snapshot.", "required": True},
            "value": {"type": "STRING", "description": "Option value attribute.", "required": False},
            "label": {"type": "STRING", "description": "Visible option text.", "required": False},
            "index": {"type": "INTEGER", "description": "0-based option index.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_select_option(self, **kwargs):
    ref = kwargs.get("ref")
    if ref is None:
        raise ValueError("Action 'browser_select_option' failed: Missing required 'ref' argument. Take browser_snapshot first.")
    mgr = _manager(self)
    result = mgr.select_option(ref, value=kwargs.get("value"), label=kwargs.get("label"), index=kwargs.get("index"))
    return _with_dialog_note(mgr, result)


@tool_meta(
    tool={
        "name": "browser_tabs",
        "strategic": "Manages browser tabs.",
        "description": "Lists, opens, selects, or closes browser tabs. The manager does NOT auto-follow popups/new tabs — use action=list to see them and action=select to switch. Refs expire on tab switch: take a fresh browser_snapshot after.",
        "arguments": {
            "description": {"type": "STRING", "description": "What is done with tabs.", "required": True},
            "action": {"type": "STRING", "description": "One of: list, new, select, close.", "required": True},
            "index": {"type": "INTEGER", "description": "Tab index from action=list (for select/close).", "required": False},
            "url": {"type": "STRING", "description": "URL to open (for action=new).", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_tabs(self, **kwargs):
    action = kwargs.get("action")
    if not action:
        raise ValueError("Action 'browser_tabs' failed: Missing required 'action' argument (list/new/select/close).")
    return _manager(self).tabs(action, index=kwargs.get("index"), url=kwargs.get("url"))


@tool_meta(
    tool={
        "name": "browser_navigate_back",
        "strategic": "Returns to the previous page.",
        "description": "Goes back to the previous page in history. Refs expire: take a fresh browser_snapshot after.",
        "arguments": {
            "description": {"type": "STRING", "description": "Why to go back.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_navigate_back(self, **kwargs):
    return _manager(self).navigate_back()


@tool_meta(
    tool={
        "name": "browser_navigate_forward",
        "strategic": "Returns to the page you went back from.",
        "description": "Goes forward in history, undoing browser_navigate_back. Refs expire: take a fresh browser_snapshot after.",
        "arguments": {
            "description": {"type": "STRING", "description": "Why to go forward.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_navigate_forward(self, **kwargs):
    return _manager(self).navigate_forward()


@tool_meta(
    tool={
        "name": "browser_reload",
        "strategic": "Reloads the current page.",
        "description": (
            "Reloads the page you are on, repeating the request it made. Use it after "
            "changing the code behind a dev server instead of browser_goto: re-entering "
            "the URL loses a POST body and the scroll position, while a reload keeps "
            "both. Refs expire: take a fresh browser_snapshot after."),
        "arguments": {
            "description": {"type": "STRING", "description": "Why the page is reloaded.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_reload(self, **kwargs):
    mgr = _manager(self)
    return _with_dialog_note(mgr, mgr.reload())


@tool_meta(
    tool={
        "name": "browser_hover",
        "strategic": "Hovers over an element.",
        "description": "Hovers the mouse over the element with the given ref (reveals menus/tooltips).",
        "arguments": {
            "description": {"type": "STRING", "description": "What is hovered and why.", "required": True},
            "ref": {"type": "INTEGER", "description": "Element ref from browser_snapshot.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_hover(self, **kwargs):
    ref = kwargs.get("ref")
    if ref is None:
        raise ValueError("Action 'browser_hover' failed: Missing required 'ref' argument. Take browser_snapshot first.")
    _manager(self).hover(ref)
    return f"Hovered element ref={ref}."


@tool_meta(
    tool={
        "name": "browser_scroll",
        "strategic": "Scrolls the page or brings an element into view.",
        "description": (
            "Scrolls the page by dx/dy pixels with a real wheel event, which is what "
            "virtualised lists and lazy images react to. Positive dy scrolls down, "
            "positive dx scrolls right. Pass ref instead to scroll that element into "
            "view (for example a field far below the fold). The result reports the new "
            "page position, so you know whether the bottom was reached."),
        "arguments": {
            "description": {"type": "STRING", "description": "What should become visible.", "required": True},
            "dy": {"type": "INTEGER", "description": "Vertical pixels: positive scrolls down, negative up.", "required": False},
            "dx": {"type": "INTEGER", "description": "Horizontal pixels: positive scrolls right.", "required": False},
            "ref": {"type": "INTEGER", "description": "Scroll this element from browser_snapshot into view instead.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_scroll(self, **kwargs):
    mgr = _manager(self)
    ref = _ref_arg(kwargs.get("ref"))
    dx = _int_arg(kwargs.get("dx"), 0, -20000, 20000)
    dy = _int_arg(kwargs.get("dy"), 0, -20000, 20000)
    return mgr.scroll(dx, dy, ref=ref)


@tool_meta(
    tool={
        "name": "browser_check",
        "strategic": "Checks a checkbox or switch.",
        "description": (
            "Checks the checkbox/switch with the given ref and verifies the final state. "
            "Prefer this over browser_click: a custom control can toggle twice when it is "
            "clicked, and this reports what the state actually is afterwards."),
        "arguments": {
            "description": {"type": "STRING", "description": "What is being checked.", "required": True},
            "ref": {"type": "INTEGER", "description": "Element ref from browser_snapshot. Required by tools that act on one element; omit it for tools that do not.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_check(self, **kwargs):
    ref = _ref_arg(kwargs.get("ref"))
    if ref is None:
        raise ValueError("Action 'browser_check' failed: Missing required 'ref' argument. Take browser_snapshot first.")
    mgr = _manager(self)
    return _with_dialog_note(mgr, mgr.set_checked(ref, True))


@tool_meta(
    tool={
        "name": "browser_uncheck",
        "strategic": "Unchecks a checkbox or switch.",
        "description": (
            "Unchecks the checkbox/switch with the given ref and verifies the final state. "
            "Prefer this over browser_click for the same reason as browser_check."),
        "arguments": {
            "description": {"type": "STRING", "description": "What is being unchecked.", "required": True},
            "ref": {"type": "INTEGER", "description": "Element ref from browser_snapshot (required for this tool).", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_uncheck(self, **kwargs):
    ref = _ref_arg(kwargs.get("ref"))
    if ref is None:
        raise ValueError("Action 'browser_uncheck' failed: Missing required 'ref' argument. Take browser_snapshot first.")
    mgr = _manager(self)
    return _with_dialog_note(mgr, mgr.set_checked(ref, False))


@tool_meta(
    tool={
        "name": "browser_set_files",
        "strategic": "Attaches local files to a file input.",
        "description": (
            "Attaches one or more local files to an <input type=file> with the given ref, "
            "so an upload flow can actually be tested. The user is asked to approve it, "
            "because the page receives the files. Use absolute paths inside the project."),
        "arguments": {
            "description": {"type": "STRING", "description": "What is being uploaded and why.", "required": True},
            "ref": {"type": "INTEGER", "description": "Ref of the file input from browser_snapshot (required for this tool).", "required": False},
            "paths": {"type": "LIST_OF_STRINGS", "description": "Local file paths to attach.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_set_files(self, **kwargs):
    ref = _ref_arg(kwargs.get("ref"))
    if ref is None:
        raise ValueError("Action 'browser_set_files' failed: Missing required 'ref' argument. Take browser_snapshot first.")
    paths = _list_arg(kwargs.get("paths"))
    if not paths:
        raise ValueError("Action 'browser_set_files' failed: Missing required 'paths' argument (one or more file paths).")
    return _manager(self).set_files(ref, paths)


@tool_meta(
    tool={
        "name": "browser_wait_for",
        "strategic": "Waits for time or text.",
        "description": "Waits for a number of seconds (max 30), for text to appear, or for text to disappear. Prefer this over blind repeated snapshots.",
        "arguments": {
            "description": {"type": "STRING", "description": "What is waited for.", "required": True},
            "time": {"type": "INTEGER", "description": "Seconds to wait (max 30).", "required": False},
            "text": {"type": "STRING", "description": "Text to wait for to appear.", "required": False},
            "text_gone": {"type": "STRING", "description": "Text to wait for to disappear.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_wait_for(self, **kwargs):
    return _manager(self).wait_for(seconds=kwargs.get("time"), text=kwargs.get("text"), text_gone=kwargs.get("text_gone"))


@tool_meta(
    tool={
        "name": "browser_handle_dialog",
        "strategic": "Arms JS dialog policy.",
        "description": "Arms accept/dismiss policy for the next JavaScript dialog (alert/confirm/prompt), then perform the triggering action. Unhandled dialogs are auto-dismissed and reported in action results.",
        "arguments": {
            "description": {"type": "STRING", "description": "Which dialog is expected.", "required": True},
            "accept": {"type": "BOOLEAN", "description": "Accept (true) or dismiss (false) the next dialog.", "required": False},
            "prompt_text": {"type": "STRING", "description": "Text to enter for prompt dialogs.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_handle_dialog(self, **kwargs):
    accept = kwargs.get("accept", True)
    if isinstance(accept, str):
        accept = accept.strip().lower() not in ("false", "no", "0", "")
    return _manager(self).handle_dialog(bool(accept), kwargs.get("prompt_text"))


@tool_meta(
    tool={
        "name": "browser_find",
        "strategic": "Searches the latest snapshot.",
        "description": "Cheap substring/regex search over the latest browser_snapshot (with ref context). Use it to locate elements instead of re-reading the whole page.",
        "arguments": {
            "description": {"type": "STRING", "description": "What to locate.", "required": True},
            "text": {"type": "STRING", "description": "Plain text to search (case-insensitive).", "required": False},
            "pattern": {"type": "STRING", "description": "Regular expression to search (case-sensitive).", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_find(self, **kwargs):
    return _manager(self).find_in_snapshot(text=kwargs.get("text"), pattern=kwargs.get("pattern"))


@tool_meta(
    tool={
        "name": "browser_screenshot",
        "strategic": "Captures the page for visual understanding.",
        "description": (
            "Captures a screenshot ONLY when the current step cannot be solved from text "
            "(visual puzzle, image options, layout that text cannot convey). The image is "
            "attached to your next request automatically, exactly once. By default it is "
            "the visible viewport; full_page=true captures the whole scrollable page, and "
            "ref captures just that one element. Do NOT use routinely: every image costs "
            "extra tokens."),
        "arguments": {
            "description": {"type": "STRING", "description": "Why the image is needed for this step.", "required": True},
            "full_page": {"type": "BOOLEAN", "description": "Capture the whole scrollable page instead of the viewport.", "required": False},
            "ref": {"type": "INTEGER", "description": "Capture only the element with this ref from the latest snapshot.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_screenshot(self, **kwargs):
    mgr = _manager(self)
    full_page = _bool_arg(kwargs.get("full_page"), False)
    ref = _ref_arg(kwargs.get("ref"))
    prefix = "browser_element" if ref is not None else ("browser_full" if full_page else "browser")
    path = mgr.capture_screenshot(prefix=prefix, full_page=full_page, ref=ref)
    scope = (f"element ref={ref}" if ref is not None
             else ("full page" if full_page else "viewport"))
    return (f"Screenshot of the {scope} saved to {path}. It will be attached to "
            f"your next request automatically (once).")


@tool_meta(
    tool={
        "name": "browser_read",
        "strategic": "Reads page title, URL and visible text.",
        "description": "Returns the current page title, URL and visible text. Use it to answer 'what is here' questions instead of guessing.",
        "arguments": {
            "description": {"type": "STRING", "description": "What information is needed.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_read(self, **kwargs):
    return _manager(self).read()


@tool_meta(
    tool={
        "name": "browser_console",
        "strategic": "Shows console messages and uncaught page errors.",
        "description": (
            "Returns the newest browser console messages and uncaught JavaScript errors "
            "with their source location, oldest first. Use it when the page looks broken "
            "or a click had no visible effect: a failed fetch, a CORS error and an "
            "uncaught exception all show up here instead of being guessed. level: all "
            "(default), error or warning. The full log of this browser session is a file "
            "on disk; read more of it with read_files or search it with "
            "search_file_content."),
        "arguments": {
            "description": {"type": "STRING", "description": "What is being diagnosed.", "required": True},
            "limit": {"type": "INTEGER", "description": "How many newest entries to return (default 40, max 200).", "required": False},
            "level": {"type": "STRING", "description": "all (default), error or warning.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_console(self, **kwargs):
    mgr = _manager(self)
    mgr.pump_events()
    limit = _int_arg(kwargs.get("limit"), 40, 1, 200)
    level = str(kwargs.get("level") or "all")
    stats = mgr.logs.stats()
    head = (f"Console: {stats['console']} message(s) in this browser session "
            f"({stats['console_errors']} error(s), {stats['console_warnings']} "
            f"warning(s))")
    body = mgr.logs.render_console(limit=limit, level=level)
    if not body:
        return f"{head}\nNo console messages match level={level}."
    if level.strip().lower() not in ("", "all", "any"):
        head += f" · newest {limit}, level={level}"
    parts = [head, body]
    hint = mgr.logs.read_hint()
    if hint:
        parts.append(hint)
    return "\n".join(parts)


@tool_meta(
    tool={
        "name": "browser_network",
        "strategic": "Shows recent requests with status and timing.",
        "description": (
            "Returns the newest network requests of the current page with method, URL, "
            "HTTP status, resource type and duration, oldest first. Use it after a form "
            "submit, a login or a fetch to see what the server actually answered: a 404, "
            "a 500 or a connection failure explains a broken flow without guessing. "
            "only_problems=true keeps only failed requests and statuses >= 400. The full "
            "log of this browser session is a file on disk; read more of it with "
            "read_files or search it with search_file_content."),
        "arguments": {
            "description": {"type": "STRING", "description": "What is being diagnosed.", "required": True},
            "limit": {"type": "INTEGER", "description": "How many newest entries to return (default 40, max 200).", "required": False},
            "only_problems": {"type": "BOOLEAN", "description": "Keep only failed requests and 4xx/5xx statuses.", "required": False},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_network(self, **kwargs):
    mgr = _manager(self)
    mgr.pump_events()
    limit = _int_arg(kwargs.get("limit"), 40, 1, 200)
    only_problems = _bool_arg(kwargs.get("only_problems"), False)
    stats = mgr.logs.stats()
    head = (f"Network: {stats['network']} response(s) in this browser session "
            f"({stats['network_problems']} failed or status >= 400)")
    body = mgr.logs.render_network(limit=limit, only_problems=only_problems)
    if not body:
        return (f"{head}\nNo requests to show"
                + (" that failed or returned >= 400." if only_problems else "."))
    if only_problems:
        head += f" · newest {limit}, problems only"
    parts = [head, body]
    hint = mgr.logs.read_hint()
    if hint:
        parts.append(hint)
    return "\n".join(parts)


@tool_meta(
    tool={
        "name": "browser_close",
        "strategic": "Closes the controlled browser.",
        "description": "Closes the browser and releases resources. Call when web work is done.",
        "arguments": {
            "description": {"type": "STRING", "description": "Why the browser is closed.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_close(self, **kwargs):
    mgr = getattr(self, "browser_manager", None)
    if mgr is not None:
        mgr.close()
        self.browser_manager = None
    if hasattr(self, "browser_factory"):
        self.browser_factory = None
    return "Browser closed."
