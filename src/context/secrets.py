"""Central credential redaction and OS-keychain storage for secrets."""
from __future__ import annotations

import copy
import logging
import re

import keyring

_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>(?:--)?[\"']?\b(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|token|password|passwd|secret|authorization)\b"
    r"[\"']?\s*(?::|=)\s*)"
    r"(?P<value>(?:bearer\s+)?[^\s,;]+)"
)
_FLAG_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>--(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"token|password|passwd|secret)\b\s+)"
    r"(?P<value>[^\s,;]+)"
)
_SECRET_KEYS = {
    "api_key", "apikey", "access_token", "refresh_token", "token",
    "password", "passwd", "secret", "authorization",
}


def redact_text(text: str) -> str:
    if not text:
        return ""
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group('prefix')}[REDACTED]", str(text))
    return _FLAG_VALUE_RE.sub(
        lambda m: f"{m.group('prefix')}[REDACTED]", text)


def _is_secret_key(key) -> bool:
    return str(key or "").strip().lower().replace("-", "_") in _SECRET_KEYS


def redact_value(value):
    """Recursively redact strings in journal event fields."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


# --- MCP server secrets ---------------------------------------------------
#
# Environment variables and headers whose names look like credentials are
# kept in the OS keychain. settings.json stores a placeholder instead:
#   ${keyring:mcp:<server>:<kind>:<name>}
# Values that are already references (``${env:...}``) are left untouched.

_MCP_PLACEHOLDER_PREFIX = "${keyring:mcp:"
_SECRET_WORDS = {
    "token", "secret", "password", "passwd", "pass", "pwd", "passphrase",
    "key", "apikey", "auth", "authorization", "authtoken", "cookie",
    "credential", "credentials", "accesstoken", "refreshtoken", "privatekey",
    "secretkey", "clientsecret", "session", "jwt", "pgpassword",
}


def is_mcp_secret_name(kind: str, name: str) -> bool:
    """Whether an env variable or header name looks like a credential."""
    words = {
        part for part in re.split(r"[^a-z0-9]+", str(name or "").lower())
        if part
    }
    return bool(words & _SECRET_WORDS)


def mcp_secret_key(server: str, kind: str, name: str) -> str:
    return f"mcp:{server}:{kind}:{name}"


def mcp_placeholder(server: str, kind: str, name: str) -> str:
    return "${keyring:" + mcp_secret_key(server, kind, name) + "}"


def _is_mcp_placeholder(value) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(_MCP_PLACEHOLDER_PREFIX)
        and value.endswith("}")
    )


def _mcp_secret_maps(entry) -> list[tuple[str, dict]]:
    if not isinstance(entry, dict):
        return []
    return [
        ("env", entry.get("env")),
        ("header", entry.get("headers")),
    ]


def sanitize_mcp_servers(servers, service: str):
    """Copies servers, moving credential-like values into the keychain."""
    cleaned = {}
    for server_name, raw in (servers or {}).items():
        entry = copy.deepcopy(raw)
        for kind, values in _mcp_secret_maps(entry):
            if not isinstance(values, dict):
                continue
            for name, value in list(values.items()):
                if not isinstance(value, str) or not value:
                    continue
                if not is_mcp_secret_name(kind, name):
                    continue
                if _is_mcp_placeholder(value) or "${" in value:
                    continue
                key = mcp_secret_key(server_name, kind, name)
                try:
                    keyring.set_password(service, key, value)
                except Exception as error:  # noqa: BLE001 - never write it to disk
                    logging.warning(
                        f"Could not store MCP secret '{key}' in the keychain: {error}")
                # The plaintext never reaches settings.json, even when the
                # keychain is unavailable (same rule as provider API keys).
                values[name] = mcp_placeholder(server_name, kind, name)
        cleaned[server_name] = entry
    return cleaned


def hydrate_mcp_servers(servers, service: str):
    """Copies servers, replacing keychain placeholders with their values."""
    hydrated = {}
    for server_name, raw in (servers or {}).items():
        entry = copy.deepcopy(raw)
        for _kind, values in _mcp_secret_maps(entry):
            if not isinstance(values, dict):
                continue
            for name, value in list(values.items()):
                if not _is_mcp_placeholder(value):
                    continue
                key = value[len("${keyring:"):-1]
                try:
                    secret = keyring.get_password(service, key)
                except Exception as error:  # noqa: BLE001 - keep the placeholder
                    logging.warning(
                        f"Could not read MCP secret '{key}' from the keychain: {error}")
                    continue
                if secret:
                    values[name] = secret
        hydrated[server_name] = entry
    return hydrated


def prune_mcp_secrets(service: str, old_servers, new_servers) -> None:
    """Deletes keychain entries for servers or fields removed by the user."""
    new_servers = new_servers if isinstance(new_servers, dict) else {}
    for server_name, raw in (old_servers or {}).items():
        new = new_servers.get(server_name)
        for kind, values in _mcp_secret_maps(raw):
            if not isinstance(values, dict):
                continue
            new_values = new.get("headers" if kind == "header" else "env") if isinstance(new, dict) else None
            if not isinstance(new_values, dict):
                new_values = {}
            for name in set(values) - set(new_values):
                if not is_mcp_secret_name(kind, name):
                    continue
                try:
                    keyring.delete_password(
                        service, mcp_secret_key(server_name, kind, name))
                except Exception:  # noqa: BLE001 - missing entries are fine
                    pass
