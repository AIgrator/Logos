"""MCP server configuration: user settings + project `.mcp.json`."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Mapping

DEFAULT_TIMEOUT = 60.0
DEFAULT_CONNECT_TIMEOUT = 30.0
SUPPORTED_TRANSPORTS = ("stdio", "http")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOKEN_RE = re.compile(r"\$\{([^{}]+)\}")
_SINGLE_BRACE_RE = re.compile(
    r"(?<!\$)\{(workspaceFolderBasename|workspaceFolder|userHome|pathSeparator|env:[^{}]+)\}")


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class MCPServer:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = DEFAULT_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    scope: str = "user"
    trusted: bool = True
    error: str = ""

    @property
    def needs_trust(self) -> bool:
        return not self.trusted

    @property
    def secret_values(self) -> list[str]:
        values = list(self.env.values()) + list(self.headers.values())
        return [value for value in values if isinstance(value, str) and len(value) >= 4]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "transport": self.transport,
            "scope": self.scope,
            "enabled": self.enabled,
            "trusted": self.trusted,
            "needs_trust": self.needs_trust,
            "error": self.error,
        }


@dataclass(slots=True)
class MCPConfig:
    servers: dict[str, MCPServer] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def enabled_servers(self) -> dict[str, MCPServer]:
        return {name: server for name, server in self.servers.items() if server.enabled and server.trusted and not server.error}


def load_servers(
    user_servers: Mapping[str, Any] | None,
    project_root: str = "",
    trusted_project: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> MCPConfig:
    """Merge user servers from settings with the project's `.mcp.json`.

    Project entries override user entries with the same name. Project servers
    stay untrusted (and are not started) until their names are listed in
    `trusted_project`.
    """
    env = dict(os.environ if environ is None else environ)
    context = _build_context(project_root)
    trusted = {str(name) for name in trusted_project}
    config = MCPConfig()

    _merge_into(config, user_servers, "user", context, env, trusted=True, source="settings")
    if project_root:
        path = Path(project_root) / ".mcp.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                config.errors.append(f"{path}: {error}")
            else:
                raw = data.get("mcpServers") if isinstance(data, dict) else None
                if raw is not None and not isinstance(raw, dict):
                    config.errors.append(f"{path}: 'mcpServers' must be an object")
                elif isinstance(raw, dict):
                    _merge_into(config, raw, "project", context, env, trusted=None, trusted_names=trusted, source=str(path))

    return config


def _merge_into(
    config: MCPConfig,
    servers: Mapping[str, Any] | None,
    scope: str,
    context: dict[str, str],
    environ: Mapping[str, str],
    trusted: bool | None,
    trusted_names: set[str] | None = None,
    source: str = "",
) -> None:
    if servers is None:
        return
    if not isinstance(servers, Mapping):
        config.errors.append(f"{source}: server list must be an object")
        return
    for raw_name, raw in servers.items():
        name = str(raw_name)
        if not _NAME_RE.match(name):
            config.errors.append(f"{source}: invalid server name '{name}' (use letters, digits, '.', '_' or '-')")
            continue
        is_trusted = trusted if trusted is not None else name in (trusted_names or set())
        try:
            config.servers[name] = _parse_server(name, raw, scope, context, environ, is_trusted)
        except ConfigError as error:
            config.servers[name] = MCPServer(name=name, transport="invalid", scope=scope, trusted=is_trusted, error=str(error))


def _parse_server(
    name: str,
    raw: Any,
    scope: str,
    context: dict[str, str],
    environ: Mapping[str, str],
    trusted: bool,
) -> MCPServer:
    if not isinstance(raw, Mapping):
        raise ConfigError("server definition must be an object")

    transport = str(raw.get("type") or raw.get("transport") or "").strip().lower().replace("_", "-")
    if transport in ("streamable-http", "streamablehttp"):
        transport = "http"
    if not transport:
        transport = "http" if raw.get("url") else "stdio"
    if transport == "sse":
        raise ConfigError("the legacy SSE transport is not supported; use Streamable HTTP")
    if transport not in SUPPORTED_TRANSPORTS:
        raise ConfigError(f"unsupported transport '{transport}'")

    enabled = _bool(raw.get("enabled", not _bool(raw.get("disabled", False), "disabled")), "enabled")
    timeout = _positive_number(raw.get("timeout_ms"), DEFAULT_TIMEOUT * 1000, "timeout_ms") / 1000
    if raw.get("timeout") is not None:
        timeout = _positive_number(raw.get("timeout"), DEFAULT_TIMEOUT, "timeout")
    connect_timeout = _positive_number(raw.get("connect_timeout"), DEFAULT_CONNECT_TIMEOUT, "connect_timeout")

    if transport == "stdio":
        command, args = _command(raw.get("command"), raw.get("args"))
        if not command:
            raise ConfigError("stdio server needs 'command'")
        env = _string_map(raw.get("env"), "env")
        cwd = _interpolate(raw.get("cwd") or "", context, environ)
        return MCPServer(
            name=name,
            transport=transport,
            command=_interpolate(command, context, environ),
            args=[_interpolate(str(arg), context, environ) for arg in args],
            env={key: _interpolate(value, context, environ) for key, value in env.items()},
            cwd=str(cwd or ""),
            enabled=enabled,
            timeout=timeout,
            connect_timeout=connect_timeout,
            scope=scope,
            trusted=trusted,
        )

    url = _interpolate(raw.get("url") or "", context, environ)
    if not url:
        raise ConfigError("http server needs 'url'")
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError("http server 'url' must start with http:// or https://")
    headers = _string_map(raw.get("headers"), "headers")
    return MCPServer(
        name=name,
        transport=transport,
        url=url,
        headers={key: _interpolate(value, context, environ) for key, value in headers.items()},
        enabled=enabled,
        timeout=timeout,
        connect_timeout=connect_timeout,
        scope=scope,
        trusted=trusted,
    )


def _command(raw_command: Any, raw_args: Any) -> tuple[str, list[str]]:
    args: list[str] = []
    if isinstance(raw_args, (list, tuple)):
        args = [str(item) for item in raw_args]
    elif raw_args is not None:
        raise ConfigError("'args' must be a list")
    if isinstance(raw_command, (list, tuple)):
        parts = [str(item) for item in raw_command]
        if not parts:
            return "", args
        return parts[0], parts[1:] + args
    if raw_command is None:
        return "", args
    return str(raw_command), args


def _string_map(raw: Any, field_name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"'{field_name}' must be an object")
    result = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ConfigError(f"'{field_name}.{key}' must be a string")
        result[str(key)] = value
    return result


def _bool(raw: Any, field_name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "yes", "no", "1", "0"):
        return raw.strip().lower() in ("true", "yes", "1")
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    raise ConfigError(f"'{field_name}' must be a boolean")


def _positive_number(raw: Any, default: float, field_name: str) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"'{field_name}' must be a number") from error
    if value <= 0:
        raise ConfigError(f"'{field_name}' must be greater than zero")
    return value


class _PassthroughEnv(dict):
    """Resolves every ${...} reference to itself for structural validation."""

    def __contains__(self, key) -> bool:
        return True

    def __getitem__(self, key):
        return f"${{{key}}}"


def server_name_error(name: str) -> str:
    if not _NAME_RE.match(str(name or "")):
        return "Use letters, digits, '.', '_' or '-' (1-64 characters)."
    return ""


def definition_error(name: str, raw: Any) -> str:
    """Checks a server definition without resolving environment references."""
    try:
        _parse_server(str(name or "server"), raw, "user", _build_context(""), _PassthroughEnv(), True)
    except ConfigError as error:
        return str(error)
    return ""


def _build_context(project_root: str) -> dict[str, str]:
    context: dict[str, str] = {}
    if project_root:
        root = os.path.abspath(project_root)
        context["workspaceFolder"] = root
        context["workspaceFolderBasename"] = os.path.basename(root.rstrip("\\/"))
    context["userHome"] = str(Path.home())
    context["pathSeparator"] = os.sep
    context["/"] = "/"
    return context


def _interpolate(value: Any, context: dict[str, str], environ: Mapping[str, str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    wrong = _SINGLE_BRACE_RE.search(value)
    if wrong:
        raise ConfigError(
            f"'{wrong.group(0)}' is not expanded; write '${{{wrong.group(1)}}}' instead")
    if "${" not in value:
        return value

    def replace(match: re.Match) -> str:
        token = match.group(1)
        if token in context:
            return context[token]
        if token.startswith("env:"):
            name = token[4:]
            if name in environ:
                return environ[name]
            raise ConfigError(f"environment variable '{name}' is not set")
        if ":-" in token:
            name, default = token.split(":-", 1)
            return environ.get(name, default)
        if token in environ:
            return environ[token]
        raise ConfigError(f"unresolved variable '${{{token}}}'")

    return _TOKEN_RE.sub(replace, value)
