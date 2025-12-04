# Guide: Adding a New LLM Provider

The architecture has been completely redesigned for maximum modularity and ease of extension. Now, to add a new LLM provider, you only need to create **one file** that contains all its logic, configuration, and UI description. The system will automatically detect and integrate it on the next launch.

**Concept:** Each provider is a self-contained class, inherited from `BaseLLMProvider`, that "tells" the system about itself through a set of static methods and correctly handles errors.

---

## Steps to Add a New Provider (Example: `Groq`)

### 1. Create the Provider File

In the `src/llm/` directory, create a new file with the `_provider.py` suffix. For example, `src/llm/groq_provider.py`.

### 2. Implement the Provider Class

Inside the file, create a class inherited from `BaseLLMProvider` and implement its abstract methods.

-   **Abstract Static Methods (Configuration):** These methods describe your provider to the rest of the system.
-   **Abstract Instance Methods (Logic):** These methods implement direct API interaction.

### 3. Implement Error Handling (Mandatory Step)

For integration with the assistant's intelligent error handling system, your provider **must** intercept errors and correctly **classify** them, "repackaging" them into one of two custom exceptions: `FatalError` or `RetryableError`.

-   **`FatalError`**: Use for **unrecoverable** errors that will not succeed on retry. These are client-side errors, such as an invalid API key, quota exhaustion, or an incorrect request (typically `4xx` errors).
-   **`RetryableError`**: Use for **temporary** errors that might disappear on a retry. These are server-side issues (`5xx`), network connection failures, or timeouts.

This is critically important as `LLMWorker` will use these exception types to decide whether to retry the request or immediately report the error to the user.

#### Approach to Error Classification

-   **If the provider has an SDK:** Catch library-specific exceptions (e.g., `AuthenticationError`, `RateLimitError`) and convert them into `FatalError` or `RetryableError`.
-   **If you are working directly with the API (e.g., via `requests`):** Analyze the HTTP status code. `4xx` codes should become `FatalError`, while `5xx` codes and network exceptions should become `RetryableError`. This approach is more universal.

### 4. Full Implementation Example

Here is a complete, minimal example of the `generic_provider.py` file structure, which uses the `requests` library and demonstrates a universal approach to error handling.

```python
# src/llm/generic_provider.py
import logging
import requests # <-- Use requests for universality
from .base_provider import BaseLLMProvider
from .exceptions import FatalError, RetryableError # <-- 1. Import our exceptions
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class GenericProvider(BaseLLMProvider):
    """
    LLM provider for a generic service.
    """
    # --- System Configuration ---

    @staticmethod
    def get_provider_id() -> str:
        return "generic"

    @staticmethod
    def get_display_name() -> str:
        return "Generic"

    @staticmethod
    def get_default_settings() -> Dict[str, Any]:
        return { "model": "some-model-v1", "api_key": "" }

    @staticmethod
    def get_ui_schema() -> Dict[str, Any]:
        return {
            "api_key": {"type": "password", "label": "API Key"},
            "model": {"type": "dropdown", "label": "Model", "options_source": "models.json"}
        }

    @staticmethod
    def get_provider_config() -> Dict[str, Any]:
        return { "supports_cancellation": False }

    # --- Initialization and API Interaction Logic ---

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        self.session = requests.Session()
        self.api_url = "https://api.generic-provider.com/v1/chat"

    def _execute_get_response(
        self,
        prompt: str,
        attachments: Optional[List[Any]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        schema_name: Optional[str] = None
    ) -> Dict[str, Any]:
        # 2. Wrap the API call in try...except
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            payload = {"model": self.model, "prompt": prompt} # Simplified payload
            
            response = self.session.post(self.api_url, headers=headers, json=payload, timeout=120)
            response.raise_for_status() # <-- Raises HTTPError for 4xx/5xx codes

            response_data = response.json()
            # ... your response processing logic ...
            return {"text": response_data.get("text", ""), "tool_calls": {}}

        # 3. Catch and classify errors
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if 400 <= status_code < 500:
                # Client error (4xx) -> FatalError
                raise FatalError(f"Generic provider client error ({status_code}): {e.response.text}") from e
            elif 500 <= status_code < 600:
                # Server error (5xx) -> RetryableError
                raise RetryableError(f"Generic provider server error ({status_code}): {e.response.text}") from e
            else:
                # Other HTTP errors
                raise FatalError(f"Generic provider unexpected HTTP error ({status_code}): {e.response.text}") from e
        
        except requests.exceptions.RequestException as e:
            # Network errors (DNS, timeout, no connection) -> RetryableError
            raise RetryableError(f"Generic provider network error: {e}") from e

        except Exception as e:
            logger.error(f"An unexpected error occurred in Generic provider: {e}", exc_info=True)
            # Unexpected errors are considered fatal by default
            raise FatalError(f"An unexpected error in Generic provider: {e}") from e

    def stream_response(
        self,
        prompt: str,
        attachments: Optional[List[Any]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        callbacks=None
    ):
        # Error handling logic for streaming requests will be similar
        try:
            # ... your streaming request code ...
            pass
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if 400 <= status_code < 500:
                raise FatalError(f"Generic provider stream client error ({status_code}): {e.response.text}") from e
            elif 500 <= status_code < 600:
                raise RetryableError(f"Generic provider stream server error ({status_code}): {e.response.text}") from e
            else:
                raise FatalError(f"Generic provider stream unexpected HTTP error ({status_code}): {e.response.text}") from e
        except requests.exceptions.RequestException as e:
            raise RetryableError(f"Generic provider stream network error: {e}") from e
        except Exception as e:
            logger.error(f"An unexpected error occurred in Generic provider stream: {e}", exc_info=True)
            raise FatalError(f"An unexpected error in Generic provider stream: {e}") from e

```

### 5. That's it!

No more steps are needed. On the next launch of the application:
- The mechanism in `src/llm/__init__.py` will automatically detect your `GroqProvider`.
- `SettingsManager` will read its static configuration and create default settings.
- `LLMProviderWidget` in the settings UI will dynamically generate the necessary fields (`API Key`, `Model`) based on `get_ui_schema`.
- `LLMWorker` will correctly handle errors generated by your provider.

This architecture completely eliminates the need to edit multiple files and makes adding new providers fast and safe.