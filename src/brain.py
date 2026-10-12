"""
This module is responsible for making all intellectual decisions.
It handles loading system prompts from files. The parsing of LLM responses
is now handled by the VisionHandler using a centralized validator.
Prompts are loaded lazily into memory when first requested.
"""
import logging
import json
from typing import Any
from src.computer.schemas.terminal import generate_response_formats_dict


class Brain:
    # Static section appended to the strategic planning prompt only when
    # shell commands are enabled for Vision. Kept here (not in the .txt) so
    # the base prompt file stays backend-agnostic.
    SHELL_SECTION = """
### Shell and File Actions (enabled)

You additionally have shell/file tools (`execute_shell`, `list_directory`, `read_files`, `write_file`).
Rules for using them:

1.  **When to use shell:** ONLY when the UI path is impossible or clearly worse (e.g., listing many files, reading file content, running a script). If the target element is visible on screen, prefer the UI action — it is verifiable. Do NOT route around the UI out of convenience. Conversely, for console commands prefer `execute_shell` (silent, returns output as text) over opening a visible terminal window — unless the user explicitly asked to SEE the process.
    Decide by the NATURE of the need, not by habit:
    - Data question ("show", "check", "which", "list", "what is the last...") → terminal FIRST (`execute_shell`), answer with the result text. Opening windows to LOOK at data is wasteful.
    - OS/file state changes with a CLI equivalent (create folders/files, read content, query settings, services) → terminal.
    - App-specific GUI with no CLI (web forms, dialogs, drag-and-drop, context menus, buttons) → visual.
    - The user explicitly wants to SEE it ("open ... so I can see", "show me") → visual.
    - Ambiguous → terminal first (it is verified instantly, without screenshots); escalate to visual only if the command cannot deliver.
2.  **Working directory:** All relative paths resolve against the Task Working Directory stated in System Information. Prefer relative paths.
    **NEVER hardcode user profile paths** (like `C:\\Users\\...\\Desktop`): folder layouts differ per user and you do not know whose screen this is. The user speaks in human terms ("рабочий стол", "documents", "downloads") and never gives paths — you map those words onto portable tokens expanded on the real machine: `{DESKTOP}` (the desktop the user sees), `{HOME}`, `{WORKDIR}`, `{DOCUMENTS}`, `{DOWNLOADS}`.
    Example: `New-Item -ItemType Directory -Path '{DESKTOP}\\LogosTest' -Force`.
    If the path itself is needed as DATA for later steps (e.g. to type it into a Save dialog or to verify it), make the FIRST task resolve it via the runtime environment (PowerShell: `[Environment]::GetFolderPath('Desktop')`; cmd: `echo %USERPROFILE%\\Desktop`) — its output stays in history for the following tasks. Do this only when the concrete path is truly required downstream; otherwise the tokens suffice.
    NEVER type channel prefixes such as `shell:` as literal text via `write_text` — to run a command, use the `execute_shell` tool.
3.  **Quoting:** On Windows commands run via cmd.exe: write plain cmd syntax (`mkdir`, `dir`) or a simple `powershell -Command "..."` without nested quotes. The executor unwraps one PowerShell layer and fixes JSON-escaped quotes, but deeply nested quoting still breaks — keep commands simple.
4.  **No interactive commands:** NEVER run editors, pagers, prompts or anything awaiting input (`vim`, `nano`, `more`, `ssh` without batch flags, installers asking questions). The command must terminate on its own within 60 seconds.
5.  **Textual contract:** Every shell/file task MUST fill both `expected_outcome` (human-readable) and `expected_output` (concrete check, e.g. "STDOUT contains 'Done' and EXIT CODE: 0"). Such tasks are judged by command output only — no screenshots are taken.
6.  **One state change per task** applies to shell actions exactly as to clicks.
"""

    def __init__(self, settings_manager=None):
        self._response_formats_cache = None
        self._cache = {}
        self._settings_manager = settings_manager

    def is_shell_allowed_in_vision(self) -> bool:
        """Single source of truth for the shell-in-Vision flag."""
        try:
            if self._settings_manager is not None:
                vision = self._settings_manager.get("vision", {}) or {}
                return bool(vision.get("allow_shell_commands", False))
        except Exception:
            pass
        return False

    def get_response_formats(self):
        """Lazily loads response formats from the cache file."""
        if self._response_formats_cache is None:
            try:
                # First, try to load the generated formats from the terminal schemas directly
                self._response_formats_cache = generate_response_formats_dict()
            except Exception as e:
                logging.error(f"Could not generate response formats from schemas: {e}")
                # Fallback to the old cache file method if generation fails
                try:
                    with open("cache/terminal/response_formats.json", encoding="utf-8") as f:
                        self._response_formats_cache = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError) as e_cache:
                    logging.error(f"Critical error: Could not load or parse response_format.json as a fallback. {e_cache}")
                    self._response_formats_cache = {}
        return self._response_formats_cache

    def _load_prompt_template(self, filepath: str) -> str:
        """A helper function to load a prompt file from a given path."""
        try:
            with open(filepath, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            error_msg = f"{filepath} not found! This is a critical error."
            logging.error(error_msg)
            return f"Error: Prompt template not found at {filepath}."

    def _get_cached_prompt(self, key: str, filepath: str) -> str:
        """Lazily loads a prompt from a file and caches it."""
        if key not in self._cache:
            self._cache[key] = self._load_prompt_template(filepath)
        return self._cache[key]

    # --- Template Getters ---
    def get_strategic_tools(self): return self._get_vision_tools_text('strategic_tools', 'cache/vision/strategic_tools.txt')
    def get_tools_prompt(self): return self._get_vision_tools_text('tools_prompt', 'cache/vision/tools.txt')

    @staticmethod
    def _strip_shell_tools(text: str) -> str:
        """Remove shell/file tool lines so a disabled flag means the model
        never sees them (prompt and toolset cannot diverge)."""
        try:
            from src.computer.modes.vision import SHELL_TOOL_NAMES
        except Exception:
            return text
        kept = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- `"):
                name = stripped[3:].split("(", 1)[0].split("`", 1)[0]
                if name in SHELL_TOOL_NAMES:
                    continue
            kept.append(line)
        return "\n".join(kept)

    def _get_vision_tools_text(self, key: str, filepath: str) -> str:
        text = self._get_cached_prompt(key, filepath)
        if not self.is_shell_allowed_in_vision():
            text = self._strip_shell_tools(text)
        return text
    def get_strategic_planning_system_template(self): return self._get_cached_prompt('strategic_planning_system', 'prompts/vision/strategic_planning_system.txt')
    def get_strategic_planning_user_template(self): return self._get_cached_prompt('strategic_planning_user', 'prompts/vision/strategic_planning_user.txt')
    def get_evaluation_system_template(self): return self._get_cached_prompt('evaluation_system', 'prompts/vision/evaluation_system.txt')
    def get_evaluation_user_template(self): return self._get_cached_prompt('evaluation_user', 'prompts/vision/evaluation_user.txt')
    def get_error_explanation_system_template(self): return self._get_cached_prompt('error_explanation_system', 'prompts/vision/error_explanation_system.txt')
    def get_error_explanation_user_template(self): return self._get_cached_prompt('error_explanation_user', 'prompts/vision/error_explanation_user.txt')
    def get_wait_origin_system_template(self): return self._get_cached_prompt('wait_origin_system', 'prompts/vision/wait_origin_system.txt')
    def get_wait_origin_user_template(self): return self._get_cached_prompt('wait_origin_user', 'prompts/vision/wait_origin_user.txt')
    def get_wait_check_system_template(self): return self._get_cached_prompt('wait_check_system', 'prompts/vision/wait_check_system.txt')
    def get_wait_check_user_template(self): return self._get_cached_prompt('wait_check_user', 'prompts/vision/wait_check_user.txt')

    # --- Terminal Prompts ---
    def get_terminal_system_template(self): return self._get_cached_prompt('terminal_system', 'prompts/terminal/system.txt')
    def get_terminal_user_template(self): return self._get_cached_prompt('terminal_user', 'prompts/terminal/user.txt')
    def get_terminal_tools(self): return self._get_cached_prompt('terminal_tools', 'cache/terminal/tools.txt')
    # -------------------------------------------------

    # --- Browser Prompts ---
    def get_browser_system_template(self): return self._get_cached_prompt('browser_system', 'prompts/browser/system.txt')
    def get_browser_user_template(self): return self._get_cached_prompt('browser_user', 'prompts/browser/user.txt')
    def get_browser_tools(self): return self._get_cached_prompt('browser_tools', 'cache/browser/tools.txt')
    # -------------------------------------------------

    def _format_prompt_with_schema(self, prompt_template: str, schema_name: str, replacements: dict = None) -> str:
        """Helper to format a prompt with instructions and example from RESPONSE_FORMATS."""
        response_formats = self.get_response_formats()
        if not response_formats:
            logging.error("RESPONSE_FORMATS is not loaded. Cannot format prompt.")
            return prompt_template

        schema_info = response_formats.get(schema_name, {})
        instructions = schema_info.get("instructions", "")
        example = schema_info.get("example", "")

        prompt = prompt_template.replace("{response_format_instructions}", instructions)
        prompt = prompt.replace("{response_format_example}", example)

        if replacements:
            for key, value in replacements.items():
                prompt = prompt.replace(f"{{{key}}}", str(value))

        return prompt

    def _build_context_block(
        self,
        user_request: str | None = None,
        original_request: str | None = None,
        plan_status_str: str | None = None,
        goal_description: str | None = None,
        performed_task: dict[str, Any] | None = None,
        executed_actions: list[dict[str, Any]] | None = None,
        error_message: str | None = None,
        screen_context: str | None = None,
        post_action_context: str | None = None,
        screen_resolution: str | None = None,
        os_info: str | None = None,
        installed_applications: list[str] | None = None,
        history: list[dict[str, str]] | None = None,
        replacements: dict[str, Any] | None = None,
        working_directory: str | None = None,
    ) -> dict[str, str]:
        """
        Centrally builds the context block strings for user prompts.
        """
        context = {}
        context['user_request'] = user_request or "No user request provided."
        context['original_request'] = original_request or "No original request provided."
        context['plan_status_str'] = plan_status_str or "No plan available."
        context['goal_description'] = goal_description or "No current goal specified."

        if performed_task:
            context['task_description'] = performed_task.get('task_description', 'No description provided.')
            context['expected_outcome'] = performed_task.get('expected_outcome', 'No expected outcome provided.')
        else:
            context['task_description'] = "No task was performed."
            context['expected_outcome'] = "No expected outcome specified."

        actions_executed_str = json.dumps(executed_actions, indent=2) if executed_actions else "[]"
        context['actions_executed'] = actions_executed_str
        context['task_error_context'] = f"An error occurred: {error_message}" if error_message else "No errors were reported during execution."
        context['screen_context'] = screen_context or "No screen analysis available."
        context['post_action_context'] = post_action_context or "No post-action screen analysis available."
        context['screen_resolution'] = screen_resolution or "Unknown"
        context['os_info'] = os_info or "Unknown"
        context['installed_applications'] = "\n".join([f"- {app}" for app in installed_applications]) if installed_applications else "No application list provided."
        context['history_str'] = "\n".join([f"- {item['role']}: {item['content']}" for item in history]) if history else "No history available."
        context['working_directory'] = working_directory or "Unknown (use absolute paths from the request context)."

        # --- New context for re-planning ---
        if replacements and replacements.get('is_replan'):
            context['replan_context_block'] = self._build_replan_context_block(
                user_request, plan_status_str, replacements.get('failure_context')
            )
        else:
            context['replan_context_block'] = "" # Ensure the placeholder is replaced with nothing if not re-planning

        return context

    def _build_replan_context_block(self, user_request, plan_status, failure_context):
        """Builds the specific text block for re-planning scenarios."""
        replan_template = self._get_cached_prompt('strategic_planning_replan_context', 'prompts/vision/strategic_planning_replan_context.txt')
        return self._apply_replacements(replan_template, {
            "user_request": user_request,
            "plan_status": plan_status,
            "failure_context": failure_context
        })

    def _apply_replacements(self, template: str, replacements: dict[str, str]) -> str:
        """Replaces all placeholders in a template with values from a dictionary."""
        for key, value in replacements.items():
            template = template.replace(f"{{{key}}}", str(value))
        return template

    def get_strategic_prompts(self, user_request: str, screen_context: str, installed_applications: list = None, screen_resolution: str = "Unknown", os_info: str = "Unknown", is_replan: bool = False, plan_status: str = None, failure_context: str = None, working_directory: str | None = None) -> tuple[str, str]:
        """Formats the system and user prompts for the strategic planning phase."""
        system_template = self.get_strategic_planning_system_template()
        user_template = self.get_strategic_planning_user_template()

        if "template not found" in system_template or "template not found" in user_template:
            return None, None

        system_prompt = self._format_prompt_with_schema(system_template, "strategic_plan", {
            "strategic_tools": self.get_strategic_tools(),
            "shell_section": self.SHELL_SECTION if self.is_shell_allowed_in_vision() else "",
        })

        # Build context with re-planning info if applicable
        context_replacements = {
            'is_replan': is_replan,
            'failure_context': failure_context
        }
        context = self._build_context_block(
            user_request=user_request,
            plan_status_str=plan_status,
            screen_context=screen_context,
            screen_resolution=screen_resolution,
            os_info=os_info,
            installed_applications=installed_applications,
            replacements=context_replacements,
            working_directory=working_directory,
        )
        user_prompt = self._apply_replacements(user_template, context)

        return system_prompt, user_prompt

    def get_evaluation_prompts(self, original_request: str, plan_status_str: str, history: list, performed_task: dict, executed_actions: list, post_action_context: str, error_message: str = None, correction_attempts: int = 0, max_correction_attempts: int = 3, os_info: str = "Unknown", action_result: str | None = None, working_directory: str | None = None) -> tuple[str, str]:
        """Generates the system and user prompts for the evaluation phase."""
        system_template = self.get_evaluation_system_template()
        user_template = self.get_evaluation_user_template()

        if "template not found" in system_template or "template not found" in user_template:
            return None, None

        system_prompt = self._format_prompt_with_schema(system_template, "evaluation", {
            "available_tools": self.get_tools_prompt(),
            "correction_attempts": correction_attempts,
            "max_correction_attempts": max_correction_attempts
        })

        context = self._build_context_block(
            original_request=original_request,
            plan_status_str=plan_status_str,
            history=history,
            performed_task=performed_task,
            executed_actions=executed_actions,
            post_action_context=post_action_context,
            error_message=error_message,
            os_info=os_info,
            working_directory=working_directory,
        )

        # Dynamically create the clipboard content block if a result was returned
        if action_result is not None:
            context['clipboard_content_block'] = f"### Clipboard Content ###\n{action_result}"
        else:
            context['clipboard_content_block'] = ""

        user_prompt = self._apply_replacements(user_template, context)

        return system_prompt, user_prompt

    def get_error_explanation_prompts(self, original_request: str, last_action: dict, reason: str) -> tuple[str, str]:
        """Generates the system and user prompts for the LLM to explain why it got stuck."""
        user_template = self.get_error_explanation_user_template()
        system_template = self.get_error_explanation_system_template()

        if "template not found" in user_template or "template not found" in system_template:
            return None, None

        system_prompt = self._format_prompt_with_schema(system_template, "error_explanation")

        user_prompt = self._apply_replacements(user_template, {
            "original_request": original_request,
            "last_action": json.dumps(last_action, indent=2),
            "reason": reason
        })

        return system_prompt, user_prompt

    def get_wait_origin_prompts(self, recent_history_str: str) -> tuple[str, str]:
        """Formats the system and user prompts for the wait origin detective."""
        user_template = self.get_wait_origin_user_template()
        system_template = self.get_wait_origin_system_template()

        if "template not found" in user_template or "template not found" in system_template:
            return None, None

        system_prompt = self._format_prompt_with_schema(system_template, "wait_origin_result")
        user_prompt = user_template.replace("{recent_history_str}", recent_history_str)

        return system_prompt, user_prompt

    def get_wait_check_prompts(self, wait_context: str, new_screen_context: str) -> tuple[str, str]:
        """Formats the system and user prompts for the wait_element check."""
        user_template = self.get_wait_check_user_template()
        system_template = self.get_wait_check_system_template()

        if "template not found" in user_template or "template not found" in system_template:
            return None, None

        system_prompt = self._format_prompt_with_schema(system_template, "wait_check_result")
        user_prompt = self._apply_replacements(user_template, {
            "wait_context": wait_context,
            "new_screen_context": new_screen_context
        })

        return system_prompt, user_prompt

    def get_terminal_prompts(self, user_request: str, os_info: str, history: list) -> tuple[str, str]:
        """Formats the system and user prompts for the terminal agent."""
        system_template = self.get_terminal_system_template()
        user_template = self.get_terminal_user_template()

        if "template not found" in system_template or "template not found" in user_template:
            return None, None

        # Manually inject the specific response format for terminal_tool_code
        response_formats = self.get_response_formats()
        schema_info = response_formats.get("terminal_tool_code", {})
        instructions = schema_info.get("instructions", "")
        example = schema_info.get("example", "")

        # Build the full instruction block
        response_format_block = f"{instructions}\n\n{example}"

        system_prompt = self._apply_replacements(system_template, {
            "available_tools": self.get_terminal_tools(),
            "response_format_instructions": response_format_block
        })

        history_str = "\n".join(history) if history else "No commands executed yet."

        # OS-aware command examples
        is_windows = "windows" in os_info.lower()
        if is_windows:
            current_directory_prompt = "(Use the `cd` command to see the current directory)"
            directory_listing_prompt = "(Use the `dir` command to see the directory listing)"
            directory_listing_command = "dir"
        else:
            current_directory_prompt = "(Use the `pwd` command to see the current directory)"
            directory_listing_prompt = "(Use the `ls -F` command to see the directory listing)"
            directory_listing_command = "ls -F"

        user_prompt = self._apply_replacements(user_template, {
                "user_request": user_request,
                "os_info": os_info,
                "current_directory": current_directory_prompt,
                "directory_listing_command": directory_listing_command,
                "directory_listing": directory_listing_prompt,
                "history_str": history_str
            })

        return system_prompt, user_prompt

    # --- Agent history compaction (Settings → Context) ---

    @staticmethod
    def _compact_history_entry(entry: str, max_chars: int, trim_tb: bool) -> str:
        text = entry if isinstance(entry, str) else str(entry)
        if trim_tb and "Traceback (most recent call last):" in text:
            lines = text.splitlines()
            if len(lines) > 10:
                kept = lines[:3] + [f"[... {len(lines) - 8} traceback lines trimmed ...]"] + lines[-5:]
                text = "\n".join(kept)
        if max_chars > 0 and len(text) > max_chars:
            head = max_chars // 2
            text = text[:head] + f"\n...[{len(text) - max_chars} chars trimmed]...\n" + text[-(max_chars - head):]
        return text

    @staticmethod
    def _dedup_snapshot_entries(entries: list) -> list:
        out, last_snapshot = [], None
        for entry in entries:
            if (isinstance(entry, str) and entry.startswith("**Tool Result:**")
                    and "Interactive elements:" in entry):
                if entry == last_snapshot:
                    out.append("**Tool Result:** (snapshot unchanged since previous step)")
                    continue
                last_snapshot = entry
            out.append(entry)
        return out

    @staticmethod
    def _condense_history_entry(entry: str, limit: int = 160) -> str:
        lines = (entry if isinstance(entry, str) else str(entry)).strip().splitlines()
        first_line = lines[0] if lines else ""
        if len(first_line) > limit:
            first_line = first_line[:limit] + "..."
        return f"[older step] {first_line}"

    def compact_browser_history(self, history: list) -> list:
        """Optionally compacts browser agent history to cut transmitted context.

        All options default to no-op (full history is sent). The last N steps
        stay verbatim; older steps condense to one line each. Facts and error
        outcomes are preserved — only bulk (repeated snapshots, long
        tracebacks, huge texts) is cut. Configure in Settings → Context.
        """
        try:
            cfg = {}
            if self._settings_manager is not None:
                cfg = self._settings_manager.get("context", {}) or {}
            keep_last = int(cfg.get("history_keep_last_n", 0) or 0)
            max_chars = int(cfg.get("history_max_result_chars", 0) or 0)
            dedup = bool(cfg.get("history_dedup_snapshots", False))
            trim_tb = bool(cfg.get("history_trim_tracebacks", False))
        except Exception:
            return history
        if not history or (keep_last <= 0 and max_chars <= 0 and not dedup and not trim_tb):
            return history
        compacted = [self._compact_history_entry(e, max_chars, trim_tb) for e in history]
        if dedup:
            compacted = self._dedup_snapshot_entries(compacted)
        if keep_last > 0 and len(compacted) > keep_last:
            head, tail = compacted[:-keep_last], compacted[-keep_last:]
            compacted = [self._condense_history_entry(e) for e in head] + tail
        return compacted

    def get_browser_response_formats(self):
        """Loads browser response formats from the generated cache file."""
        try:
            with open("cache/browser/response_formats.json", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logging.error(f"Could not load browser response formats: {e}")
            return {}

    def get_browser_prompts(self, user_request: str, os_info: str, history: list, current_url: str = "not open yet") -> tuple[str, str]:
        """Formats the system and user prompts for the browser agent."""
        system_template = self.get_browser_system_template()
        user_template = self.get_browser_user_template()

        if "template not found" in system_template or "template not found" in user_template:
            return None, None

        response_formats = self.get_browser_response_formats()
        schema_info = response_formats.get("browser_tool_code", {})
        instructions = schema_info.get("instructions", "")
        example = schema_info.get("example", "")
        response_format_block = f"{instructions}\n\n{example}"

        system_prompt = self._apply_replacements(system_template, {
            "available_tools": self.get_browser_tools(),
            "response_format_instructions": response_format_block
        })

        compacted = self.compact_browser_history(history) if history else []
        history_str = "\n".join(compacted) if compacted else "No commands executed yet."

        user_prompt = self._apply_replacements(user_template, {
                "user_request": user_request,
                "os_info": os_info,
                "current_url": current_url,
                "history_str": history_str
            })

        return system_prompt, user_prompt

    def get_self_correction_prompts(self, original_request: str, schema_example: str, validation_error: str, malformed_response: str) -> tuple[str, str]:
        """Formats the system and user prompts for the self-correction mechanism."""
        system_template = self._get_cached_prompt('self_correction_system', 'prompts/correction/system.txt')
        user_template = self._get_cached_prompt('self_correction_user', 'prompts/correction/user.txt')

        if "template not found" in system_template or "template not found" in user_template:
            return None, None

        system_prompt = system_template
        user_prompt = self._apply_replacements(user_template, {
            "original_request": original_request,
            "schema_example": schema_example,
            "validation_error": validation_error,
            "malformed_response": malformed_response
        })

        return system_prompt, user_prompt

    def get_web_search_prompts(self, query: str, search_results: str) -> tuple[str, str]:
        """Formats the system and user prompts for summarizing web search results."""
        system_template = self._get_cached_prompt('web_search_system', 'prompts/terminal/web_search_system.txt')
        user_template = self._get_cached_prompt('web_search_user', 'prompts/terminal/web_search_user.txt')

        if "template not found" in system_template or "template not found" in user_template:
            return None, None

        system_prompt = system_template
        user_prompt = self._apply_replacements(user_template, {
            "query": query,
            "search_results": search_results
        })

        return system_prompt, user_prompt

    def get_web_fetch_prompts(self, prompt: str, page_content: str) -> tuple[str, str]:
        """Formats the system and user prompts for processing fetched web page content."""
        system_template = self._get_cached_prompt('web_fetch_system', 'prompts/terminal/web_fetch_system.txt')
        user_template = self._get_cached_prompt('web_fetch_user', 'prompts/terminal/web_fetch_user.txt')

        if "template not found" in system_template or "template not found" in user_template:
            return None, None

        system_prompt = system_template
        user_prompt = self._apply_replacements(user_template, {
            "prompt": prompt,
            "page_content": page_content
        })

        return system_prompt, user_prompt

    def get_web_delegation_prompts(self, prompt: str) -> tuple[str, str]:
        """This method is deprecated and should not be used."""
        raise NotImplementedError("get_web_delegation_prompts is deprecated and no longer supported.")


