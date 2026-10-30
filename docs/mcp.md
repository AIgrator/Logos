# MCP servers

Logos can use external tools through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP). An MCP server is a small program or remote service that advertises tools with JSON schemas; Logos connects to it, passes the tool list to the model and executes the calls the model asks for.

MCP is used by **Terminal** only. Chat, Browser and Vision do not see MCP tools.

## How the agent uses a server

The model never receives one tool per MCP tool. It works through three meta-tools:

| Tool | Purpose |
|---|---|
| `mcp_list` | Configured servers with their state and, for connected servers, tool names and short descriptions. |
| `mcp_get` | One tool's description, annotations and full input schema. The model calls it before a first call. |
| `mcp_call` | Executes a tool. `arguments` is a JSON object string such as `{"path": "."}`. |

This keeps the prompt small no matter how many servers are configured.

The Terminal system prompt also lists the configured servers with their live state and a one-line description: the catalog text for standard servers, the tool names for custom ones (at most 12 servers inline). The model can therefore recognize a server the user names — for example "remember this in Memory" — without a discovery call. `mcp_list` remains the full inventory with every tool name.

## Permissions

Every `mcp_call` passes the Terminal access policy before it runs (`Settings → Terminal → Access permissions`):

- `Ask every time` — a call runs silently only when the server is trusted and marks the tool read-only (`readOnlyHint`), and otherwise asks for approval.
- `Auto approve` — the same rule: read-only tools on trusted servers are silent, everything else asks.
- `Full access` — never asks.

`mcp_list` and `mcp_get` are read-only and never ask. A denied call is reported to the model as `Denied` and must not be retried. Read-only hints come from the server itself, so a malicious or buggy server can mislabel a tool; trust servers and projects accordingly.

## Catalog

**Browse Catalog...** on `Settings → MCP Servers` installs a standard server without typing commands or paths. Pick an entry and press **Install**; Logos fills in the definition and asks only for what it cannot know: an allowed folder, a database file or a token.

The bundled catalog has **Memory**, **Filesystem**, **SQLite**, **Git**, **Fetch**, **Time** and **Sequential Thinking** for local use, plus the remote **DeepWiki**, **Microsoft Learn**, **Context7** and **GitHub** services.

- Local entries need Node.js (`npx`) or uv (`uvx`). The catalog shows which one an entry needs and disables **Install** when it is missing.
- Memory keeps its knowledge graph in `~/.logos/mcp/memory.json`; SQLite defaults to `~/.logos/mcp/sqlite.db`; Filesystem and Git follow the active project (`${workspaceFolder}`).
- A token entered during installation is stored in the OS keychain, like any other credential-like MCP value.
- Installed entries become ordinary user servers: they appear in the list, start automatically and can be edited or removed there.

**Add Server** and **Import** remain for custom servers and definitions from other applications.

## Adding servers

Open `Settings → MCP Servers`.

- **Add Server...** creates a server. A **local process (stdio)** server has a command, one argument per line, an optional working directory and environment variables. A **remote (Streamable HTTP)** server has a URL and optional headers. The legacy SSE transport is not supported.
- **Import...** accepts a pasted or opened JSON file in the Claude Desktop / VS Code shape (`{"mcpServers": {...}}`) or a bare map of names to definitions.
- **Edit...** and **Remove** work on user servers. Removing a server deletes its stored secrets when you press **OK** or close the window.
- The checkbox in the Server column enables or disables a server: disabled servers stay configured but are not started and their tools are not offered.
- `Tool timeout` (default 60 s) bounds one call; `Connect timeout` (default 30 s) bounds the first connection and the initial tool listing.

Changes are saved as you make them, so closing the window or the app keeps them. **Cancel** restores the configuration from before you opened the page. Secrets of removed servers are deleted from the keychain when you press **OK** or close the window.

## Project servers

A project can add servers in `.mcp.json` at its root, using the same format:

```json
{
  "mcpServers": {
    "project-files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "${workspaceFolder}"]
    }
  }
}
```

A project server overrides a user server with the same name while that project is open. Project servers are untrusted by default: they show `needs_trust` and do not start until you select one and press **Trust Server**. Trust is remembered per project path in `settings.json` (`mcp.trusted_projects`); **Revoke Trust** stops the server again. `.mcp.json` comes from the repository, so review it like code before trusting it. **Open .mcp.json** opens the file in your editor and **Reload** re-reads servers and reconnects.

## Secrets

Environment and header values whose names look like credentials (`TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `AUTH`, `COOKIE`, …) are stored in the operating system keychain, not in `settings.json`. The settings file keeps only a placeholder (`${keyring:mcp:<server>:<kind>:<name>}`). Values are restored when Logos loads settings, and placeholders are removed from the keychain when the server or field is deleted.

If the keychain is unavailable, Logos still writes only the placeholder and warns you: re-enter the value after fixing the keychain. Secrets are masked in server logs and redacted from session events.

## Variables

Configuration values are expanded when a server starts:

| Variable | Value |
|---|---|
| `${workspaceFolder}` | Absolute path of the active project. |
| `${workspaceFolderBasename}` | Folder name of the active project. |
| `${userHome}` | Your home directory. |
| `${pathSeparator}` | The OS path separator. |
| `${env:NAME}` | An environment variable; an error is reported if it is missing. |
| `${NAME:-default}` | An environment variable with a fallback. |

Only the `${...}` form is expanded. A known name in single braces, such as `{userHome}`, is rejected when the server is saved or loaded with a hint to write `${userHome}`; otherwise the server would silently receive a relative path.

## Limits and recovery

- **Output cap.** A tool result larger than 20,000 characters is truncated in the conversation. The full text is saved under `<project>/.logos/mcp_outputs/` and the truncation notice contains its path, which the model can open with `read_files`. Each spill file is capped at 5 MB and the folder keeps the newest 50 files.
- **Timeouts.** A call that exceeds the server's tool timeout fails with a timeout error. Connection attempts and the initial tool listing are bounded by the connect timeout and tool timeout respectively; a server that hangs there ends in `error` instead of `starting`.
- **Circuit breaker.** Three consecutive transport failures or timeouts stop the server: its state becomes `error` and later calls fail fast with the reason. An error that the tool itself reports (a normal MCP error result) does not trip the breaker. **Restart** resets the counter and reconnects.
- **Config changes reconnect.** Editing, disabling or removing a server applies immediately to the running manager: changed servers restart, removed servers stop and disappear from `mcp_list`, and unchanged servers keep their connections.
- **Tool count.** At most 200 tools are listed per server; the rest is ignored and noted in the logs.

## Statuses

| State | Meaning |
|---|---|
| `stopped` | Configured but not started yet, or stopped between restarts. |
| `starting` | The process or HTTP connection is being opened. |
| `connected` | Ready; the Tools column shows how many tools it exposes. |
| `error` | The server failed to start, hung during discovery or tripped the circuit breaker. The message is shown next to the status and in **Logs**. |
| `needs_trust` | A project server from `.mcp.json` that has not been trusted. |
| `disabled` | Turned off with the checkbox or `"disabled": true` in the file. |

**Test** restarts the selected server and reports whether it connects and how many tools it exposes; **Restart** does the same without the test wording. **Logs** shows stderr output and connection messages for the selected server (last 200 lines, secrets masked).

## Troubleshooting

- **`command not found`.** The server command must be on `PATH` for the Logos process. Use an absolute path or a launcher such as `npx -y <package>`; the first `npx` run downloads the package and can take a while.
- **Server connects, then fails.** A stdio server must write only MCP protocol messages to stdout; diagnostic text belongs on stderr. Check **Logs** for the first error.
- **Remote server answers 401/403.** Add the required header (for example an API key); values whose names look like credentials are kept in the keychain.
- **Calls time out.** Raise `Tool timeout`, or check whether the tool is slow by design. The default is 60 s.
- **Server disappeared from `mcp_list`.** It was removed or renamed in settings; press **Reload** after editing `.mcp.json` by hand.

## What is not included

- MCP tools are not available in Browser, Chat or Vision.
- Logos does not register one tool per MCP tool with direct `mcp__<server>__<tool>` names.
- OAuth flows for remote servers and the legacy SSE transport are not supported; use Streamable HTTP with headers.
- Logos only connects to servers you add, manually or from the catalog. It does not sandbox them: a stdio server runs as your user, with your permissions.
