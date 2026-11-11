"""Project instructions (AGENTS.md): the facts the ``/init`` prompt starts from.

The file is the user's, not the application's: this module only *reads* facts
that already exist on disk (manifests, scripts, CI, conventions) and hands them
to the agent, which reads the code and writes the file itself. It never invents
a command: every command it reports comes from a real manifest line or from the
project's own documentation, because a wrong build command costs more than a
missing one.

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
_README_LIMIT = 2000
_MAX_TREE = 14
_MAX_CI_COMMANDS = 8

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "dist",
    "build", "target", "out", "bin", "obj", ".next", ".nuxt", ".tox", "coverage",
    ".logos",
}

_COMMAND_HEADS = {
    "uv", "python", "python3", "pip", "pytest", "ruff", "mypy", "black", "poetry",
    "npm", "pnpm", "yarn", "bun", "node", "npx", "deno", "vite", "next", "tsc",
    "cargo", "rustc", "go", "make", "just", "task", "docker", "docker-compose",
    "composer", "php", "bundle", "rake", "rails", "gradle", "./gradlew", "mvn",
    "dotnet", "cmake", "ninja", "tox", "nox", "hatch", "pdm", "git",
}

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


def _venv_dir(root: str) -> str:
    """The project's own virtual environment, when it has one."""
    for name in ("venv", ".venv", "env"):
        base = os.path.join(root, name)
        if os.name == "nt":
            if os.path.isfile(os.path.join(base, "Scripts", "python.exe")):
                return name
        elif os.path.isfile(os.path.join(base, "bin", "python")):
            return name
    return ""


def _python_command(root: str) -> str:
    """How this project's own interpreter is invoked, not a guess."""
    if os.path.isfile(os.path.join(root, "uv.lock")):
        return "uv run python"
    venv = _venv_dir(root)
    if venv:
        if os.name == "nt":
            return f"{venv}\\Scripts\\python.exe"
        return f"{venv}/bin/python"
    return "python"


def _tool_command(root: str, tool: str) -> str:
    """``ruff`` / ``pytest`` / ... through the same environment as the app."""
    if os.path.isfile(os.path.join(root, "uv.lock")):
        return f"uv run {tool}"
    python = _python_command(root)
    if python != "python":
        return f"{python} -m {tool}"
    return tool


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
        facts.append(Fact(f"`{_tool_command(root, 'pytest')} -q`", "pyproject.toml [tool.pytest]", "command"))
    if "ruff" in tool:
        facts.append(Fact(f"`{_tool_command(root, 'ruff')} check .`", "pyproject.toml [tool.ruff]", "command"))
        line_length = (tool.get("ruff") or {}).get("line-length")
        if isinstance(line_length, int):
            facts.append(Fact(f"ruff line length is {line_length}.", "pyproject.toml [tool.ruff]", "convention"))
    if "mypy" in tool:
        facts.append(Fact(f"`{_tool_command(root, 'mypy')} .`", "pyproject.toml [tool.mypy]", "command"))
    if "black" in tool:
        facts.append(Fact(f"`{_tool_command(root, 'black')} .`", "pyproject.toml [tool.black]", "command"))


def _scan_standalone_configs(root: str, facts: list[Fact]) -> None:
    """Tool configs that live outside ``pyproject.toml`` (``.ruff.toml``, ...)."""
    for name in ("ruff.toml", ".ruff.toml"):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        facts.append(Fact(f"`{_tool_command(root, 'ruff')} check .`", name, "command"))
        try:
            data = tomllib.loads(_read_text(path))
        except (ValueError, TypeError):
            data = {}
        line_length = data.get("line-length") if isinstance(data, dict) else None
        if isinstance(line_length, int):
            facts.append(Fact(f"ruff line length is {line_length}.", name, "convention"))
        break
    for name in ("pytest.ini", "tox.ini"):
        if os.path.isfile(os.path.join(root, name)):
            facts.append(Fact(f"`{_tool_command(root, 'pytest')} -q`", name, "command"))
            break


def _scan_requirements(root: str, facts: list[Fact]) -> None:
    if not os.path.isfile(os.path.join(root, "requirements.txt")):
        return
    if os.path.isfile(os.path.join(root, "pyproject.toml")):
        # A PEP 621 project installs through its own tool (uv, pip install .),
        # and pyproject or the README already names that step; a pip line here
        # would be a second, competing instruction.
        return
    facts.append(Fact(
        f"`{_python_command(root)} -m pip install -r requirements.txt`",
        "requirements.txt", "command"))


def _scan_builders(root: str, facts: list[Fact], readme_commands: list[str]) -> None:
    """Packaging entry points: a PyInstaller spec and an Inno Setup script."""
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if name.endswith(".spec") and os.path.isfile(os.path.join(root, name)):
            if any("pyinstaller" in command.lower() for command in readme_commands):
                break  # the README already states how the exe is built
            if os.path.isfile(os.path.join(root, "uv.lock")):
                command = f"uv run --with pyinstaller pyinstaller {name} --noconfirm"
            else:
                command = f"{_python_command(root)} -m PyInstaller {name} --noconfirm"
            facts.append(Fact(f"`{command}`", name, "command"))
            break
    installer_dir = os.path.join(root, "installer")
    if os.path.isdir(installer_dir):
        scripts = sorted(item for item in os.listdir(installer_dir) if item.endswith(".iss"))
        if scripts:
            facts.append(Fact(
                f"`installer/{scripts[0]}` builds the Windows installer with Inno Setup.",
                "installer/", "structure"))


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


def _has_test_files(root: str) -> bool:
    """A test folder is only a test suite when it actually holds tests.

    ``scribe/tests`` exists but carries a manual scanner script, and counting
    the folder alone hid the "no test suite" pitfall that the file needs.
    """
    import glob

    patterns = ("**/test_*.py", "**/*_test.py", "**/*.test.ts", "**/*.test.js",
                "**/*.spec.ts", "**/*.spec.js", "**/*.test.tsx", "**/*.test.jsx")
    candidates: list[str] = []
    for base in ("tests", "test", "__tests__"):
        path = os.path.join(root, base)
        if not os.path.isdir(path):
            continue
        for pattern in patterns:
            candidates.extend(glob.glob(os.path.join(path, pattern), recursive=True))
    candidates.extend(glob.glob(os.path.join(root, "test_*.py")))
    # A file named like a test is not a test suite: `scribe/tests` holds a
    # manual scanner script, so the content has to show a real test.
    test_marker = re.compile(r"(?m)^\s*(?:async\s+)?def test_|@pytest|describe\(|\bit\(|test\(")
    for path in candidates[:20]:
        if test_marker.search(_read_text(path, 20_000)):
            return True
    return False


_TEST_COMMAND_RE = re.compile(
    r"(?<![\w.])(pytest|vitest|jest|tox|nox|unittest|test|spec)(?![\w.])", re.IGNORECASE)


def _scan_pitfalls(root: str, facts: list[Fact]) -> None:
    venv = _venv_dir(root)
    if venv and not os.path.isfile(os.path.join(root, "uv.lock")):
        facts.append(Fact(
            f"`{venv}/` is the project's own environment; run tools from it instead of creating another one.",
            "top-level scan", "convention"))
    # ``scribe.spec`` is a PyInstaller spec, not a test: the dot in front of
    # "spec" is what separates a build file from a test command.
    has_test_command = any(
        fact.kind == "command" and _TEST_COMMAND_RE.search(fact.command)
        for fact in facts)
    if not has_test_command and not _has_test_files(root):
        if _exists(root, "tests", "test", "__tests__"):
            text = ("No test suite is configured: the test folder holds no test files and no "
                    "manifest defines a test command.")
        else:
            text = "There is no test suite in this project; verify changes by running the project."
        facts.append(Fact(text, "top-level scan", "pitfall"))
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
    copilot = ".github/copilot-instructions.md"
    if os.path.isfile(os.path.join(root, copilot)):
        facts.append(Fact(
            f"`{copilot}` also carries instructions; keep it in sync with AGENTS.md.",
            copilot, "convention"))


def _entry_points(root: str) -> list[str]:
    found = []
    for name in ("run.py", "main.py", "app.py", "cli.py", "bot.py", "manage.py",
                 "src/main.py", "src/run.py", "src/index.ts", "src/main.ts",
                 "index.js", "src/main.rs", "cmd"):
        if os.path.exists(os.path.join(root, name)):
            found.append(name)
    return found


def _entry_commands(root: str, entries: list[str], facts: list[Fact]) -> None:
    """A Python entry point is a real run command: the file exists on disk."""
    for name in entries:
        if not name.endswith(".py"):
            continue
        facts.append(Fact(
            f"`{_python_command(root)} {name}`",
            f"top-level scan: {name}", "command"))
        return


def scan(project_root: str) -> Scan:
    """Collects every fact the scanner can prove about one project."""
    root = os.path.abspath(project_root)
    result = Scan(root=root, name=os.path.basename(root.rstrip("\\/")) or root)
    result.stack = _stack(root)
    result.tree = _top_level(root)[:_MAX_TREE]
    result.language = _detect_language(root)
    result.readme = _read_text(os.path.join(root, "README.md"), _README_LIMIT).strip()
    result.readme_commands = []
    for name in ("README.md", "BUILDING.md", "CONTRIBUTING.md", "DEVELOPMENT.md"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            result.readme_commands.extend(_readme_commands(_read_text(path, 200_000)))
    result.readme_commands = list(dict.fromkeys(result.readme_commands))[:24]

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
    _scan_standalone_configs(root, facts)
    _scan_requirements(root, facts)
    _scan_builders(root, facts, result.readme_commands)
    _scan_makefile(root, facts, "Makefile", "make")
    _scan_makefile(root, facts, "justfile", "just")
    _scan_other_stacks(root, facts)
    _scan_ci(root, facts)
    _scan_conventions(root, facts)
    _scan_pitfalls(root, facts)
    return result


def _normalize_command(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").split()).strip().strip("`")


def read_raw(project_root: str) -> str:
    """The current file exactly as written, or an empty string."""
    path = os.path.join(project_root, FILENAME)
    if not os.path.isfile(path):
        return ""
    return _read_text(path).replace("\r\n", "\n")
