"""Background MCP client manager used by the Terminal tool layer."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Mapping

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from .config import MCPServer

logger = logging.getLogger(__name__)

MAX_TOOLS_PER_SERVER = 200


def _describe_error(error: BaseException) -> str:
    """Unwrap anyio task-group wrappers so the status carries the real cause."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    text = str(error).strip()
    return text or type(error).__name__


def _decode_stderr(raw: bytes) -> str:
    """Decode child output: UTF-8 normally, the Windows OEM code page for console tools."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if sys.platform == "win32":
        return raw.decode("cp866", "replace")
    return raw.decode("utf-8", "replace")


class MCPManagerError(RuntimeError):
    def __init__(self, message: str, server: str = "", tool: str = ""):
        super().__init__(message)
        self.server = server
        self.tool = tool


@dataclass(slots=True)
class ToolInfo:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    title: str = ""

    @classmethod
    def from_sdk(cls, tool: Any) -> ToolInfo:
        raw_annotations = getattr(tool, "annotations", None)
        annotations: dict[str, Any] = {}
        if raw_annotations is not None:
            for key in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
                value = getattr(raw_annotations, key, None)
                if value is not None:
                    annotations[key] = value
        return cls(
            name=str(getattr(tool, "name", "") or ""),
            description=str(getattr(tool, "description", "") or ""),
            input_schema=dict(getattr(tool, "input_schema", None) or {}),
            annotations=annotations,
            title=str(getattr(tool, "title", None) or ""),
        )

    @property
    def read_only(self) -> bool:
        return self.annotations.get("read_only_hint") is True

    def as_dict(self, include_schema: bool = True) -> dict:
        data = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "annotations": dict(self.annotations),
        }
        if include_schema:
            data["input_schema"] = self.input_schema
        return data


@dataclass(slots=True)
class CallResult:
    server: str
    tool: str
    text: str = ""
    content: list = field(default_factory=list)
    structured: Any = None
    is_error: bool = False
    duration: float = 0.0

    def as_text(self) -> str:
        if self.text:
            return self.text
        if self.structured is not None:
            return json.dumps(self.structured, ensure_ascii=False, indent=2)
        return ""


@dataclass(slots=True)
class ServerStatus:
    name: str
    state: str = "stopped"
    tools: int = 0
    error: str = ""
    scope: str = "user"
    transport: str = "stdio"
    trusted: bool = True

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "tools": self.tools,
            "error": self.error,
            "scope": self.scope,
            "transport": self.transport,
            "trusted": self.trusted,
        }


class _StderrLog:
    """Pipe write-end for a subprocess plus a reader thread feeding the log ring."""

    def __init__(self, manager: MCPManager, server: str):
        self._manager = manager
        self._server = server
        self._lock = threading.Lock()
        self._closed = False
        read_fd, write_fd = os.pipe()
        self._reader = os.fdopen(read_fd, "rb", buffering=0)
        self._writer = os.fdopen(write_fd, "w", encoding="utf-8", errors="replace", buffering=1)
        self._thread = threading.Thread(target=self._pump, name=f"mcp-stderr-{server}", daemon=True)
        self._thread.start()

    @property
    def errlog(self):
        return self._writer

    def _pump(self) -> None:
        buffer = b""
        try:
            while True:
                chunk = self._reader.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._emit(line)
            self._emit(buffer)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._reader.close()
            except (OSError, ValueError):
                pass

    def _emit(self, raw: bytes) -> None:
        text = _decode_stderr(raw).strip()
        if text:
            self._manager._log(self._server, text)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._writer.close()
        except (OSError, ValueError):
            pass


class MCPManager:
    """Owns one asyncio loop thread and one live MCP client per server."""

    def __init__(self, servers: Mapping[str, MCPServer] | None = None, max_failures: int = 3, log_lines: int = 200):
        self._servers: dict[str, MCPServer] = dict(servers or {})
        self._max_failures = max(1, int(max_failures))
        self._log_lines = max(20, int(log_lines))
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._clients: dict[str, Client] = {}
        self._tools: dict[str, dict[str, ToolInfo]] = {}
        self._status: dict[str, ServerStatus] = {}
        self._logs: dict[str, deque[str]] = {}
        self._attempt_events: dict[str, threading.Event] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active: dict[str, MCPServer] = {}
        self._failures: dict[str, int] = {}
        for name, server in self._servers.items():
            self._status[name] = self._initial_status(server)
            self._logs[name] = deque(maxlen=self._log_lines)
            self._attempt_events[name] = threading.Event()
            self._failures[name] = 0

    # -- public lifecycle -------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mcp-manager", daemon=True)
        self._thread.start()
        self._schedule(self._reconcile())

    def stop(self, timeout: float = 20.0) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
        try:
            future.result(timeout)
        except Exception as error:  # noqa: BLE001 - shutdown must not raise
            logger.warning(f"MCP manager shutdown did not finish cleanly: {error}")
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        self._thread = None
        self._loop = None
        try:
            loop.close()
        except Exception:
            pass

    def refresh(self, servers: Mapping[str, MCPServer]) -> None:
        """Apply a new server map. Removed/changed servers are stopped."""
        if self._loop is None:
            self._servers = dict(servers)
            return
        with self._lock:
            for name, server in servers.items():
                if name not in self._attempt_events or self._active.get(name) != server:
                    self._attempt_events[name] = threading.Event()
                self._status.setdefault(name, self._initial_status(server))
                self._logs.setdefault(name, deque(maxlen=self._log_lines))
            self._servers = dict(servers)
        self._schedule(self._reconcile())

    def restart(self, name: str) -> None:
        with self._lock:
            server = self._servers.get(name)
        if server is None:
            raise MCPManagerError(f"Unknown MCP server '{name}'", server=name)
        if self._loop is None:
            raise MCPManagerError("MCP manager is not running", server=name)
        event = threading.Event()
        with self._lock:
            self._attempt_events[name] = event
            self._failures[name] = 0
            status = self._status.get(name)
            if status is not None:
                status.state = "starting"
                status.tools = 0
                status.error = ""
        self._schedule(self._restart(name, server, event))

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """Wait until every enabled server has connected or failed once."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                servers = dict(self._servers)
                events = [self._attempt_events.get(name) for name, server in servers.items() if self._is_startable(server)]
            if not events:
                return True
            if all(event is not None and event.is_set() for event in events):
                return True
            time.sleep(0.05)
        return False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- public queries ---------------------------------------------------

    def statuses(self) -> dict[str, ServerStatus]:
        with self._lock:
            return {name: ServerStatus(**status.as_dict()) for name, status in self._status.items()}

    def status(self, name: str) -> ServerStatus:
        with self._lock:
            status = self._status.get(name)
            if status is None:
                raise MCPManagerError(f"Unknown MCP server '{name}'", server=name)
            return ServerStatus(**status.as_dict())

    def tools(self, server: str | None = None) -> dict:
        with self._lock:
            if server is not None:
                return dict(self._tools.get(server, {}))
            return {name: dict(tools) for name, tools in self._tools.items()}

    def get_tool(self, server: str, tool: str) -> ToolInfo:
        with self._lock:
            info = self._tools.get(server, {}).get(tool)
        if info is None:
            raise MCPManagerError(f"MCP server '{server}' has no tool '{tool}'", server=server, tool=tool)
        return info

    def logs(self, server: str, limit: int = 50) -> list[str]:
        with self._lock:
            lines = list(self._logs.get(server, ()))
        return lines[-max(1, int(limit)):]

    def call_tool(self, server: str, tool: str, arguments: dict | None = None, timeout: float | None = None) -> CallResult:
        """Call a tool from any thread. Blocks until the result or timeout."""
        with self._lock:
            status = self._status.get(server)
            server_config = self._servers.get(server)
            client = self._clients.get(server)
        if status is None or server_config is None:
            raise MCPManagerError(f"Unknown MCP server '{server}'", server=server, tool=tool)
        if client is None or status.state != "connected":
            detail = status.error or status.state
            hint = (
                " Restart it from Settings -> MCP Servers."
                if status.state == "error" else "")
            raise MCPManagerError(
                f"MCP server '{server}' is not connected ({detail}).{hint}",
                server=server, tool=tool)
        if self._loop is None:
            raise MCPManagerError("MCP manager is not running", server=server, tool=tool)

        call_timeout = float(timeout if timeout is not None else server_config.timeout)
        future = asyncio.run_coroutine_threadsafe(
            self._call(server, tool, arguments or {}, call_timeout), self._loop)
        try:
            return future.result(call_timeout + 10.0)
        except FutureTimeoutError as error:
            future.cancel()
            self._record_failure(server, f"tool '{tool}' did not finish in {call_timeout:g}s")
            raise MCPManagerError(f"MCP tool '{tool}' timed out after {call_timeout:g}s", server=server, tool=tool) from error
        except MCPManagerError:
            raise
        except Exception as error:  # noqa: BLE001 - normalize transport failures
            raise MCPManagerError(f"MCP tool '{tool}' failed: {error}", server=server, tool=tool) from error

    # -- internals --------------------------------------------------------

    def _run_loop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _schedule(self, coro) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        future = asyncio.run_coroutine_threadsafe(coro, loop)

        def _done(result):
            try:
                result.result()
            except Exception as error:  # noqa: BLE001 - background task
                logger.error(f"MCP manager task failed: {error}", exc_info=True)

        future.add_done_callback(_done)

    async def _reconcile(self) -> None:
        with self._lock:
            servers = dict(self._servers)
        for name, task in list(self._tasks.items()):
            server = servers.get(name)
            if server is not None and server == self._active.get(name) and not task.done():
                continue
            await self._stop_task(name)
            with self._lock:
                if name not in servers:
                    # A removed server must also disappear from mcp_list.
                    self._status.pop(name, None)
                    self._logs.pop(name, None)
                    self._failures.pop(name, None)
                    self._attempt_events.pop(name, None)
                    self._stop_events.pop(name, None)
        for name, server in servers.items():
            task = self._tasks.get(name)
            if task is not None and not task.done() and self._active.get(name) == server:
                continue
            stop_event = asyncio.Event()
            with self._lock:
                self._active[name] = server
                self._stop_events[name] = stop_event
                self._status[name] = self._initial_status(server)
                self._failures[name] = 0
                event = self._attempt_events.get(name)
                if event is None or event.is_set():
                    event = threading.Event()
                    self._attempt_events[name] = event
            task = asyncio.create_task(self._supervise(name, server, stop_event, event))
            with self._lock:
                self._tasks[name] = task

    async def _stop_task(self, name: str, grace: float = 10.0) -> None:
        """Let a supervisor close its client first; cancel only if it overruns."""
        with self._lock:
            task = self._tasks.get(name)
            stop_event = self._stop_events.get(name)
        if task is None:
            return
        if stop_event is not None:
            stop_event.set()
        done, pending = await asyncio.wait({task}, timeout=grace)
        if pending:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with self._lock:
            if self._tasks.get(name) is task:
                self._tasks.pop(name, None)
                self._active.pop(name, None)
                self._clients.pop(name, None)
                self._tools.pop(name, None)

    async def _restart(self, name: str, server: MCPServer, event: threading.Event) -> None:
        await self._stop_task(name)
        with self._lock:
            self._clients.pop(name, None)
            self._tools.pop(name, None)
            self._failures[name] = 0
            self._status[name] = self._initial_status(server)
            self._active[name] = server
        stop_event = asyncio.Event()
        with self._lock:
            self._stop_events[name] = stop_event
        task = asyncio.create_task(self._supervise(name, server, stop_event, event))
        with self._lock:
            self._tasks[name] = task

    async def _supervise(self, name: str, server: MCPServer, stop_event: asyncio.Event, attempt_event: threading.Event) -> None:
        stack = AsyncExitStack()
        client: Client | None = None
        try:
            if server.error:
                self._set_status(name, "error", error=server.error)
                return
            if not server.enabled:
                self._set_status(name, "disabled")
                return
            if not server.trusted:
                self._set_status(name, "needs_trust", error="Project server is not trusted yet")
                return
            self._set_status(name, "starting")
            client = await self._open_client(server, stack)
            try:
                tools = await asyncio.wait_for(
                    self._discover_tools(name, client), server.timeout)
            except TimeoutError as error:
                raise MCPManagerError(
                    f"tool discovery did not finish in {server.timeout:g}s",
                    server=name) from error
            with self._lock:
                self._clients[name] = client
                self._tools[name] = tools
                self._failures[name] = 0
            self._set_status(name, "connected", tools=len(tools))
            attempt_event.set()
            await stop_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - one bad server must not break others
            message = _describe_error(error)
            self._log(name, f"connection failed: {message}")
            self._set_status(name, "error", error=message)
        finally:
            with self._lock:
                if self._clients.get(name) is client:
                    self._clients.pop(name, None)
                self._tools.pop(name, None)
                status = self._status.get(name)
                if status is not None and status.state in ("connected", "starting"):
                    # Clear the state in the same lock as the client: callers
                    # must never see "connected" with no client.
                    status.state = "stopped"
                    status.tools = 0
            await self._close_stack(name, stack)
            attempt_event.set()

    async def _open_client(self, server: MCPServer, stack: AsyncExitStack) -> Client:
        if server.transport == "stdio":
            params = StdioServerParameters(
                command=server.command,
                args=list(server.args),
                env=dict(server.env) or None,
                cwd=server.cwd or None,
            )
            stderr_log = _StderrLog(self, server.name)
            stack.callback(stderr_log.close)
            transport = stdio_client(params, errlog=stderr_log.errlog)
            try:
                client = await asyncio.wait_for(
                    stack.enter_async_context(
                        Client(transport, read_timeout_seconds=server.timeout)),
                    server.connect_timeout,
                )
            except TimeoutError as error:
                raise MCPManagerError(
                    f"connection did not open within {server.connect_timeout:g}s",
                    server=server.name) from error
            stderr_log.close()
            return client
        if server.transport == "http":
            http_client = await stack.enter_async_context(
                create_mcp_http_client(headers=dict(server.headers) or None))
            transport = streamable_http_client(server.url, http_client=http_client)
        else:
            raise MCPManagerError(f"Unsupported transport '{server.transport}'", server=server.name)
        try:
            return await asyncio.wait_for(
                stack.enter_async_context(
                    Client(transport, read_timeout_seconds=server.timeout)),
                server.connect_timeout,
            )
        except TimeoutError as error:
            raise MCPManagerError(
                f"connection did not open within {server.connect_timeout:g}s",
                server=server.name) from error

    async def _discover_tools(self, name: str, client: Client) -> dict[str, ToolInfo]:
        tools: dict[str, ToolInfo] = {}
        cursor = None
        while True:
            result = await client.list_tools(cursor=cursor)
            for raw_tool in getattr(result, "tools", []) or []:
                info = ToolInfo.from_sdk(raw_tool)
                if info.name:
                    tools[info.name] = info
                if len(tools) >= MAX_TOOLS_PER_SERVER:
                    self._log(name, f"tool list truncated at {MAX_TOOLS_PER_SERVER}")
                    return tools
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                return tools

    async def _call(self, server: str, tool: str, arguments: dict, timeout: float) -> CallResult:
        with self._lock:
            client = self._clients.get(server)
        if client is None:
            raise MCPManagerError(f"MCP server '{server}' is not connected", server=server, tool=tool)
        started = time.perf_counter()
        try:
            result = await client.call_tool(tool, arguments, read_timeout_seconds=timeout)
        except Exception as error:  # noqa: BLE001 - transport/protocol errors
            message = str(error) or type(error).__name__
            self._record_failure(server, f"tool '{tool}' failed: {message}")
            raise MCPManagerError(f"MCP tool '{tool}' failed: {message}", server=server, tool=tool) from error
        self._failures[server] = 0
        return CallResult(
            server=server,
            tool=tool,
            text=_text_of(result),
            content=list(getattr(result, "content", None) or []),
            structured=getattr(result, "structured_content", None),
            is_error=bool(getattr(result, "is_error", False)),
            duration=time.perf_counter() - started,
        )

    async def _shutdown(self) -> None:
        names = list(self._tasks)
        if names:
            await asyncio.gather(*(self._stop_task(name) for name in names), return_exceptions=True)
        current = asyncio.current_task()
        leftovers = [task for task in asyncio.all_tasks() if task is not current]
        for task in leftovers:
            task.cancel()
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)
        with self._lock:
            self._tasks.clear()
            self._clients.clear()
            self._tools.clear()
            self._stop_events.clear()
            self._active.clear()
            for status in self._status.values():
                status.state = "stopped"
                status.tools = 0

    async def _close_stack(self, name: str, stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except Exception as error:  # noqa: BLE001 - shutdown must be best effort
            self._log(name, f"shutdown error: {error}")

    def _initial_status(self, server: MCPServer) -> ServerStatus:
        if server.error:
            state = "error"
            error = server.error
        elif not server.enabled:
            state = "disabled"
            error = ""
        elif not server.trusted:
            state = "needs_trust"
            error = "Project server is not trusted yet"
        else:
            state = "stopped"
            error = ""
        return ServerStatus(
            name=server.name,
            state=state,
            error=error,
            scope=server.scope,
            transport=server.transport,
            trusted=server.trusted,
        )

    def _is_startable(self, server: MCPServer) -> bool:
        return bool(server.enabled and server.trusted and not server.error)

    def _set_status(self, name: str, state: str, tools: int = 0, error: str = "") -> None:
        if error:
            error = self._redact(name, error)
        with self._lock:
            status = self._status.get(name)
            if status is None:
                server = self._servers.get(name)
                status = ServerStatus(
                    name=name,
                    scope=server.scope if server else "user",
                    transport=server.transport if server else "stdio",
                    trusted=server.trusted if server else True,
                )
                self._status[name] = status
            status.state = state
            status.tools = tools
            status.error = error

    def _record_failure(self, name: str, message: str) -> None:
        with self._lock:
            count = self._failures.get(name, 0) + 1
            self._failures[name] = count
        self._log(name, f"{message} ({count}/{self._max_failures})")
        if count >= self._max_failures:
            self._set_status(name, "error", error=f"Stopped after {count} failed calls: {message}")
            event = self._stop_events.get(name)
            if event is not None:
                event.set()

    def _log(self, name: str, message: str) -> None:
        text = self._redact(name, str(message).strip())
        if not text:
            return
        with self._lock:
            lines = self._logs.setdefault(name, deque(maxlen=self._log_lines))
        lines.append(f"[{time.strftime('%H:%M:%S')}] {text}")

    def _redact(self, name: str, text: str) -> str:
        with self._lock:
            server = self._servers.get(name)
        if server is None:
            return text
        for secret in server.secret_values:
            text = text.replace(secret, "***")
        return text

    def __enter__(self) -> MCPManager:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


def _text_of(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
    return "\n".join(parts)


__all__ = [
    "CallResult",
    "MCPManager",
    "MCPManagerError",
    "ServerStatus",
    "ToolInfo",
]
