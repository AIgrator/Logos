from src.computer.computer import tool_meta


def _manager(self):
    mgr = getattr(self, "browser_manager", None)
    if mgr is None:
        raise ValueError(
            "Action failed: browser is not started. The plan must open the browser first "
            "(browser_goto starts it automatically)."
        )
    return mgr


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
    return f"Opened {url}. Page title: '{mgr.title()}'. Take a fresh browser_snapshot before acting."


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
    _manager(self).click(ref)
    return f"Clicked element ref={ref}."


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
    _manager(self).fill(ref, text)
    return f"Filled element ref={ref}."


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
    _manager(self).press(key)
    return f"Pressed {key}."


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
