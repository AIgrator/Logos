"""
This module is responsible for making all intellectual decisions.
It handles loading system prompts from files. The parsing of LLM responses
is now handled by the VisionHandler using a centralized validator.
Prompts are loaded lazily into memory when first requested.
"""
import logging
import json
from src.computer.schemas.terminal import generate_response_formats_dict


class Brain:
    def __init__(self):
        self._response_formats_cache = None
        self._cache = {}

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

    def _apply_replacements(self, template: str, replacements: dict[str, str]) -> str:
        """Replaces all placeholders in a template with values from a dictionary."""
        for key, value in replacements.items():
            template = template.replace(f"{{{key}}}", str(value))
        return template

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

    def get_terminal_prompts(self, user_request: str, os_info: str, history: list, available_skills: str = "") -> tuple[str, str]:
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
            "available_skills": available_skills or "No skills are installed.",
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

    def get_browser_response_formats(self):
        """Loads browser response formats from the generated cache file."""
        try:
            with open("cache/browser/response_formats.json", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logging.error(f"Could not load browser response formats: {e}")
            return {}

    def get_browser_prompts(self, user_request: str, os_info: str, history: list, current_url: str = "not open yet", available_skills: str = "") -> tuple[str, str]:
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
            "available_skills": available_skills or "No skills are installed.",
            "response_format_instructions": response_format_block
        })

        history_str = "\n".join(history) if history else "No commands executed yet."

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


