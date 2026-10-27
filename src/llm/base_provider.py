# src/llm/base_provider.py

import json
import logging
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any

# Correlation context for the single-log timeline. Set by whoever drives the
# call (llm_worker, CUA worker); read by the automatic base wrapper below.
# ContextVar = thread/task-local, so worker threads don't clash, and provider
# files stay untouched (no signature changes, no logger lines required).
log_ctx: ContextVar = ContextVar("llm_log_ctx", default=None)


def set_log_ctx(tag: str, turn: int = 0) -> None:
    log_ctx.set({"tag": tag, "turn": turn})


class BaseLLMProvider(ABC):
    """
    Abstract base class for all LLM providers.
    Defines the essential contract for interacting with different LLM APIs.

    Logging rule: provider files contain NO logger lines. Every provider
    call is journaled automatically by the wrapper installed in __init__
    (start / duration / exception) into the single app timeline. Authors
    of new providers get this for free — nothing to remember.
    """
    def __init__(self, provider_config: dict):
        self.provider_config = provider_config
        self.api_key = self.provider_config.get("api_key")
        self.model = self.provider_config.get("model")
        self.require_json_output = self.provider_config.get("require_json_output", False)
        self.last_usage = None
        self._install_auto_logging()

    def _record_usage(self, raw, *, merge: bool = False) -> None:
        """Store normalized token usage for the next worker signal."""
        from llm.usage import merge_usage, normalize_usage

        usage = normalize_usage(
            raw, type(self).get_provider_id(), self.model)
        if not usage:
            return
        self.last_usage = merge_usage(self.last_usage, usage) if merge else usage

    def _install_auto_logging(self):
        """Wraps both call paths with timing + error journaling.

        Runs once per instance; subclasses never touch it. The worker keeps
        logging semantic content (prompts/responses); this layer adds the
        mechanical envelope: who, how long, what blew up.
        """
        comm = logging.getLogger("llm_communication")
        me = type(self).__name__

        def _ctx():
            try:
                current = log_ctx.get()
                if isinstance(current, dict):
                    return dict(current)
            except Exception:
                pass
            return {"tag": "untagged", "turn": 0}

        orig_call = self._execute_get_response

        def logged_call(*args, **kwargs):
            ctx = _ctx()
            t0 = time.perf_counter()
            schema = kwargs.get(
                "schema_name", args[4] if len(args) > 4 else None)
            try:
                result = orig_call(*args, **kwargs)
                comm.info(json.dumps({
                    "direction": "PROVIDER-CALL",
                    "tag": ctx.get("tag"), "turn": ctx.get("turn"),
                    "provider": me, "model": self.model,
                    "mode": "structured",
                    "schema": schema,
                    "took_seconds": round(time.perf_counter() - t0, 2),
                    "ok": True,
                }, ensure_ascii=False))
                return result
            except Exception as e:
                comm.info(json.dumps({
                    "direction": "PROVIDER-CALL",
                    "tag": ctx.get("tag"), "turn": ctx.get("turn"),
                    "provider": me, "model": self.model,
                    "mode": "structured",
                    "schema": schema,
                    "took_seconds": round(time.perf_counter() - t0, 2),
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}"[:500],
                }, ensure_ascii=False))
                raise

        orig_stream = self.stream_response

        def logged_stream(*args, **kwargs):
            ctx = _ctx()
            t0 = time.perf_counter()
            chunks = 0
            chars = 0
            self.last_usage = None
            try:
                for chunk in orig_stream(*args, **kwargs):
                    chunks += 1
                    try:
                        chars += len(str(chunk.get("content", chunk)
                                         if isinstance(chunk, dict) else chunk))
                    except Exception:
                        pass
                    yield chunk
                comm.info(json.dumps({
                    "direction": "PROVIDER-CALL",
                    "tag": ctx.get("tag"), "turn": ctx.get("turn"),
                    "provider": me, "model": self.model,
                    "mode": "streaming",
                    "took_seconds": round(time.perf_counter() - t0, 2),
                    "ok": True, "chunks": chunks, "chars": chars,
                }, ensure_ascii=False))
            except Exception as e:
                comm.info(json.dumps({
                    "direction": "PROVIDER-CALL",
                    "tag": ctx.get("tag"), "turn": ctx.get("turn"),
                    "provider": me, "model": self.model,
                    "mode": "streaming",
                    "took_seconds": round(time.perf_counter() - t0, 2),
                    "ok": False, "chunks": chunks,
                    "error": f"{type(e).__name__}: {e}"[:500],
                }, ensure_ascii=False))
                raise

        self._execute_get_response = logged_call
        self.stream_response = logged_stream

        orig_cua = self.computer_use_turn

        def logged_cua(*args, **kwargs):
            ctx = _ctx()
            t0 = time.perf_counter()
            self.last_usage = None
            try:
                result = orig_cua(*args, **kwargs)
                comm.info(json.dumps({
                    "direction": "PROVIDER-CALL",
                    "tag": ctx.get("tag"), "turn": ctx.get("turn"),
                    "provider": me, "model": self.model,
                    "mode": "computer-use",
                    "took_seconds": round(time.perf_counter() - t0, 2),
                    "ok": True,
                    "actions": (result.get("actions")
                                if isinstance(result, dict) else None),
                }, ensure_ascii=False))
                return result
            except Exception as e:
                comm.info(json.dumps({
                    "direction": "PROVIDER-CALL",
                    "tag": ctx.get("tag"), "turn": ctx.get("turn"),
                    "provider": me, "model": self.model,
                    "mode": "computer-use",
                    "took_seconds": round(time.perf_counter() - t0, 2),
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}"[:500],
                }, ensure_ascii=False))
                raise

        self.computer_use_turn = logged_cua

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
        self.last_usage = None
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

    # --- Computer Use contract (optional; only providers with native support) ---

    def supports_computer_use(self) -> bool:
        """
        Whether this provider can drive a computer-use loop (native grounding +
        structured actions, e.g. OpenAI `computer` tool, Anthropic computer toolset).
        The agent loop itself lives in the handler (screenshots, action execution,
        safety approvals); the provider only implements one protocol turn.
        """
        return False

    def wants_first_screenshot(self) -> bool:
        """
        Whether the provider's first computer-use turn should carry an opening
        screenshot. OpenAI/Anthropic start blind (the model requests a shot via
        its own tool); Google's documented loop always sends one with the task.
        The worker injects it; providers that don't declare it never see it.
        """
        return False

    def computer_use_turn(
        self,
        task: str | None = None,
        call_id: str | None = None,
        screenshot_b64: str | None = None,
        previous_response_id: str | None = None,
        acknowledged_safety_checks: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        launch_apps: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Executes one computer-use turn.

        First turn: pass `task` (and optionally an opening screenshot).
        Following turns: pass `previous_response_id` + `outputs` answered
        from the previous turn (computer and/or function outputs).

        `launch_apps` is the user-selected Applications list (exact display
        names). When non-empty, providers expose a `launch_app` function
        tool restricted to those names; calls come back under `"launch"`.

        Every computer-use provider implements BOTH approval paths:
        native safety flags where the vendor supports them, and the
        explicit `ask_user`-style function tool as the universal fallback
        (the model calls it by meaning; the handler shows the dialog and
        waits, then resumes the same chain with the answer).

        Returns a dict:
        {
            "actions": [ ...structured actions, possibly [{"type": "screenshot"}...] ],
            "call_id": str | None,
            "response_id": str | None,
            "pending_safety_checks": [ {"id":..., "code":..., "message":...} ],
            "ask": [ {"call_id":..., "question":...} ],
            "launch": [ {"call_id":..., "app_name":...} ],
            "done_text": str | None   # final answer when the model stops calling
        }
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement computer use."
        )

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
