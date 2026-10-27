"""User-defined OpenAI-compatible providers.

People may run models behind an OpenAI-compatible Chat Completions endpoint
(LM Studio, Ollama, llama.cpp, a gateway, a company proxy). Definitions live
in settings.json under "custom_providers"; each definition becomes a real
provider class in llm.PROVIDERS, so the settings pages, model picker, agent
loops and usage accounting work without special cases.

Custom providers are general-purpose (Chat/Terminal/Browser). They are not
native Computer Use providers, so Vision never offers them.
"""
import base64
import io
import json
import logging
import mimetypes
import re
from typing import Any

import requests
from PIL import Image

from .base_provider import BaseLLMProvider
from .exceptions import (
    FatalError,
    RetryableError,
    extract_retry_after,
    is_transient_error,
)
from .llm_parsing import repair_json_string

logger = logging.getLogger(__name__)

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

_SCHEMA_FILES = (
    'cache/terminal/response_formats.json',
    'cache/vision/response_formats.json',
    'cache/browser/response_formats.json',
)

_registered_ids: set[str] = set()


# --------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------

def provider_id_error(provider_id: str, reserved=()) -> str | None:
    """Human-readable reason the id is unusable, or None."""
    pid = str(provider_id or "").strip()
    if not pid:
        return "Provider ID is required."
    if not _PROVIDER_ID_RE.match(pid):
        return ("Use lowercase letters, digits, hyphens or underscores "
                "(max 32 characters).")
    if pid in set(reserved or ()):
        return f"'{pid}' is already in use."
    return None


def normalize_definition(raw) -> dict | None:
    """Canonical definition, or None when it cannot produce a provider."""
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip()
    if not _PROVIDER_ID_RE.match(pid):
        return None
    name = " ".join(str(raw.get("name") or "").split()) or pid
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return None

    models = []
    seen = set()
    for item in raw.get("models") or []:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            label = " ".join(str(item.get("name") or "").split()) or model_id
        else:
            model_id = str(item or "").strip()
            label = model_id
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append({"id": model_id, "name": label})
    if not models:
        return None

    headers = {}
    raw_headers = raw.get("headers") or {}
    if isinstance(raw_headers, dict):
        pairs = raw_headers.items()
    else:
        pairs = []
        for item in raw_headers:
            if isinstance(item, dict):
                pairs.append((item.get("name"), item.get("value")))
    for key, value in pairs:
        header_name = str(key or "").strip()
        if header_name:
            headers[header_name] = str(value or "")

    return {
        "id": pid,
        "name": name,
        "base_url": base_url,
        "models": models,
        "headers": headers,
    }


def is_custom_provider(provider_id: str) -> bool:
    return provider_id in _registered_ids


def registered_custom_ids() -> set[str]:
    return set(_registered_ids)


def provider_is_configured(settings: dict, provider_id: str) -> bool:
    """Whether a provider can be used right now.

    Built-in providers need an API key. Custom providers are local or
    self-hosted often enough that a key is optional — the definition
    (valid base URL + at least one model) is what makes them usable.
    """
    if not provider_id:
        return False
    try:
        from llm import PROVIDERS
        if provider_id not in PROVIDERS:
            return False
    except Exception:
        return False
    if is_custom_provider(provider_id):
        return True
    providers = (settings or {}).get("llm_providers") or {}
    return bool((providers.get(provider_id) or {}).get("api_key"))


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def _build_provider_class(definition: dict):
    """Creates a concrete provider class bound to one user definition."""
    pid = definition["id"]
    display_name = definition["name"]
    model_ids = [m["id"] for m in definition["models"]]
    model_labels = {m["id"]: m["name"] for m in definition["models"]}

    def provider_config():
        return {
            "api_url": definition["base_url"],
            "display_name": display_name,
            "models": list(model_ids),
            "model_labels": dict(model_labels),
            "custom_headers": dict(definition["headers"]),
            "supports_cancellation": True,
            "supports_streaming_tools": False,
            "custom": True,
        }

    namespace = {
        "get_provider_id": staticmethod(lambda: pid),
        "get_display_name": staticmethod(lambda: display_name),
        "get_default_settings": staticmethod(lambda: {
            "model": model_ids[0] if model_ids else "",
            "api_key": "",
            "max_tokens": 4096,
            "require_json_output": True,
        }),
        "get_ui_schema": staticmethod(lambda: {
            "api_key": {
                "type": "password",
                "label": "API Key (optional)",
                "tooltip": "Leave empty for local servers that need no key.",
            },
            "model": {
                "type": "dropdown",
                "label": "Model",
                "options_source": "models.json",
            },
            "max_tokens": {"type": "string", "label": "Max Tokens"},
            "require_json_output": {
                "type": "checkbox",
                "label": "Require JSON Output",
            },
        }),
        "get_provider_config": staticmethod(provider_config),
    }
    return type(f"CustomOpenAIProvider_{pid}",
                (CustomOpenAIProvider,), namespace)


def register_custom_providers(definitions: list):
    """Registers valid definitions and drops providers that disappeared.

    Returns (normalized_definitions, classes_by_id, removed_ids).
    """
    from llm import PROVIDERS

    builtin_ids = set(PROVIDERS) - set(_registered_ids)
    normalized = []
    classes = {}
    for raw in definitions or []:
        definition = normalize_definition(raw)
        if definition is None:
            continue
        pid = definition["id"]
        if pid in builtin_ids or pid in classes:
            logger.warning("Custom provider id '%s' is reserved; skipped.", pid)
            continue
        classes[pid] = _build_provider_class(definition)
        normalized.append(definition)

    removed = _registered_ids - set(classes)
    for pid in removed:
        PROVIDERS.pop(pid, None)
    for pid, provider_class in classes.items():
        PROVIDERS[pid] = provider_class
    _registered_ids.clear()
    _registered_ids.update(classes)
    return normalized, classes, sorted(removed)


# --------------------------------------------------------------------------
# Provider implementation
# --------------------------------------------------------------------------

class CustomOpenAIProvider(BaseLLMProvider):
    """Chat Completions client for a user-provided base URL."""

    def __init__(self, provider_config: dict):
        super().__init__(provider_config)
        self.base_url = (self.provider_config.get("api_url") or "").rstrip("/")
        self.custom_headers = dict(
            self.provider_config.get("custom_headers") or {})
        self._tools_cache = None
        self.current_response = None
        self.is_cancelling = False
        self.session = requests.Session()

    @property
    def _display(self) -> str:
        return type(self).get_display_name()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # User headers win, so a gateway can replace auth or add routing.
        headers.update({str(k): str(v)
                        for k, v in self.custom_headers.items()})
        return headers

    def _endpoint(self) -> str:
        if not self.base_url:
            raise FatalError(
                f"{self._display}: base URL is not configured.")
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _tools_from_cache(self) -> list:
        if self._tools_cache is not None:
            return self._tools_cache
        schemas = {}
        for path in _SCHEMA_FILES:
            try:
                with open(path, encoding='utf-8') as f:
                    schemas.update(json.load(f))
            except FileNotFoundError:
                logger.warning("Custom provider: schema file '%s' missing.", path)
            except (OSError, json.JSONDecodeError) as e:
                logger.error("Custom provider: cannot read '%s': %s", path, e)
        tools = []
        for name, info in schemas.items():
            params = info.get("schema")
            if params:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": info.get("description", ""),
                        "parameters": params,
                    },
                })
        self._tools_cache = tools
        return tools

    @staticmethod
    def _image_part(attachment):
        """File path/bytes -> OpenAI-style image_url content part."""
        try:
            if isinstance(attachment, str):
                media_type, _ = mimetypes.guess_type(attachment)
                media_type = media_type or "image/png"
                with open(attachment, "rb") as f:
                    data = base64.b64encode(f.read()).decode('utf-8')
            elif isinstance(attachment, bytes):
                image = Image.open(io.BytesIO(attachment))
                buffer = io.BytesIO()
                image_format = image.format or 'PNG'
                image.save(buffer, format=image_format)
                data = base64.b64encode(buffer.getvalue()).decode('utf-8')
                media_type = f"image/{image_format.lower()}"
            else:
                return None
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            }
        except Exception as e:
            logger.error("Custom provider: image attachment failed: %s", e)
            return None

    def _prepare_messages(self, prompt: str, attachments: list,
                          system_prompt: str = None, history: list = None):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            if history[-1].get("content") == prompt:
                history = history[:-1]
            for message in history:
                role = message.get("role")
                if role == "model":
                    role = "assistant"
                if role in ("user", "assistant", "system"):
                    messages.append({
                        "role": role,
                        "content": message.get("content", ""),
                    })
        if attachments:
            content = [part for part in
                       (self._image_part(a) for a in attachments) if part]
            content.append({"type": "text", "text": prompt})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": prompt})
        return messages

    def _max_tokens(self) -> int:
        try:
            return int(self.provider_config.get("max_tokens", 4096))
        except (ValueError, TypeError):
            return 4096

    @staticmethod
    def _reasoning_text(message: dict) -> str:
        return message.get("reasoning_content") or message.get("reasoning") or ""

    def _raise_http_error(self, error: requests.exceptions.HTTPError):
        response = error.response
        status = response.status_code if response is not None else 0
        detail = response.text if response is not None else str(error)
        name = self._display
        if status == 429:
            raise FatalError(f"{name} quota/rate limit ({status}): {detail}") from error
        if 400 <= status < 500:
            raise FatalError(f"{name} client error ({status}): {detail}") from error
        if 500 <= status < 600:
            raise RetryableError(f"{name} server error ({status}): {detail}") from error
        raise FatalError(f"{name} HTTP error ({status}): {detail}") from error

    def _execute_get_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        schema_name: str | None = None,
    ) -> dict[str, Any]:
        messages = self._prepare_messages(prompt, attachments,
                                          system_prompt, history)
        request_body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self._max_tokens(),
            "stream": False,
        }
        use_forced_tool = bool(schema_name and self.require_json_output)
        if use_forced_tool:
            tools = self._tools_from_cache()
            target = next(
                (t for t in tools
                 if t.get("function", {}).get("name") == schema_name), None)
            if target:
                request_body["tools"] = [target]
                request_body["tool_choice"] = {
                    "type": "function",
                    "function": {"name": schema_name},
                }
            else:
                logger.warning("Custom provider: schema '%s' not found.",
                               schema_name)

        try:
            response = self.session.post(
                self._endpoint(),
                headers=self._headers(),
                json=request_body,
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            self._record_usage(data.get("usage"))

            text = ""
            reasoning = ""
            choices = data.get("choices") or []
            if choices:
                message = choices[0].get("message") or {}
                text = message.get("content") or ""
                reasoning = self._reasoning_text(message)
                tool_calls = message.get("tool_calls") or []
                if tool_calls and use_forced_tool:
                    call = tool_calls[0]
                    function = call.get("function") or {}
                    fn_name = function.get("name") or schema_name
                    payload = self._decode_tool_payload(
                        function.get("arguments", "{}"), fn_name)
                    return {
                        "text": text,
                        "tool_calls": {"type": fn_name, "payload": payload},
                        "reasoning": reasoning,
                    }
            return {"text": text, "tool_calls": [], "reasoning": reasoning}
        except requests.exceptions.HTTPError as e:
            self._raise_http_error(e)
        except requests.exceptions.RequestException as e:
            raise RetryableError(
                f"{self._display} network error: {e}") from e
        except (FatalError, RetryableError):
            raise
        except Exception as e:
            logger.error("Custom provider request failed: %s", e, exc_info=True)
            if is_transient_error(e):
                raise RetryableError(
                    f"{self._display} transient error: {e}",
                    retry_after=extract_retry_after(message=str(e))) from e
            raise FatalError(f"{self._display} request failed: {e}") from e

    def _decode_tool_payload(self, raw_arguments, fn_name: str):
        """Unwraps nested/double-encoded JSON some local models produce."""
        try:
            payload = json.loads(raw_arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        for _ in range(10):
            if isinstance(payload, dict) and 'payload' in payload and (
                len(payload) == 1 or payload.get('type') == fn_name
            ):
                payload = payload.get('payload')
                continue
            if isinstance(payload, str):
                try:
                    decoded = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    decoded = repair_json_string(payload)
                if decoded is None:
                    break
                payload = decoded
                continue
            break
        return payload

    def cancel_stream(self, generation_id: str = None):
        if self.current_response:
            try:
                self.is_cancelling = True
                self.current_response.close()
            except Exception as e:
                logger.error("Custom provider: cancel failed: %s", e)

    def stream_response(
        self,
        prompt: str,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, Any]] | None = None,
        callbacks=None,
    ):
        messages = self._prepare_messages(prompt, attachments,
                                          system_prompt, history)
        request_body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self._max_tokens(),
            "stream": True,
        }
        self.is_cancelling = False
        raw_logger = logging.getLogger('llm_raw_response')
        try:
            with self.session.post(
                self._endpoint(),
                headers=self._headers(),
                json=request_body,
                timeout=120,
                stream=True,
            ) as response:
                self.current_response = response
                response.raise_for_status()
                for line in response.iter_lines():
                    if self.is_cancelling:
                        break
                    if not line:
                        continue
                    decoded = line.decode('utf-8') if isinstance(line, bytes) else line
                    raw_logger.info(decoded)
                    if not decoded.startswith("data:"):
                        continue
                    payload = decoded[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("Custom provider: bad stream chunk: %s",
                                       payload)
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        self._record_usage(usage, merge=True)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            yield content
                        elif isinstance(content, list):
                            for part in content:
                                text = (part.get("text")
                                        if isinstance(part, dict) else None)
                                if text:
                                    yield text
        except requests.exceptions.HTTPError as e:
            if self.is_cancelling:
                logger.info("Custom provider: stream cancelled.")
            else:
                self._raise_http_error(e)
        except requests.exceptions.RequestException as e:
            if not self.is_cancelling:
                raise RetryableError(
                    f"{self._display} network error: {e}") from e
        except (FatalError, RetryableError):
            raise
        except Exception as e:
            if self.is_cancelling:
                logger.info("Custom provider: stream cancelled.")
            else:
                logger.error("Custom provider stream failed: %s", e,
                             exc_info=True)
                raise FatalError(f"{self._display} stream failed: {e}") from e
        finally:
            self.current_response = None
            self.is_cancelling = False

    def get_generation_id(self, response_chunk) -> str | None:
        return None
