"""Curated MCP servers that can be installed from Settings in one click.

Entries are data only: `server` is a definition template where `{name}`
tokens are replaced by the values collected for `parameters`. The templates
use the same `${...}` variables as `.mcp.json` and are expanded when the
server connects, so `${workspaceFolder}` follows the active project.
"""
from __future__ import annotations

import copy
import shutil
from dataclasses import dataclass
from pathlib import Path

CATALOG_DIR = Path.home() / ".logos" / "mcp"

REQUIREMENTS = {
    "npx": (
        "Node.js (npx)",
        "Install Node.js from https://nodejs.org and reopen Settings.",
    ),
    "uvx": (
        "uv (uvx)",
        "Install uv from https://docs.astral.sh/uv/ and reopen Settings.",
    ),
}

_PARAMETER_KINDS = ("folder", "file", "secret", "text")


@dataclass(frozen=True, slots=True)
class CatalogParameter:
    key: str
    label: str
    kind: str = "text"
    default: str = ""
    hint: str = ""

    def __post_init__(self):
        if self.kind not in _PARAMETER_KINDS:
            raise ValueError(f"unknown catalog parameter kind '{self.kind}'")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    name: str
    title: str
    description: str
    transport: str
    requires: tuple[str, ...]
    parameters: tuple[CatalogParameter, ...]
    server: dict

    @property
    def local(self) -> bool:
        return self.transport == "stdio"

    @property
    def requirement_label(self) -> str:
        labels = [REQUIREMENTS.get(exe, (exe, ""))[0] for exe in self.requires]
        return ", ".join(labels)


def ensure_catalog_dir() -> Path:
    """Creates the folder used by catalog defaults such as the memory graph."""
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    return CATALOG_DIR


def missing_requirements(entry: CatalogEntry) -> list[tuple[str, str]]:
    """Returns (label, hint) for every runner this entry needs but cannot find."""
    missing = []
    for executable in entry.requires:
        label, hint = REQUIREMENTS.get(executable, (executable, ""))
        if shutil.which(executable) is None:
            missing.append((label, hint))
    return missing


def build_definition(entry: CatalogEntry, values: dict) -> dict:
    """Builds the settings.json definition from an entry and parameter values."""
    missing = [
        parameter.label for parameter in entry.parameters
        if not str(values.get(parameter.key) or "").strip()
    ]
    if missing:
        raise ValueError("Missing value for " + ", ".join(missing) + ".")
    return _substitute(copy.deepcopy(entry.server), values)


def _substitute(node, values: dict):
    if isinstance(node, str):
        for key, value in values.items():
            node = node.replace("{" + key + "}", str(value).strip())
        return node
    if isinstance(node, list):
        return [_substitute(item, values) for item in node]
    if isinstance(node, dict):
        return {key: _substitute(value, values) for key, value in node.items()}
    return node


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        name="memory",
        title="Memory",
        description=(
            "A local knowledge graph where the assistant stores facts, people, "
            "projects and how they relate, then recalls them in later sessions."
        ),
        transport="stdio",
        requires=("npx",),
        parameters=(),
        server={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "env": {"MEMORY_FILE_PATH": "${userHome}/.logos/mcp/memory.json"},
            "connect_timeout": 90.0,
            "timeout": 60.0,
        },
    ),
    CatalogEntry(
        name="filesystem",
        title="Filesystem",
        description=(
            "Read, write, move and search files inside the folders you allow. "
            "The default follows the active project."
        ),
        transport="stdio",
        requires=("npx",),
        parameters=(
            CatalogParameter(
                key="folder",
                label="Allowed folder",
                kind="folder",
                default="${workspaceFolder}",
                hint="Use ${workspaceFolder} to follow the active project, or pick a fixed folder.",
            ),
        ),
        server={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "{folder}"],
            "connect_timeout": 90.0,
            "timeout": 60.0,
        },
    ),
    CatalogEntry(
        name="sqlite",
        title="SQLite",
        description="Inspect tables and run SQL against a SQLite database file.",
        transport="stdio",
        requires=("npx",),
        parameters=(
            CatalogParameter(
                key="database",
                label="Database file",
                kind="file",
                default="${userHome}/.logos/mcp/sqlite.db",
                hint="Pick an existing database or a new file to create.",
            ),
        ),
        server={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-sqlite", "{database}"],
            "connect_timeout": 90.0,
            "timeout": 60.0,
        },
    ),
    CatalogEntry(
        name="git",
        title="Git",
        description=(
            "Inspect a repository: status, diffs, log, branches and commits. "
            "The default follows the active project."
        ),
        transport="stdio",
        requires=("uvx",),
        parameters=(),
        server={
            "type": "stdio",
            "command": "uvx",
            "args": ["mcp-server-git", "--repository", "${workspaceFolder}"],
            "connect_timeout": 120.0,
            "timeout": 60.0,
        },
    ),
    CatalogEntry(
        name="fetch",
        title="Fetch",
        description="Download a web page and turn it into Markdown the assistant can read.",
        transport="stdio",
        requires=("uvx",),
        parameters=(),
        server={
            "type": "stdio",
            "command": "uvx",
            "args": ["mcp-server-fetch"],
            "connect_timeout": 120.0,
            "timeout": 120.0,
        },
    ),
    CatalogEntry(
        name="time",
        title="Time",
        description="Current time and time zone conversions.",
        transport="stdio",
        requires=("uvx",),
        parameters=(),
        server={
            "type": "stdio",
            "command": "uvx",
            "args": ["mcp-server-time"],
            "connect_timeout": 120.0,
            "timeout": 60.0,
        },
    ),
    CatalogEntry(
        name="sequential-thinking",
        title="Sequential Thinking",
        description="A scratchpad for structured, step-by-step reasoning on hard problems.",
        transport="stdio",
        requires=("npx",),
        parameters=(),
        server={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
            "connect_timeout": 90.0,
            "timeout": 60.0,
        },
    ),
    CatalogEntry(
        name="deepwiki",
        title="DeepWiki",
        description=(
            "Ask questions about any public GitHub repository and get answers "
            "grounded in its documentation (deepwiki.com)."
        ),
        transport="http",
        requires=(),
        parameters=(),
        server={
            "type": "http",
            "url": "https://mcp.deepwiki.com/mcp",
            "connect_timeout": 45.0,
            "timeout": 120.0,
        },
    ),
    CatalogEntry(
        name="microsoft-learn",
        title="Microsoft Learn",
        description=(
            "Search Microsoft's official documentation for Azure, .NET, "
            "Microsoft 365 and other products."
        ),
        transport="http",
        requires=(),
        parameters=(),
        server={
            "type": "http",
            "url": "https://learn.microsoft.com/api/mcp",
            "connect_timeout": 45.0,
            "timeout": 60.0,
        },
    ),
    CatalogEntry(
        name="context7",
        title="Context7",
        description="Look up current documentation and code examples for common libraries and frameworks.",
        transport="http",
        requires=(),
        parameters=(),
        server={
            "type": "http",
            "url": "https://mcp.context7.com/mcp",
            "connect_timeout": 45.0,
            "timeout": 60.0,
        },
    ),
    CatalogEntry(
        name="github",
        title="GitHub",
        description=(
            "Work with repositories, issues and pull requests through a "
            "GitHub personal access token."
        ),
        transport="http",
        requires=(),
        parameters=(
            CatalogParameter(
                key="token",
                label="Personal access token",
                kind="secret",
                hint=(
                    "Create one at https://github.com/settings/tokens and paste it here. "
                    "It is stored in the OS keychain, not in settings.json."
                ),
            ),
        ),
        server={
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {"Authorization": "Bearer {token}"},
            "connect_timeout": 45.0,
            "timeout": 120.0,
        },
    ),
)


__all__ = [
    "CATALOG",
    "CatalogEntry",
    "CatalogParameter",
    "build_definition",
    "ensure_catalog_dir",
    "missing_requirements",
]
