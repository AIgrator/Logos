"""Slash commands typed into the composer (``/init``, ``/compact``, ``/memory``).

The registry below is the only place a command exists: the palette, the routing
and the journal all follow from it. There are two kinds, and they differ in
what pressing Enter does.

A **prompt command** (``build``) is a prepared prompt, not a dialog: pressing
Enter sends one ordinary user message, so the agent's steps appear in the
transcript like any other turn. ``/init`` is one. The scanner in
``context/project_instructions.py`` supplies the facts its prompt starts from,
so the agent spends its steps reading the code instead of discovering the same
manifests, and every command it prints can be traced to a real file.

An **app action** (``action``) sends nothing to the model. It names a method on
``AppController`` (``_command_compact``, ``_command_memory``) that does the
work in the UI: summarizing the session, opening a memory file. An action
creates no turn, writes no journal event, and runs even when no provider is
configured, because there is no request to make.
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
    #: Absolute path this command promises to write. The handler compares it
    #: before a ``finish``: a model that answers "Wrote ..." without writing
    #: anything is sent back to do the work instead of being believed.
    expects_write: str = ""


@dataclass(frozen=True)
class SlashCommand:
    """One entry of the composer's command palette."""

    name: str
    summary: str
    #: Prompt commands: build the message that is sent to the model.
    build: Callable[[str, str], CommandRun] | None = None
    #: App actions: the name of the ``AppController`` method that runs it.
    #: A command has one of the two, never both.
    action: str = ""
    capability: str = ""
    takes_arguments: bool = False

    @property
    def slash(self) -> str:
        return f"/{self.name}"


def _init_run(project_root: str, arguments: str = "") -> CommandRun:
    if not project_root or not os.path.isdir(project_root):
        raise CommandError("Open a project folder before running /init.")
    root = os.path.abspath(project_root)
    scan = project_instructions.scan(root)
    existing = project_instructions.read_raw(root)
    template = _INIT_PROMPT.replace("${path}", root)
    prompt = _append_arguments(template, arguments) + "\n\n" + _facts_block(scan, existing)
    return CommandRun(
        prompt=prompt,
        objective=f"Write {FILENAME} for {scan.name}",
        capability="terminal",
        expects_write=os.path.join(root, FILENAME),
    )


def _append_arguments(template: str, text: str) -> str:
    """Puts the user's tail into the prompt the way OpenCode's ``append`` does.

    A template with a ``$ARGUMENTS`` placeholder fills it where it stands
    (``/init focus on the test setup``); one without keeps the text as a
    closing paragraph. An empty tail adds nothing, so a bare ``/init`` sends
    the template alone.
    """
    value = str(text or "").strip()
    if "$ARGUMENTS" in template:
        return template.replace("$ARGUMENTS", value)
    return "\n\n".join(part for part in (template, value) if part)


COMMANDS: tuple[SlashCommand, ...] = (
    # `/init` stays first: a bare `/` plus Enter runs the highlighted row, and
    # that gesture has meant `/init` since the palette existed. An action that
    # rewrites session context should never be the accidental default.
    SlashCommand(
        name="init",
        summary=f"Generate or update {FILENAME} for this project",
        build=_init_run,
        capability="terminal",
    ),
    SlashCommand(
        name="compact",
        summary="Summarize this session to free context",
        action="_command_compact",
    ),
    SlashCommand(
        name="memory",
        summary="Open the user or project memory file",
        action="_command_memory",
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
        lines += ["", f"- `{FILENAME}` already exists ({size} bytes)"]
    return "\n".join(lines)


#: The prompt `/init` sends, ported from OpenCode's ``initialize.txt`` (the
#: ``init`` entry of its command plugin): same structure, same quality bar, same
#: handling of an existing file. ``$ARGUMENTS`` is filled with whatever the user
#: typed after ``/init`` and ``${path}`` is the project root, exactly like
#: OpenCode's ``append``/``replace`` pair. The scanner's facts are appended below
#: it, because this app can prove more than the template asks the model to find.
_INIT_TEMPLATE = """\
Create or update `{file}` for this repository.

The goal is a compact instruction file that helps future sessions avoid \
mistakes and ramp up quickly. Every line should answer: "Would an agent likely \
miss this without help?" If not, leave it out.

User-provided focus or constraints (honor these):
$ARGUMENTS

## How to investigate

Read the highest-value sources first:
- `README*`, root manifests, workspace config, lockfiles
- build, test, lint, formatter, typecheck, and codegen config
- CI workflows and pre-commit / task runner config
- existing instruction files (`{file}`, `CLAUDE.md`, `.cursor/rules/`, \
`.cursorrules`, `.github/copilot-instructions.md`)

If architecture is still unclear after reading config and docs, inspect a small \
number of representative code files to find the real entrypoints, package \
boundaries, and execution flow. Prefer reading the files that explain how the \
system is wired together over random leaf files.

Prefer executable sources of truth over prose. If docs conflict with config or \
scripts, trust the executable source and only keep what you can verify.

Use `list_directory`, `read_files`, `search_file_content` and `find_files` to \
look around, and read each file once: old tool output can drop out of your \
history, so note what matters as you go instead of reading the same range again.

## What to extract

Look for the highest-signal facts for an agent working in this repo:
- exact developer commands, especially non-obvious ones
- how to run a single test, a single package, or a focused verification step
- required command order when it matters, such as `lint -> typecheck -> test`
- monorepo or multi-package boundaries, ownership of major directories, and the \
real app/library entrypoints
- framework or toolchain quirks: generated code, migrations, codegen, build \
artifacts, special env loading, dev servers, infra deploy flow
- testing quirks: fixtures, integration test prerequisites, snapshot workflows, \
required services, flaky or expensive suites
- important constraints from existing instruction files worth preserving

Good `{file}` content is usually hard-earned context that took reading multiple \
files to infer.

## Questions

Only ask the user questions if the repo cannot answer something important, and \
only once, with `communicate_to_user` (type `question`). Good questions:
- undocumented team conventions
- branch / PR / release expectations
- missing setup or test prerequisites that are known but not written down

Do not ask about anything the repo already makes clear.

## Writing rules

Include only high-signal, repo-specific guidance such as:
- exact commands and shortcuts the agent would otherwise guess wrong
- architecture notes that are not obvious from filenames
- conventions that differ from language or framework defaults
- setup requirements, environment quirks, and operational gotchas
- references to existing instruction sources that matter

Exclude:
- generic software advice
- long tutorials or exhaustive file trees
- obvious language conventions
- speculative claims or anything you could not verify
- content better stored in another file referenced from `{file}`

When in doubt, omit.

Prefer short sections and bullets. If the repo is simple, keep the file simple. \
If the repo is large, summarize the few structural facts that actually change \
how an agent should work.

Keep the file between 2 and 6 KB and under 90 lines, with commands in backticks, \
and write it in the language the README uses.

`{file}` is the only file you write. Do not edit, rewrite or reformat another \
instruction file (`.github/copilot-instructions.md`, `CLAUDE.md`, \
`.cursor/rules`, ...) even when one exists and looks stale: mention it in \
`{file}` as the other file to keep in sync and leave its content to the user.

If `{file}` already exists in ${path}, improve it in place rather than rewriting \
blindly. Preserve verified useful guidance, delete fluff or stale claims, and \
reconcile it with the current codebase."""

_INIT_PROMPT = _INIT_TEMPLATE.replace("{file}", FILENAME)
