"""Slash commands typed into the composer (``/init``, later ``/memory``).

A command is a prepared prompt, not a dialog: pressing Enter sends one ordinary
user message, so the agent's steps appear in the transcript like any other
turn. The registry below is the only place a command exists; adding ``/memory``
later means one entry here.

``/init`` writes ``AGENTS.md``. The scanner in
``context/project_instructions.py`` supplies the facts the prompt starts from,
so the agent spends its steps reading the code instead of discovering the same
manifests, and every command it prints can be traced to a real file.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from context import project_instructions

#: The prompt names the file it writes; keep the two in one place.
FILENAME = project_instructions.FILENAME

_MAX_FACTS = 32
_MAX_README_COMMANDS = 10

#: A leading slash is a command only when it is one word: ``/init`` is one,
#: ``/home/user`` (a pasted path) is not.
_COMMAND_RE = re.compile(r"^/[A-Za-z][\w-]*$")


class CommandError(Exception):
    """A command that cannot run here; the message is shown to the user."""


@dataclass(frozen=True)
class CommandRun:
    """What one command sends to the model."""

    prompt: str
    objective: str = ""
    capability: str = ""


@dataclass(frozen=True)
class SlashCommand:
    """One entry of the composer's command palette."""

    name: str
    summary: str
    build: Callable[[str, str], CommandRun]
    capability: str = ""
    takes_arguments: bool = False

    @property
    def slash(self) -> str:
        return f"/{self.name}"


def _init_run(project_root: str, arguments: str = "") -> CommandRun:
    if not project_root or not os.path.isdir(project_root):
        raise CommandError("Open a project folder before running /init.")
    scan = project_instructions.scan(project_root)
    existing = project_instructions.read_raw(project_root)
    prompt = _INIT_PROMPT + "\n\n" + _facts_block(scan, existing)
    return CommandRun(
        prompt=prompt,
        objective=f"Write {FILENAME} for {scan.name}",
        capability="terminal",
    )


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="init",
        summary=f"Generate or update {FILENAME} for this project",
        build=_init_run,
        capability="terminal",
    ),
)


def command_name(text: str) -> str:
    """The leading token of a slash message (``/init``), or an empty string."""
    stripped = str(text or "").strip()
    if not stripped.startswith("/"):
        return ""
    return stripped.split(None, 1)[0]


def arguments(text: str) -> str:
    """Everything after the command word."""
    parts = str(text or "").strip().split(None, 1)
    return parts[1] if len(parts) > 1 else ""


def looks_like_command(text: str) -> bool:
    """True for a leading one-word slash token, known or not."""
    return bool(_COMMAND_RE.match(command_name(text)))


def find(text: str) -> SlashCommand | None:
    """The command a sent message invokes, or None for ordinary text."""
    token = command_name(text)
    if not token:
        return None
    name = token[1:].lower()
    for command in COMMANDS:
        if command.name == name:
            return command
    return None


def suggestions(prefix: str) -> list[SlashCommand]:
    """Commands whose name starts with ``prefix`` (with or without the slash)."""
    prefix = str(prefix or "").strip().lstrip("/").lower()
    return [command for command in COMMANDS if command.name.startswith(prefix)]


def _facts_block(scan: project_instructions.Scan, existing: str) -> str:
    """The scanner's findings, written as leads the agent has to verify."""
    lines = [
        "### Facts the scanner already found",
        "",
        ("These are leads, not the answer: open the file a fact points to before "
         "you rely on it, and go beyond this list where the project needs it."),
        "",
        f"- Project: `{scan.name}` at `{scan.root}`",
    ]
    if scan.stack:
        lines.append("- Stack: " + ", ".join(scan.stack))
    if scan.tree:
        lines.append(
            "- Top level: " + ", ".join(f"`{name}`" for name in scan.tree))
    if scan.language == "ru":
        lines.append(
            f"- The README is written in Russian: write `{FILENAME}` in Russian.")
    else:
        lines.append(
            f"- The README is written in English: write `{FILENAME}` in English.")
    if scan.facts:
        lines += ["", "Facts:"]
        lines += [
            f"- {fact.text} _(source: {fact.source})_"
            for fact in scan.facts[:_MAX_FACTS]
        ]
    if scan.readme_commands:
        lines += ["", "Commands printed by the project's own documentation:"]
        lines += [
            f"- `{command}`"
            for command in scan.readme_commands[:_MAX_README_COMMANDS]
        ]
    if existing.strip():
        size = len(existing.encode("utf-8"))
        lines += [
            "",
            (f"`{FILENAME}` already exists ({size} bytes). Read it before writing, "
             "keep every rule that is still true, and change only what the "
             "project has outgrown."),
        ]
    return "\n".join(lines)


_INIT_PROMPT = f"""\
Create or update `{FILENAME}` at the root of this project. It is the file a new \
contributor - human or AI - reads before changing the code, so it must describe \
this project specifically: its real commands, its layout, the rules the code \
enforces, and the traps a newcomer hits.

Work in four passes.

1. Investigate. Read the files that decide how this project is installed, run, \
tested, linted, built and released: dependency manifests and lockfiles, tool \
configs (ruff, eslint, prettier, tsconfig, pytest, Makefile), CI workflows, the \
packaging or installer spec, README/BUILDING/CONTRIBUTING, and any existing \
instruction file. Use `list_directory`, `read_files`, `search_file_content` and \
`find_files`. Never describe a file you have not read, and read each one once: \
old tool output can be dropped from your history, so note what matters as you go \
instead of reading the same range again.

2. Extract. Collect only what changes a decision:
- the real entry point and how the app starts;
- the exact install/run/test/lint/build commands, with the package manager or \
interpreter the project actually uses (uv, pnpm, a venv, ...);
- the architecture and directories that are not obvious from their names;
- conventions the tooling enforces (line length, formatter, generated or \
vendored folders);
- pitfalls: a test folder with no tests, a build artifact that must not be \
edited, a server that has to run first.

3. Write. Use `write_file` on `{FILENAME}`. Keep it 2-6 KB and under 90 lines, \
with short `##` sections and commands in backticks. Prefer a concrete line \
("Run `uv run python main.py`") over advice ("run the app"). Leave out generic \
engineering advice ("write clean code", "follow best practices") and never \
invent a command: if the project does not show one, omit it. If `{FILENAME}` \
already exists, read it first and keep every rule that is still true. Write the \
file in the language the README uses. `{FILENAME}` is the only file you touch: \
do not edit, rewrite or reformat another instruction file \
(`.github/copilot-instructions.md`, `CLAUDE.md`, `.cursor/rules`, ...) even when \
one exists and looks stale - mention it in `{FILENAME}` as the other file to \
keep in sync and leave its content to the user.

4. Finish. Reply with the path you wrote and a short list of what the file now \
covers."""
