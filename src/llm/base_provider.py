# src/llm/base_provider.py

from abc import ABC, abstractmethod
from typing import Any

class BaseLLMProvider(ABC):
    """
    Abstract base class for all LLM providers.
    Defines the essential contract for interacting with different LLM APIs.
    """
    def __init__(self, provider_config: dict):
        self.provider_config = provider_config
        self.api_key = self.provider_config.get("api_key")
        self.model = self.provider_config.get("model")
        self.require_json_output = self.provider_config.get("require_json_output", False)

    # These methods are optional for providers to implement if they use
    # the default stream_response method.
    def get_request_url(self): return ""
    def prepare_request_headers(self): return {}
    def prepare_request_body(self, prompt: str, attachments: list, system_prompt: str = None, history: list = None): return {}
    def parse_response(self, chunk: str): return ""

    @abstractmethod
    def _execute_get_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        schema_name: str | None = None
    ) -> dict[str, Any]:
        """
        Provider-specific implementation for sending a non-streaming request.
        This method is called by the public `get_response` wrapper.
        """
        pass

    def get_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        schema_name: str | None = None
    ) -> dict[str, Any]:
        """
        Public wrapper for making a non-streaming request to the LLM.
        It ensures that the response always contains a structured tool_calls object.
        """
        import logging
        response_dict = self._execute_get_response(
            prompt, attachments, system_prompt, history, schema_name
        )

        tool_calls = response_dict.get("tool_calls")

        # Centralized validation: If the provider is *supposed* to return a JSON object
        # but fails to do so, replace the empty tool_calls with a structured error.
        if self.require_json_output and (not tool_calls or not isinstance(tool_calls, dict)):
            logging.warning(
                "Provider configured to require JSON output returned no valid tool_calls object. "
                "Replacing with a structured error_explanation."
            )
            error_tool_call = {
                "type": "error_explanation",
                "payload": {
                    "error_explanation": "The model did not return a valid action or evaluation. It may be stuck or confused."
                }
            }
            response_dict["tool_calls"] = error_tool_call

        return response_dict

    def web_search(self, query: str) -> str | None:
        """
        Performs a web search if the provider supports it natively.
        This method is intended to be overridden by providers that have
        built-in search capabilities.

        Args:
            query: The search query string.

        Returns:
            The search result as a string, or None if the provider
            does not support this feature.
        """
        return None

    @abstractmethod
    def stream_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        callbacks=None
    ):
        """
        Sends a streaming request to the LLM, typically for chat responses.
        This method should yield text chunks as they are received.
        """
        pass

    # --- Dynamic Configuration Contract ---

    @staticmethod
    @abstractmethod
    def get_provider_id() -> str:
        """
        Returns the unique identifier for this provider (e.g., 'google_gemini').
        This ID is used as the key in settings.json.
        """
        pass

    @staticmethod
    @abstractmethod
    def get_display_name() -> str:
        """
        Returns the user-friendly name of the provider for the UI (e.g., 'Google Gemini').
        """
        pass

    @staticmethod
    @abstractmethod
    def get_default_settings() -> dict[str, Any]:
        """
        Returns a dictionary of the default settings for this provider.
        These values will be used to populate settings.json for the first time.
        Example: {'model': 'gemini-1.5-flash', 'api_key': ''}
        """
        pass

    @staticmethod
    @abstractmethod
    def get_ui_schema() -> dict[str, Any]:
        """
        Returns a schema defining the UI elements for the settings page.
        The schema dictates the type, label, and options for each setting.
        Example: {
            'api_key': {'type': 'password', 'label': 'API Key'},
            'model': {'type': 'dropdown', 'label': 'Model', 'options_source': 'models.json'}
        }
        """
        pass

    @staticmethod
    def get_provider_config() -> dict[str, Any]:
        """
        Returns a dictionary of the static configuration for this provider.
        This includes information not directly edited by the user but needed by the system.
        Example: {'require_json_output': True, 'supports_cancellation': False}
        """
        # This method is optional and can be overridden if needed.
        return {}
