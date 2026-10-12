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


@tool_meta(
    tool={
        "name": "browser_goto",
        "strategic": "Opens a URL in the controlled browser (starts it if needed).",
        "description": "Navigates the controlled browser to the given URL and waits for the page to load.",
        "arguments": {
            "description": {"type": "STRING", "description": "What this navigation is for.", "required": True},
            "url": {"type": "STRING", "description": "Full URL including https://.", "required": True},
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
    return _with_dialog_note(mgr, f"Opened {url}. Page title: '{mgr.title()}'. Take a fresh browser_snapshot before acting.")


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
    if not elements:
        return "No interactive elements found on this page."
    lines = [f"[{e['ref']}] {e['role']}: {e['name'] or '(no label)'}" for e in elements]
    return "Interactive elements:\n" + "\n".join(lines)


@tool_meta(
    tool={
        "name": "browser_click",
        "strategic": "Clicks an element by its ref.",
        "description": "Clicks the element with the given ref from the latest snapshot.",
        "arguments": {
            "description": {"type": "STRING", "description": "What is being clicked.", "required": True},
            "ref": {"type": "INTEGER", "description": "Element ref from browser_snapshot.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_click(self, **kwargs):
    ref = kwargs.get("ref")
    if ref is None:
        raise ValueError("Action 'browser_click' failed: Missing required 'ref' argument. Take browser_snapshot first.")
    mgr = _manager(self)
    mgr.click(ref)
    return _with_dialog_note(mgr, f"Clicked element ref={ref}.")


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
        "description": "Captures a viewport screenshot ONLY when the current step cannot be solved from text (visual puzzle, image options, layout that text cannot convey). The image is attached to your next request automatically, exactly once. Do NOT use routinely: every image costs extra tokens.",
        "arguments": {
            "description": {"type": "STRING", "description": "Why the image is needed for this step.", "required": True},
        },
    },
    flags={"platform": "any", "mode": "browser"},
)
def browser_screenshot(self, **kwargs):
    mgr = _manager(self)
    path = mgr.capture_screenshot()
    return f"Screenshot saved to {path}. It will be attached to your next request automatically (once)."


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
    return "Browser closed."
