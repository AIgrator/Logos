"""Project instructions (AGENTS.md): scan a project and draft the file.

The file is the user's, not the application's: this module only *reads* facts
that already exist on disk (manifests, scripts, CI, conventions), renders a
draft from them and checks a model-written draft against the same facts. It
never invents a command: every command in the draft must come from a real
manifest line, because a wrong build command costs more than a missing one.

Scanning is stdlib-only and bounded: top-level entries, a fixed list of
manifest files and the CI workflows. No recursive walk of the whole tree, so
it stays fast on a large repository.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field

FILENAME = "AGENTS.md"
MAX_DRAFT_BYTES = 8 * 1024
MAX_DRAFT_LINES = 90
_README_LIMIT = 2000
_MAX_TREE = 14
_MAX_CI_COMMANDS = 8

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "dist",
    "build", "target", "out", "bin", "obj", ".next", ".nuxt", ".tox", "coverage",
    ".logos",
}

# A line that would be true of every project is noise: it spends the reader's
# context without changing a single decision.
_GENERIC_PHRASES = (
    "best practice", "clean code", "write meaningful", "use meaningful",
    "be careful", "read the documentation", "follow the conventions",
    "keep the code", "ensure code quality", "always write tests",
    "test your changes", "document your code", "use descriptive",
    "keep it simple", "follow the style guide", "avoid magic numbers",
)

_COMMAND_HEADS = {
    "uv", "python", "python3", "pip", "pytest", "ruff", "mypy", "black", "poetry",
    "npm", "pnpm", "yarn", "bun", "node", "npx", "deno", "vite", "next", "tsc",
    "cargo", "rustc", "go", "make", "just", "task", "docker", "docker-compose",
    "composer", "php", "bundle", "rake", "rails", "gradle", "./gradlew", "mvn",
    "dotnet", "cmake", "ninja", "tox", "nox", "hatch", "pdm", "git",
}

_PATH_RE = re.compile(r"^(?:\./|/|~|[A-Za-z]:\\|[\w.-]+/)[\w./\\*-]*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Fact:
    """One verifiable statement about the project and where it came from."""

    text: str
    source: str
    kind: str = "fact"  # command | stack | structure | convention | pitfall | env

    @property
    def command(self) -> str:
        """The bare command of a command fact (its first backticked span)."""
        if self.kind != "command":
            return ""
        match = re.search(r"`([^`]+)`", self.text)
        return match.group(1).strip() if match else ""


@dataclass
class Scan:
    """Everything the scanner could prove about one project."""

    root: str
    name: str
    facts: list[Fact] = field(default_factory=list)
    tree: list[str] = field(default_factory=list)
    readme: str = ""
    language: str = "en"
    stack: list[str] = field(default_factory=list)
    readme_commands: list[str] = field(default_factory=list)

    @property
    def commands(self) -> list[Fact]:
        return [fact for fact in self.facts if fact.kind == "command"]

    def by_kind(self, kind: str) -> list[Fact]:
        return [fact for fact in self.facts if fact.kind == kind]


def _read_text(path: str, limit: int = 200_000) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _exists(root: str, *names: str) -> bool:
    return any(os.path.exists(os.path.join(root, name)) for name in names)


def _glob_any(root: str, pattern: str) -> bool:
    import glob

    return bool(glob.glob(os.path.join(root, pattern)))


def _detect_language(root: str) -> str:
    """Russian only when the README is clearly written in it."""
    text = _read_text(os.path.join(root, "README.md"), _README_LIMIT)
    if not text:
        return "en"
    cyrillic = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    if cyrillic and cyrillic > latin * 0.4:
        return "ru"
    return "en"


_SCRIPT_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".sh", ".py")


def _is_command_line(line: str) -> bool:
    """A shell line that *runs* something, not a path or a fragment of code.

    Used for fenced blocks, where the model puts the commands it cares about.
    A bare ``src/index.ts`` is a path, while ``.venv\\Scripts\\pythonw.exe x.py``
    is an invocation, and the difference is the executable head.
    """
    text = _normalize_command(line.lstrip("$>").strip())
    if not text or text.startswith("#") or text.startswith("//"):
        return False
    head = text.split()[0]
    if head in _COMMAND_HEADS:
        return True
    return head.lower().endswith(_SCRIPT_SUFFIXES)


def _readme_commands(text: str) -> list[str]:
    """Commands the project's own README shows in fenced blocks.

    The README is the project's documentation, so a command printed there is
    evidence in the same sense a manifest line is - and for a script-style
    project it is often the only place the run command exists.
    """
    commands: list[str] = []
    seen: set[str] = set()
    in_fence = False
    for raw in str(text or "").splitlines():
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if not in_fence or not _is_command_line(raw):
            continue
        command = _normalize_command(raw.lstrip("$>").strip())
        if command.startswith("git clone"):
            # Cloning is how the project was obtained, not how it is worked on.
            continue
        if command and command not in seen:
            seen.add(command)
            commands.append(command)
        if len(commands) >= 24:
            break
    return commands


def _top_level(root: str) -> list[str]:
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    names = []
    for entry in entries:
        if entry.startswith(".") or entry in _SKIP_DIRS:
            continue
        names.append(entry + ("/" if os.path.isdir(os.path.join(root, entry)) else ""))
    return names


def _stack(root: str) -> list[str]:
    stack = []
    checks = (
        ("pyproject.toml", "Python (pyproject.toml)"),
        ("requirements.txt", "Python (requirements.txt)"),
        ("setup.py", "Python (setup.py)"),
        ("package.json", "Node.js"),
        ("Cargo.toml", "Rust (Cargo)"),
        ("go.mod", "Go modules"),
        ("Gemfile", "Ruby (Bundler)"),
        ("composer.json", "PHP (Composer)"),
        ("CMakeLists.txt", "CMake"),
        ("Dockerfile", "Docker"),
        ("docker-compose.yml", "Docker Compose"),
        ("docker-compose.yaml", "Docker Compose"),
    )
    for name, label in checks:
        if os.path.isfile(os.path.join(root, name)):
            stack.append(label)
    if _glob_any(root, "*.sln") or _glob_any(root, "*.csproj"):
        stack.append("C# (.NET)")
    return stack


def _node_runner(root: str, data: dict) -> str:
    manager = str(data.get("packageManager") or "").lower()
    if manager.startswith("pnpm") or os.path.isfile(os.path.join(root, "pnpm-lock.yaml")):
        return "pnpm"
    if manager.startswith("yarn") or os.path.isfile(os.path.join(root, "yarn.lock")):
        return "yarn"
    if manager.startswith("bun") or os.path.isfile(os.path.join(root, "bun.lockb")):
        return "bun run"
    return "npm run"


def _scan_package_json(root: str, facts: list[Fact]) -> None:
    path = os.path.join(root, "package.json")
    if not os.path.isfile(path):
        return
    try:
        data = json.loads(_read_text(path))
    except (ValueError, TypeError) as exc:
        logging.warning("Could not parse package.json: %s", exc)
        return
    if not isinstance(data, dict):
        return
    runner = _node_runner(root, data)
    scripts = data.get("scripts") or {}
    if isinstance(scripts, dict):
        for key, value in scripts.items():
            facts.append(Fact(
                f"`{runner} {key}` — {_clip(value, 70)}",
                f"package.json scripts.{key}",
                "command",
            ))
    if isinstance(data.get("workspaces"), (list, dict)):
        facts.append(Fact("Monorepo workspaces are declared in `package.json`.", "package.json workspaces", "structure"))


def _scan_pyproject(root: str, facts: list[Fact]) -> None:
    path = os.path.join(root, "pyproject.toml")
    if not os.path.isfile(path):
        return
    try:
        data = tomllib.loads(_read_text(path))
    except (ValueError, TypeError) as exc:
        logging.warning("Could not parse pyproject.toml: %s", exc)
        return
    runner = "uv run " if os.path.isfile(os.path.join(root, "uv.lock")) else ""
    project = data.get("project") or {}
    scripts = dict(project.get("scripts") or {})
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("scripts") or {}
    if isinstance(poetry, dict):
        scripts.update(poetry)
    for name in scripts:
        facts.append(Fact(f"`{runner}{name}`", f"pyproject.toml scripts.{name}", "command"))

    tool = data.get("tool") or {}
    if "pytest" in tool or os.path.isdir(os.path.join(root, "tests")):
        facts.append(Fact(f"`{runner}pytest -q`", "pyproject.toml [tool.pytest]", "command"))
    if "ruff" in tool:
        facts.append(Fact(f"`{runner}ruff check .`", "pyproject.toml [tool.ruff]", "command"))
        line_length = (tool.get("ruff") or {}).get("line-length")
        if isinstance(line_length, int):
            facts.append(Fact(f"ruff line length is {line_length}.", "pyproject.toml [tool.ruff]", "convention"))
    if "mypy" in tool:
        facts.append(Fact(f"`{runner}mypy .`", "pyproject.toml [tool.mypy]", "command"))
    if "black" in tool:
        facts.append(Fact(f"`{runner}black .`", "pyproject.toml [tool.black]", "command"))


def _scan_makefile(root: str, facts: list[Fact], filename: str, runner: str) -> None:
    path = os.path.join(root, filename)
    if not os.path.isfile(path):
        return
    text = _read_text(path, 60_000)
    seen = set()
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?!=)", line)
        if not match:
            continue
        target = match.group(1)
        if target in seen or target in {"PHONY", "help", "all"}:
            continue
        seen.add(target)
        facts.append(Fact(f"`{runner} {target}`", f"{filename} target {target}", "command"))
        if len(seen) >= 8:
            break


def _scan_ci(root: str, facts: list[Fact]) -> None:
    import glob

    workflows = sorted(glob.glob(os.path.join(root, ".github", "workflows", "*.yml"))) + sorted(
        glob.glob(os.path.join(root, ".github", "workflows", "*.yaml")))
    seen: set[str] = set()
    for path in workflows[:4]:
        rel = os.path.relpath(path, root).replace("\\", "/")
        for line in _read_text(path, 60_000).splitlines():
            match = re.match(r"^\s*-?\s*run:\s*(.+?)\s*$", line)
            if not match:
                continue
            command = match.group(1).strip().strip("'\"")
            if not command or command.startswith("echo ") or command in seen:
                continue
            seen.add(command)
            facts.append(Fact(f"`{_clip(command, 90)}`", rel, "command"))
            if len(seen) >= _MAX_CI_COMMANDS:
                return


def _scan_other_stacks(root: str, facts: list[Fact]) -> None:
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        facts.append(Fact("`cargo test`", "Cargo.toml", "command"))
        facts.append(Fact("`cargo build --release`", "Cargo.toml", "command"))
    if os.path.isfile(os.path.join(root, "go.mod")):
        facts.append(Fact("`go test ./...`", "go.mod", "command"))
        facts.append(Fact("`go build ./...`", "go.mod", "command"))
    if os.path.isfile(os.path.join(root, "composer.json")):
        try:
            data = json.loads(_read_text(os.path.join(root, "composer.json")))
        except (ValueError, TypeError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict):
            for name in list(scripts)[:5]:
                facts.append(Fact(f"`composer {name}`", f"composer.json scripts.{name}", "command"))


def _scan_conventions(root: str, facts: list[Fact]) -> None:
    if os.path.isfile(os.path.join(root, ".editorconfig")):
        facts.append(Fact("EditorConfig is used.", ".editorconfig", "convention"))
    if _glob_any(root, ".prettierrc*") or _glob_any(root, "prettier.config.*"):
        facts.append(Fact("Prettier formats the code.", "prettier config", "convention"))
    if _glob_any(root, "eslint.config.*") or _glob_any(root, ".eslintrc*"):
        facts.append(Fact("ESLint checks the code.", "eslint config", "convention"))
    tsconfig = os.path.join(root, "tsconfig.json")
    if os.path.isfile(tsconfig):
        text = _read_text(tsconfig, 20_000)
        if re.search(r'"strict"\s*:\s*true', text):
            facts.append(Fact("TypeScript runs in strict mode.", "tsconfig.json", "convention"))
    if os.path.isfile(os.path.join(root, ".pre-commit-config.yaml")):
        facts.append(Fact("pre-commit hooks are configured.", ".pre-commit-config.yaml", "convention"))


def _scan_pitfalls(root: str, facts: list[Fact]) -> None:
    has_test_command = any(
        fact.kind == "command" and re.search(r"\b(test|spec|vitest|jest|pytest)\b", fact.command)
        for fact in facts)
    if not has_test_command and not _exists(root, "tests", "test", "spec", "__tests__"):
        facts.append(Fact(
            "There is no test suite in this project; verify changes by running the project.",
            "top-level scan", "pitfall"))
    generated = [name for name in ("dist", "build", "out", "target", "coverage") if os.path.isdir(os.path.join(root, name))]
    if generated:
        listing = ", ".join(f"`{name}/`" for name in generated)
        facts.append(Fact(f"{listing} are generated; do not edit them by hand.", "top-level scan", "pitfall"))
    if os.path.isdir(os.path.join(root, "node_modules")):
        facts.append(Fact("`node_modules/` is installed output, never edit it.", "top-level scan", "pitfall"))
    if _exists(root, "vendor", "third_party", "external"):
        facts.append(Fact("Vendored code lives in `vendor/`, `third_party/` or `external/`; do not rewrite it.", "top-level scan", "pitfall"))
    env_example = next((name for name in (".env.example", ".env.sample", ".env.template") if os.path.isfile(os.path.join(root, name))), "")
    if env_example:
        keys = []
        for line in _read_text(os.path.join(root, env_example), 20_000).splitlines():
            match = re.match(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=", line)
            if match:
                keys.append(match.group(1))
            if len(keys) >= 8:
                break
        if keys:
            listing = ", ".join(f"`{key}`" for key in keys)
            facts.append(Fact(f"Configuration is read from `.env` ({listing}); never commit real values.", env_example, "env"))
    if os.path.isdir(os.path.join(root, ".logos")):
        facts.append(Fact("`.logos/` holds Logos session data; do not edit or commit it.", "top-level scan", "pitfall"))


def _entry_points(root: str) -> list[str]:
    found = []
    for name in ("main.py", "app.py", "manage.py", "src/main.py", "src/index.ts", "src/main.ts", "index.js", "src/main.rs", "cmd"):
        if os.path.exists(os.path.join(root, name)):
            found.append(name)
    return found


def _entry_commands(root: str, entries: list[str], facts: list[Fact]) -> None:
    """A Python entry point is a real run command: the file exists on disk."""
    for name in entries:
        if not name.endswith(".py"):
            continue
        runner = "uv run " if os.path.isfile(os.path.join(root, "uv.lock")) else ""
        facts.append(Fact(f"`{runner}python {name}`", f"top-level scan: {name}", "command"))
        return


def scan(project_root: str) -> Scan:
    """Collects every fact the scanner can prove about one project."""
    root = os.path.abspath(project_root)
    result = Scan(root=root, name=os.path.basename(root.rstrip("\\/")) or root)
    result.stack = _stack(root)
    result.tree = _top_level(root)[:_MAX_TREE]
    result.language = _detect_language(root)
    result.readme = _read_text(os.path.join(root, "README.md"), _README_LIMIT).strip()
    result.readme_commands = _readme_commands(
        _read_text(os.path.join(root, "README.md"), 200_000))

    facts = result.facts
    if result.stack:
        facts.append(Fact("Stack: " + ", ".join(result.stack) + ".", "manifests", "stack"))
    entries = _entry_points(root)
    if entries:
        listing = ", ".join(f"`{name}`" for name in entries)
        facts.append(Fact(f"Entry point: {listing}.", "top-level scan", "structure"))
        _entry_commands(root, entries, facts)
    _scan_package_json(root, facts)
    _scan_pyproject(root, facts)
    _scan_makefile(root, facts, "Makefile", "make")
    _scan_makefile(root, facts, "justfile", "just")
    _scan_other_stacks(root, facts)
    _scan_ci(root, facts)
    _scan_conventions(root, facts)
    _scan_pitfalls(root, facts)
    return result


def _section_for(fact: Fact) -> str:
    low = fact.text.lower()
    if any(word in low for word in ("test", "spec", "pytest", "vitest", "jest")):
        return "Test"
    if any(word in low for word in ("lint", "ruff", "eslint", "prettier", "mypy", "black", "format", "typecheck")):
        return "Lint"
    if any(word in low for word in ("build", "dist", "release", "package")):
        return "Build"
    return "Run"


def render_template(scan_result: Scan) -> str:
    """A draft built only from scan facts, for projects with no model configured."""
    lines = [f"# {FILENAME}", ""]
    stack = scan_result.by_kind("stack")
    if stack:
        lines += ["## What this is", "", stack[0].text, ""]
    structure = scan_result.by_kind("structure")
    if structure:
        lines += ["## Layout", ""] + [f"- {fact.text}" for fact in structure] + [""]

    commands = scan_result.commands
    for section in ("Run", "Test", "Lint", "Build"):
        picked = [fact for fact in commands if _section_for(fact) == section]
        if not picked:
            continue
        lines += [f"## {section}", ""]
        lines += [f"- {fact.text}" for fact in picked[:10]]
        lines.append("")

    if scan_result.readme_commands:
        lines += ["## Commands (from the README)", ""]
        lines += [f"- `{item}`" for item in scan_result.readme_commands[:12]]
        lines.append("")

    conventions = scan_result.by_kind("convention")
    if conventions:
        lines += ["## Conventions", ""] + [f"- {fact.text}" for fact in conventions] + [""]

    pitfalls = scan_result.by_kind("pitfall") + scan_result.by_kind("env")
    if pitfalls:
        lines += ["## Notes", ""] + [f"- {fact.text}" for fact in pitfalls] + [""]

    if len(lines) <= 2:
        lines += ["## What this is", "", "Describe the project here; the scanner found no manifests.", ""]
    return "\n".join(lines).rstrip() + "\n"


def draft_prompts(scan_result: Scan) -> tuple[str, str]:
    """System and user prompts for the model-written draft."""
    language = "Russian" if scan_result.language == "ru" else "English"
    evidence = "\n".join(f"- {fact.text}  [source: {fact.source}]" for fact in scan_result.facts)
    tree = ", ".join(scan_result.tree) or "(nothing detected)"
    readme = scan_result.readme[:1200] or "(no README)"
    readme_commands = "\n".join(f"- `{item}`" for item in scan_result.readme_commands) or "(none)"
    system = (
        "You write a project's AGENTS.md: the short instruction file an AI coding agent reads at the start "
        "of every session. Write it for an agent, not for a human reader.\n"
        "Hard rules:\n"
        "1. Use ONLY the facts listed under EVIDENCE. Never invent a command, path, URL, version or tool.\n"
        "2. Every command you write must appear verbatim in EVIDENCE or in the README commands list. If a step has "
        "no command, describe it in words or omit it.\n"
        "3. No generic advice ('write clean code', 'follow best practices', 'test your changes'): a line that is true of "
        "every project is noise. Every line must name something specific to THIS project.\n"
        "4. No absolute paths, no usernames, no secrets, no API keys.\n"
        "5. Prefer short imperative lines. Skip a section entirely rather than filling it with guesses.\n"
        "6. If EVIDENCE has no 'Stack:' fact, do not name a language, framework or runtime: the stack is "
        "unknown, and a guess is worse than a gap.\n"
        f"7. Write the file in {language}.\n"
        "8. At most 60 lines. Return only the Markdown file, with no preamble and no code fence around it.\n"
        "Sections, in this order, only when you have facts for them: '# AGENTS.md', '## What this is', '## Run', "
        "'## Test', '## Lint', '## Build', '## Conventions', '## Do not touch'."
    )
    user = (
        f"Project: {scan_result.name}\n"
        f"Detected stack: {', '.join(scan_result.stack) or 'unknown'}\n"
        f"Top-level entries: {tree}\n\n"
        f"EVIDENCE\n{evidence}\n\n"
        f"README commands (fenced blocks of the project's own README)\n{readme_commands}\n\n"
        f"README excerpt\n{readme}\n"
    )
    return system, user


def _normalize_command(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").split()).strip().strip("`")


def _looks_like_command(span: str) -> bool:
    span = _normalize_command(span)
    if not span or _PATH_RE.match(span):
        return False
    head = span.split()[0]
    return head in _COMMAND_HEADS


def _command_supported(span: str, commands: list[str]) -> bool:
    span = _normalize_command(span)
    if not span:
        return True
    if _PATH_RE.match(span):
        return True
    for command in commands:
        command = _normalize_command(command)
        if not command:
            continue
        # Accepted when the model used the manifest command as-is, trimmed it,
        # or appended flags to it. A shared first word is not enough: that
        # would let "uv run pytest" pass because "uv run ruff" exists.
        if span in command or command in span:
            return True
    return False


def _is_generic(line: str) -> bool:
    low = line.lower()
    return any(phrase in low for phrase in _GENERIC_PHRASES)


def _drop_empty_sections(lines: list[str]) -> list[str]:
    """Removes a heading whose body was entirely dropped (no facts to fill it)."""
    result: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+\S", line)
        if match:
            level = len(match.group(1))
            has_body = False
            for follower in lines[index + 1:]:
                deeper = re.match(r"^(#{1,6})\s+\S", follower)
                if deeper and len(deeper.group(1)) <= level:
                    break
                if follower.strip():
                    has_body = True
                    break
            if not has_body:
                continue
        result.append(line)
    return result


def _normalize_spacing(lines: list[str]) -> list[str]:
    """One blank line around every heading, and never two in a row.

    Models are inconsistent here (``# AGENTS.md`` immediately followed by
    ``## Run``), and the file is read by humans too.
    """
    spaced: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            spaced.append(line)
            continue
        heading = not in_fence and bool(re.match(r"^#{1,6}\s+\S", line))
        if heading and spaced and spaced[-1].strip():
            spaced.append("")
        spaced.append(line)
        if heading:
            spaced.append("")
    collapsed: list[str] = []
    for line in spaced:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line)
    return collapsed


def _drop_empty_fences(lines: list[str]) -> list[str]:
    """Removes a fenced block whose every command line was dropped."""
    result: list[str] = []
    index = 0
    while index < len(lines):
        if _FENCE_RE.match(lines[index]):
            body: list[str] = []
            cursor = index + 1
            while cursor < len(lines) and not _FENCE_RE.match(lines[cursor]):
                body.append(lines[cursor])
                cursor += 1
            if cursor < len(lines) and not any(item.strip() for item in body):
                index = cursor + 1
                continue
        result.append(lines[index])
        index += 1
    return result


def _drop_orphan_leads(lines: list[str]) -> list[str]:
    """Drops a lead-in line whose command was removed ("Deploy with:" and nothing)."""
    result: list[str] = []
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            result.append(line)
            continue
        if not in_fence and line.strip().endswith(":"):
            following = next((item for item in lines[index + 1:] if item.strip()), "")
            if not following.strip() or re.match(r"^#{1,6}\s+\S", following):
                continue
        result.append(line)
    return result


def _drop_commandless_sections(lines: list[str]) -> list[str]:
    """Drops ``## Run`` / ``## Test`` / ``## Lint`` / ``## Build`` with no command.

    A section named after a command that carries only prose ("verify changes by
    running the project") tells an agent nothing it can execute, and the same
    sentence is already in Notes.
    """
    names = {"run", "test", "lint", "build"}
    result: list[str] = []
    index = 0
    while index < len(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", lines[index])
        if not match or match.group(2).strip().lower() not in names:
            result.append(lines[index])
            index += 1
            continue
        level = len(match.group(1))
        body: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            deeper = re.match(r"^(#{1,6})\s+\S", lines[cursor])
            if deeper and len(deeper.group(1)) <= level:
                break
            body.append(lines[cursor])
            cursor += 1
        has_command = any(
            _is_command_line(line) or any(_looks_like_command(span) for span in re.findall(r"`([^`]+)`", line))
            for line in body)
        if has_command:
            result.append(lines[index])
            result.extend(body)
        index = cursor
    return result


def _ensure_title(lines: list[str]) -> list[str]:
    """The file opens with ``# AGENTS.md`` even when the model forgot it."""
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("# "):
            return lines
        return [f"# {FILENAME}", ""] + lines
    return lines


def clean_draft(text: str, scan_result: Scan) -> tuple[str, list[str]]:
    """Drops lines the scan cannot support; returns the kept text and the rejects."""
    commands = [fact.command for fact in scan_result.commands if fact.command]
    commands += scan_result.readme_commands
    kept: list[str] = []
    dropped: list[str] = []
    in_fence = False
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            kept.append(line)
            continue
        if not line.strip():
            kept.append(line)
            continue
        if in_fence:
            # A fenced block is where a model puts the commands it wants the
            # agent to run, so each command line is checked like a backticked
            # span. Comments and shell plumbing pass untouched.
            if _is_command_line(line) and not _command_supported(line, commands):
                dropped.append(line.strip())
                continue
            kept.append(line)
            continue
        if _is_generic(line):
            dropped.append(line.strip())
            continue
        spans = re.findall(r"`([^`]+)`", line)
        bad = [span for span in spans if _looks_like_command(span) and not _command_supported(span, commands)]
        if bad:
            dropped.append(line.strip())
            continue
        kept.append(line)

    kept = _ensure_title(_normalize_spacing(_drop_commandless_sections(
        _drop_empty_sections(_drop_orphan_leads(_drop_empty_fences(kept))))))
    if len(kept) > MAX_DRAFT_LINES:
        kept = kept[:MAX_DRAFT_LINES] + ["", f"<!-- Truncated at {MAX_DRAFT_LINES} lines. -->"]
    text = "\n".join(kept).strip() + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_DRAFT_BYTES:
        text = encoded[:MAX_DRAFT_BYTES].decode("utf-8", errors="ignore").rstrip() + "\n"
    return text, dropped


def audit(text: str, scan_result: Scan) -> list[str]:
    """Commands in the file that neither a manifest nor the README supports."""
    commands = [fact.command for fact in scan_result.commands if fact.command]
    commands += scan_result.readme_commands
    unknown: list[str] = []
    in_fence = False
    for raw in str(text or "").splitlines():
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            if _is_command_line(raw) and not _command_supported(raw, commands):
                unknown.append(_normalize_command(raw.lstrip("$>").strip()))
            continue
        for span in re.findall(r"`([^`]+)`", raw):
            if _looks_like_command(span) and not _command_supported(span, commands):
                unknown.append(_normalize_command(span))
    return sorted(set(unknown))


def read_raw(project_root: str) -> str:
    """The current file exactly as written, or an empty string."""
    path = os.path.join(project_root, FILENAME)
    if not os.path.isfile(path):
        return ""
    return _read_text(path).replace("\r\n", "\n")


def write(project_root: str, text: str) -> str:
    """Writes ``AGENTS.md`` and returns its absolute path."""
    path = os.path.join(project_root, FILENAME)
    body = str(text or "").replace("\r\n", "\n").rstrip() + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    logging.info("Wrote project instructions: %s (%d bytes)", path, len(body.encode("utf-8")))
    return path
