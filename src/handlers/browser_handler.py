"""
Handles the browser mode: an iterative agent loop over Playwright tools.

Same shape as TerminalHandler (thought -> single action -> observe),
but the tools drive a real Chromium window instead of a shell.
No desktop screenshots are involved: grounding comes from the DOM snapshot.

The loop mechanics (states, single-step plan -> execute -> observe cycle,
pause/resume/stop, user communication) live in BaseAgentHandler; here only
browser specifics remain: prompts, Playwright manager, result rendering.
"""
import json
import logging
import platform

from src.computer.computer import Computer
from src.executor.executor import Executor

from .base_agent import BaseAgentHandler


class BrowserHandler(BaseAgentHandler):
    MODE_NAME = "browser"
    SCHEMA_NAME = "browser_tool_code"

    def __init__(self, controller, working_directory=None):
        super().__init__(controller)
        self.computer = Computer(mode='browser', working_directory=working_directory)
        self.os_info = f"{platform.system()} {platform.release()}"
        self.executor = Executor(self.computer, ui=None)

    # -- mode specifics -------------------------------------------------

    def handle_response_chunk(self, chunk):
        # Browser mode is non-streaming; chunks are ignored.
        pass

    def current_url(self) -> str:
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is not None and getattr(mgr, "_page", None) is not None:
                return mgr.url()
        except Exception:
            pass
        return "not open yet"

    def _browser_ctx(self) -> dict:
        """Execution context for the Executor (kept for parity)."""
        return {"shell_allowed": False}

    def _ensure_browser_manager(self):
        """Create and attach the shared BrowserManager on first use."""
        from src.browser.manager import BrowserManager
        mgr = getattr(self.computer, "browser_manager", None)
        if mgr is not None:
            return mgr
        settings = self.settings if isinstance(self.settings, dict) else {}
        browser_settings = settings.get("browser", {}) or {}
        mgr = BrowserManager(
            headless=bool(browser_settings.get("headless", False)),
            profile_dir=browser_settings.get("profile_dir", "cache/browser_profile"),
            timeout_ms=int(browser_settings.get("default_timeout_ms", 30000) or 30000),
            block_ads=bool(browser_settings.get("block_ads", True)),
        )
        self.computer.browser_manager = mgr
        # The browser_* tools also look for a factory on the Computer.
        self.computer.browser_factory = lambda: mgr
        return mgr

    def _cleanup_task(self):
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is not None:
                mgr.close()
                self.computer.browser_manager = None
        except Exception:
            pass

    def build_prompts(self):
        system_prompt, user_prompt = self.brain.get_browser_prompts(
            user_request=self.original_user_request,
            os_info=self.os_info,
            history=self.command_history,
            current_url=self.current_url(),
        )
        return system_prompt, user_prompt

    def _request_attachments(self) -> list:
        return self._consume_pending_screenshot()

    def _consume_pending_screenshot(self) -> list:
        """Attaches the latest browser_screenshot (once) to the next request."""
        try:
            mgr = getattr(self.computer, "browser_manager", None)
            if mgr is None or not hasattr(mgr, "consume_screenshot_path"):
                return []
            path = mgr.consume_screenshot_path()
            if not path:
                return []
            with open(path, "rb") as f:
                logging.info(f"Attaching browser screenshot to next request: {path}")
                return [f.read()]
        except Exception as e:
            logging.warning(f"Could not attach browser screenshot: {e}")
            return []

    def execute_action(self, action: dict) -> str:
        action_name = action.get('name')
        action_args = action.get('arguments') or {}
        if not isinstance(action_args, dict):
            action_args = {}

        self.ui.add_message(f"<b>Executing:</b> {action_name}")
        self.ui.add_message({
            'type': 'collapsible',
            'title': "Arguments",
            'content': json.dumps(action_args, indent=2, ensure_ascii=False),
        })

        # Lazy browser start: tools expect computer.browser_manager/factory.
        if action_name.startswith("browser_") and action_name != "browser_close":
            try:
                self._ensure_browser_manager()
            except Exception as e:
                logging.error(f"Failed to start browser: {e}", exc_info=True)
                return f"**Tool Result:** Error: could not start the browser: {e}"

        result = self.executor.execute(
            {"name": action_name, "arguments": action_args}, self._browser_ctx())

        if not result.ok:
            return f"**Tool Result:** Error: {result.error}"

        output = result.output_text or "(ok, no output)"
        self.ui.add_message({
            'type': 'collapsible',
            'title': f"Browser Result: {action_name}",
            'content': output[-4000:],
        })
        return f"**Tool Result:**\n```\n{output}\n```"
