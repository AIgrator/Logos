"""The research pass behind the single "Generate AGENTS.md" action.

The user presses one button; everything else happens here. The model is given
the project's tree, the facts the deterministic scanner already proved, and a
small read-only toolset, and it walks the repository until it can write the
instruction file. That is the difference between a one-shot draft from a handful
of facts and a file that knows where the real entry point is.

The loop is deliberately tiny and not a second agent framework: one request per
step, one tool call per reply, the tool result appended to the conversation, and
the finished Markdown ending it. Only read-only tools are offered, so the pass
cannot touch the project even if the model asks it to.

The deterministic scanner stays the source of truth for *commands*: every
command in the finished file is checked against the manifests, the README and
the files the pass actually read (``ResearchResult.evidence_commands``), and an
unsupported one is dropped and shown to the user.
"""
from __future__ import annotations

import json
import logging
import os
import re

MAX_STEPS = 14
MAX_TOOL_OUTPUT = 3000
MAX_SEED_CHARS = 6000
MAX_EXISTING_CHARS = 4000
#: A file written from the seed facts alone is a guess; the pass has to look.
MIN_TOOLS = 3
MAX_PUSHES = 2

#: The only tools the pass may use. All of them read.
RESEARCH_TOOLS = (
    "list_directory",
    "read_files",
    "search_file_content",
    "find_files",
    "path_exists",
)

#: Folders a reader never needs to see in a project tree.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "out", "target", "coverage", "site-packages",
}

SYSTEM_PROMPT = """You are writing the AGENTS.md of the project in front of you: the short instruction file an AI coding agent reads at the start of every session.

Work like a careful newcomer who has to be productive in this repository within ten minutes:
1. Look around first: the tree, README, manifests, tool configs, CI, and any existing instruction file (AGENTS.md, CLAUDE.md, .cursorrules, .github/copilot-instructions.md, opencode.json).
2. Read the files that decide how the project is built and run. Prefer the executable source of truth (a manifest, a config, a build spec) over prose; if the docs and the config disagree, trust the config.
3. Follow the real entry point far enough to say what the program does and where the wiring lives.
4. Only then write the file.

Reply with EXACTLY ONE of these two shapes, and nothing else:
- a tool call: {{"name": "<tool>", "arguments": {{...}}}}
- the finished file, starting with the line "# AGENTS.md"

You have at most {max_steps} tool calls. Spend them on the highest-value files; never list the same directory twice.

What the file must contain, in this order, and only when you have a fact for it:
- One short paragraph: what the program does, the real entry point, the supported platform.
- ## Commands - the exact commands a developer runs: run, lint, test, build, installer. Use the project's own environment (its venv, uv, pnpm, make...). When a command does not exist (no test suite, no typecheck), say so instead of inventing one.
- ## Architecture - only what a reader cannot guess from filenames: what talks to what, where the wiring lives, which module is the real entry point and which look like one but are not.
- ## Conventions - only what differs from language or framework defaults; toolchain behaviour that bites: required import order, generated files, a version pinned in two places, a per-user installer, where data is written at runtime.
- ## Do not touch - files, folders or actions that would break the project.

Rules:
- Every command must exist: it comes from a manifest, a config, the README, or a file you read. Never invent a command, path, version or URL.
- Every line must be specific to THIS project. "Write clean code", "follow best practices", "test your changes" are noise: leave them out.
- No absolute paths, no usernames, no secrets, no API keys.
- Aim for 2-6 KB and never more than 90 lines. Short sections and bullets, no tutorials.
- Write in {language}.
- If an AGENTS.md already exists, improve it in place: keep what is still true, delete what the code contradicts.
"""

TOOL_NUDGE = (
    "That was not a tool call. Reply with exactly one JSON object "
    '{"name": "<tool>", "arguments": {...}}, or with the finished file starting with "# AGENTS.md".'
)

LOOK_FIRST_NUDGE = (
    "Do not write the file yet: you have read almost nothing. Look at the project first. "
    "Read the manifest, the tool config (lint/test/build) and the file the real entry point "
    "lives in, and search for the wiring. Reply with one tool call."
)

FINAL_NUDGE = (
    "The tool budget is exhausted. Do not call more tools. Write the finished AGENTS.md now, "
    'starting with the line "# AGENTS.md".'
)


class ResearchError(RuntimeError):
    """A failure the dialog can show as one readable line."""


class ResearchResult:
    """The finished text plus the trail of what the pass looked at."""

    def __init__(self, text: str = "", trail: list[dict] | None = None, steps: int = 0,
                 evidence_commands: list[str] | None = None, exhausted: bool = False):
        self.text = text
        self.trail = trail or []
        self.steps = steps
        self.evidence_commands = evidence_commands or []
        self.exhausted = exhausted

    def summary(self) -> str:
        """One line for the status label."""
        read = sum(1 for item in self.trail if item.get("tool") == "read_files" and item.get("ok"))
        looked = len({item.get("target", "") for item in self.trail if item.get("ok")} - {""})
        return (f"Read {read} file{'s' if read != 1 else ''}, looked at {looked} "
                f"path{'s' if looked != 1 else ''} in {self.steps} step{'s' if self.steps != 1 else ''}")


# -- prompt material -----------------------------------------------------


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} characters omitted]"


def _shallow_tree(root: str, limit: int = 140) -> list[str]:
    """A shallow tree, so the model knows which files are worth reading."""
    entries: list[str] = []

    def walk(base: str, depth: int) -> None:
        if len(entries) >= limit:
            return
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return
        for name in names:
            if len(entries) >= limit:
                return
            if name.startswith(".") and name not in (".github", ".logos", ".env.example"):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            if os.path.isdir(path):
                entries.append(rel + "/")
                if name not in SKIP_DIRS and depth < 2:
                    walk(path, depth + 1)
            else:
                entries.append(rel)

    walk(root, 0)
    return entries


def _tool_text(computer) -> str:
    """The tool contract, read from the bound Computer so it cannot drift."""
    lines = []
    for name in RESEARCH_TOOLS:
        func = getattr(computer, name, None)
        if func is None:
            continue
        meta = (getattr(func, "__tool_meta__", {}) or {}).get("tool", {})
        description = meta.get("strategic") or meta.get("description") or ""
        arguments = ", ".join((meta.get("arguments") or {}).keys())
        lines.append(f"- {name}({arguments}): {description}")
    return "\n".join(lines)


def _seed_prompt(scan_result, existing: str, tree: list[str]) -> str:
    language = "Russian" if scan_result.language == "ru" else "English"
    facts = "\n".join(f"- {fact.text}  [source: {fact.source}]" for fact in scan_result.facts)
    commands = "\n".join(f"- `{item}`" for item in scan_result.readme_commands) or "(none)"
    parts = [
        f"PROJECT: {scan_result.name}",
        f"Write the file in {language}.",
        "",
        "FACTS THE SCANNER ALREADY PROVED",
        _clip(facts, MAX_SEED_CHARS) or "(nothing detected)",
        "",
        "COMMANDS PRINTED IN THE PROJECT'S OWN DOCS",
        _clip(commands, 2000),
        "",
        "TREE",
        _clip(", ".join(tree), 4000) or "(empty)",
    ]
    if existing.strip():
        parts += [
            "",
            "EXISTING AGENTS.md (improve it, do not rewrite it blindly)",
            _clip(existing.strip(), MAX_EXISTING_CHARS),
        ]
    parts += [
        "",
        "Start with the files that decide how this project is built and run, then write the file.",
    ]
    return "\n".join(parts)


# -- reply parsing -------------------------------------------------------


def _unwrap_fence(text: str) -> str:
    """Drops a ```markdown wrapper a model puts around the whole file."""
    stripped = text.strip()
    match = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _final_text(reply: str) -> str:
    """The finished file, when the reply is one."""
    text = _unwrap_fence(reply)
    if not text or text.startswith("{"):
        return ""
    if text.startswith("# AGENTS.md"):
        return text
    if re.match(r"^#{1,2}\s+\S", text) and "## " in text:
        return text
    return ""


def _parse_call(reply: str) -> dict | None:
    """One tool call, in any of the shapes models actually produce."""
    text = _unwrap_fence(reply)
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start:end + 1]
    data = None
    try:
        data = json.loads(text)
    except ValueError:
        try:
            from llm.llm_parsing import repair_json_string

            data = repair_json_string(text)
        except Exception:  # noqa: BLE001 - an unparsable reply is a nudge, not a crash
            return None
    if not isinstance(data, dict):
        return None
    name = data.get("name") or data.get("tool") or data.get("action")
    if not isinstance(name, str):
        return None
    arguments = data.get("arguments") or data.get("payload") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    return {"name": name, "arguments": arguments}


def _target(arguments: dict) -> str:
    """A short human label for the trail: what the call is aimed at."""
    for key in ("paths", "path", "pattern", "query", "directory", "file", "files"):
        value = arguments.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if value:
            return _clip(str(value), 120)
    return ""


# -- the loop ------------------------------------------------------------


def _ask(provider, system_prompt: str, history: list[dict]) -> str:
    response = provider.get_response(
        prompt=history[-1]["content"],
        attachments=[],
        system_prompt=system_prompt,
        history=history[:-1],
        require_structured=False,
    )
    text = ""
    if isinstance(response, dict):
        text = response.get("text") or ""
        if not text:
            tool_calls = response.get("tool_calls")
            if isinstance(tool_calls, dict):
                text = tool_calls.get("text") or ""
    return str(text or "").strip()


def research(project_root: str, settings_manager, settings, on_progress=None,
             max_steps: int = MAX_STEPS, cancel_event=None) -> ResearchResult:
    """Walks the project read-only and returns the finished AGENTS.md text.

    ``on_progress(tool, detail)`` is called once per executed step, so the dialog
    can show the same trail the model worked through.
    """
    from computer.computer import Computer
    from context import project_instructions
    from executor.executor import Executor
    from llm.llm_worker import create_llm_provider

    def progress(tool: str, detail: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(tool, detail)
        except Exception:  # noqa: BLE001 - progress must never break the pass
            logging.debug("Project instructions progress callback failed", exc_info=True)

    provider = create_llm_provider(settings_manager, settings)
    if provider is None:
        raise ResearchError(
            "No model is configured. Connect a provider in Settings → Providers.")

    scan_result = project_instructions.scan(project_root)
    existing = project_instructions.read_raw(project_root)
    computer = Computer(settings_manager=settings_manager, mode="terminal",
                        working_directory=project_root)
    executor = Executor(computer, ui=None)
    system_prompt = SYSTEM_PROMPT.format(
        max_steps=max_steps,
        language="Russian" if scan_result.language == "ru" else "English",
    ) + "\nAvailable tools:\n" + _tool_text(computer)

    history: list[dict] = [{
        "role": "user",
        "content": _seed_prompt(scan_result, existing, _shallow_tree(project_root)),
    }]
    trail: list[dict] = []
    evidence: list[str] = []
    steps = 0
    pushes = 0

    for step in range(1, max_steps + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise ResearchError("Generation was cancelled.")
        reply = _ask(provider, system_prompt, history)
        if not reply:
            raise ResearchError("The model returned an empty reply.")

        final = _final_text(reply)
        if final:
            if len(trail) < MIN_TOOLS and pushes < MAX_PUSHES:
                # The seed already carries enough to sound convincing, and a
                # model will happily write a plausible file from it. The pass
                # exists to read the code, so the first answers are sent back.
                pushes += 1
                history.append({"role": "assistant", "content": _clip(reply, 800)})
                history.append({"role": "user", "content": LOOK_FIRST_NUDGE})
                continue
            return ResearchResult(final, trail, len(trail), evidence)

        call = _parse_call(reply)
        if call is None:
            history.append({"role": "assistant", "content": _clip(reply, 1500)})
            history.append({"role": "user", "content": TOOL_NUDGE})
            continue

        name = call["name"]
        if name in ("finish", "communicate_to_user"):
            message = str(call["arguments"].get("message") or "").strip()
            if message:
                return ResearchResult(message, trail, len(trail), evidence)
            break
        if name not in RESEARCH_TOOLS:
            history.append({"role": "assistant", "content": json.dumps(call, ensure_ascii=False)})
            history.append({"role": "user", "content": (
                f"Tool '{name}' is not available in this pass. Use only: "
                f"{', '.join(RESEARCH_TOOLS)}.")})
            continue

        steps = step
        detail = _target(call["arguments"])
        progress(name, detail)
        result = executor.execute(call)
        ok = bool(result.ok)
        text = result.output_text if ok else (result.error or "The tool failed.")
        if ok and name == "read_files":
            for command in project_instructions.commands_in_text(text):
                if command not in evidence:
                    evidence.append(command)
        trail.append({"step": step, "tool": name, "target": detail, "ok": ok})
        history.append({"role": "assistant", "content": json.dumps(call, ensure_ascii=False)})
        history.append({"role": "user", "content": (
            f"RESULT of {name} ({'ok' if ok else 'error'}):\n{_clip(text)}\n\n"
            "Continue with the next file, or write the finished file.")})

    history.append({"role": "user", "content": FINAL_NUDGE})
    reply = _ask(provider, system_prompt, history)
    final = _final_text(reply)
    if final:
        return ResearchResult(final, trail, steps, evidence, exhausted=True)
    call = _parse_call(reply)
    if call is not None and call["name"] in ("finish", "communicate_to_user"):
        message = str(call["arguments"].get("message") or "").strip()
        if message:
            return ResearchResult(message, trail, steps, evidence, exhausted=True)
    raise ResearchError(
        "The model never wrote the file. Try again, or edit the draft by hand.")
