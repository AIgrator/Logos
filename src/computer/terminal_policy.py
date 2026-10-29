"""Permission policy for Terminal tool calls.

Modes mirror the access-permission pickers of agent IDEs:

- ``ask``  - read-only shell commands and edits inside the project run
  without a prompt; state-changing shell commands, network tools, deletes,
  sensitive paths and anything outside the project ask first.
- ``auto`` - only detected risk asks: destructive commands, sensitive
  paths and paths outside the project.
- ``full`` - never ask.

The policy is best-effort. A shell command that cannot be proven read-only
is treated as state-changing, and a script can always hide intent, so the
risk list covers the patterns where a miss would be expensive.
"""
from __future__ import annotations

import json
import os
import re

from src.computer.tools.locations import expand_path_tokens

ACCESS_MODES = ("ask", "auto", "full")
ACCESS_MODE_LABELS = {
    "ask": "Ask every time",
    "auto": "Auto approve",
    "full": "Full access",
}
DEFAULT_ACCESS_MODE = "ask"

_READ_ONLY_TOOLS = {
    "read_files", "list_directory", "search_file_content", "find_files",
    "path_exists", "get_clipboard_content", "use_skill", "wait_seconds",
    "poll_shell_job", "stop_shell_job",
}

_NETWORK_TOOLS = {"web_fetchs", "web_search", "download_file"}

_FILE_MUTATIONS = {
    "write_file", "replace", "replace_many", "create_directory",
    "delete_files", "move_files", "copy_files",
}

_ACTION_SUMMARIES = {
    "execute_shell": "Run a shell command",
    "write_file": "Write a file",
    "replace": "Edit a file",
    "replace_many": "Edit a file",
    "create_directory": "Create a directory",
    "delete_files": "Delete files",
    "move_files": "Move files",
    "copy_files": "Copy files",
    "download_file": "Download a file",
    "web_fetchs": "Open web pages",
    "web_search": "Search the web",
}

_SENSITIVE_DIRS = {".ssh", ".aws", ".azure", ".gnupg", ".kube"}
_SENSITIVE_NAMES = {
    ".netrc", ".npmrc", ".pypirc", ".pgpass", ".git-credentials",
    "credentials.json", "secrets.json",
}
_PRIVATE_KEY_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore", ".kdbx")
_ENV_TEMPLATES = {"example", "sample", "template", "dist"}


def normalize_mode(mode) -> str:
    value = str(mode or "").strip().lower()
    return value if value in ACCESS_MODES else DEFAULT_ACCESS_MODE


def evaluate(action_name, action_args, project_root: str = "",
             mode: str = DEFAULT_ACCESS_MODE) -> dict | None:
    """Approval request for one Terminal tool call, or None to run it."""
    mode = normalize_mode(mode)
    if mode == "full":
        return None
    name = str(action_name or "")
    args = action_args if isinstance(action_args, dict) else {}
    if name in _READ_ONLY_TOOLS:
        return None
    if name == "execute_shell":
        return _shell_request(args, project_root, mode)
    if name in _NETWORK_TOOLS:
        return _network_request(name, args, project_root, mode)
    if name in _FILE_MUTATIONS:
        return _file_request(name, args, project_root, mode)
    if mode == "ask":
        detail = json.dumps(args, ensure_ascii=False, indent=2) if args else ""
        return _request(mode, name,
                        f"Run {name.replace('_', ' ') or 'a tool'}",
                        detail, ["Runs a tool that may change state"])
    return None


# -- paths ------------------------------------------------------------------

def _text(value) -> str:
    return str(value or "").strip()


def _first(args: dict, *keys) -> str:
    for key in keys:
        value = _text(args.get(key))
        if value:
            return value
    return ""


def _list(args: dict, key) -> list[str]:
    value = args.get(key)
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value if _text(item)]
    return []


def _resolve(path, project_root) -> str:
    text = expand_path_tokens(_text(path), project_root)
    if not text:
        return ""
    text = os.path.expanduser(text)
    if not os.path.isabs(text):
        text = os.path.join(project_root or os.getcwd(), text)
    return os.path.normpath(os.path.abspath(text))


def _within(path, project_root) -> bool:
    if not path or not project_root:
        return True
    try:
        path_norm = os.path.normcase(os.path.realpath(path))
        root_norm = os.path.normcase(os.path.realpath(project_root))
        return os.path.commonpath([path_norm, root_norm]) == root_norm
    except ValueError:
        return False


def _is_sensitive(path) -> bool:
    text = str(path or "").replace("\\", "/")
    parts = [part for part in text.split("/") if part]
    if not parts:
        return False
    name = parts[-1].lower()
    if name == ".env":
        return True
    if name.startswith(".env.") and name[5:] not in _ENV_TEMPLATES:
        return True
    if name in _SENSITIVE_NAMES or name in _PRIVATE_KEY_NAMES:
        return True
    if name.endswith(_SENSITIVE_SUFFIXES):
        return True
    return any(part.lower() in _SENSITIVE_DIRS for part in parts[:-1])


def _paths_for(name: str, args: dict):
    """(written, touched) paths. Copy sources are read, not written."""
    written: list[str] = []
    touched: list[str] = []
    if name in {"write_file", "create_directory", "replace", "replace_many"}:
        path = _first(args, "path", "file_path")
        written.append(path)
        touched.append(path)
    elif name == "delete_files":
        paths = _list(args, "paths")
        written.extend(paths)
        touched.extend(paths)
    elif name in {"copy_files", "move_files"}:
        sources = _list(args, "source_paths")
        destination = _first(args, "destination_path")
        touched.extend(sources)
        touched.append(destination)
        if name == "move_files":
            written.extend(sources)
        written.append(destination)
    return [path for path in written if path], [path for path in touched if path]


def _mutation_detail(name: str, args: dict) -> str:
    if name in {"write_file", "create_directory", "replace_many"}:
        return _first(args, "path")
    if name == "replace":
        return _first(args, "file_path", "path")
    if name == "delete_files":
        return "\n".join(_list(args, "paths"))
    if name in {"copy_files", "move_files"}:
        sources = _list(args, "source_paths")
        destination = _first(args, "destination_path")
        lines = list(sources)
        if destination:
            lines.append(f"\u2192 {destination}")
        return "\n".join(lines)
    return ""


def _file_request(name: str, args: dict, project_root: str, mode: str):
    written, touched = _paths_for(name, args)
    if not written and not touched:
        return None
    detail = _mutation_detail(name, args)
    outside = []
    for raw in written:
        resolved = _resolve(raw, project_root)
        resolved_root = _resolve(project_root, project_root)
        if resolved and resolved_root and not _within(resolved, resolved_root):
            if raw not in outside:
                outside.append(raw)
    sensitive = []
    for raw in touched:
        resolved = _resolve(raw, project_root) or raw
        if _is_sensitive(resolved) and raw not in sensitive:
            sensitive.append(raw)
    if mode == "ask" and name != "delete_files" and not outside and not sensitive:
        return None
    reasons = []
    if name == "delete_files":
        reasons.append("Deletes files")
    if outside:
        reasons.append("Changes files outside the project")
    if sensitive:
        reasons.append("Touches sensitive files")
    if mode == "auto" and not reasons:
        return None
    if not reasons:
        reasons.append("Changes files")
    return _request(mode, name, _ACTION_SUMMARIES.get(name, "Change files"),
                    detail, reasons)


# -- network tools ----------------------------------------------------------

def _network_request(name: str, args: dict, project_root: str, mode: str):
    if name == "download_file":
        url = _first(args, "url")
        save_path = _first(args, "save_path")
        if not url and not save_path:
            return None
        detail = f"{url}\n\u2192 {save_path}" if save_path else url
        if mode == "auto":
            resolved = _resolve(save_path, project_root)
            resolved_root = _resolve(project_root, project_root)
            reasons = []
            if resolved and resolved_root and not _within(resolved, resolved_root):
                reasons.append("Writes outside the project")
            if resolved and _is_sensitive(resolved):
                reasons.append("Touches sensitive files")
            if not reasons:
                return None
            return _request(mode, name, _ACTION_SUMMARIES[name], detail, reasons)
        return _request(mode, name, _ACTION_SUMMARIES[name], detail,
                        ["Accesses the network", "Writes a file"])
    if name == "web_fetchs":
        detail = "\n".join(_list(args, "urls"))
        if not detail:
            return None
    else:
        query = _first(args, "query")
        if not query:
            return None
        detail = f"Search: {query}"
    if mode == "ask":
        return _request(mode, name, _ACTION_SUMMARIES[name], detail,
                        ["Accesses the network"])
    return None


# -- shell ------------------------------------------------------------------

_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;&\n\r]")
_TOKEN_RE = re.compile(r"[^\s]+")
_RAW_DEVICE_RE = re.compile(
    r"(?i)(>\s*/dev/(sd|hd|nvme|mmcblk|disk)|\\\\\.\\physicaldrive)")
_DOWNLOAD_EXEC_RE = re.compile(
    r"(?i)\b(curl|wget|iwr|Invoke-WebRequest|Invoke-RestMethod|irm)\b"
    r"[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh|dash|python[\d.]*|node|perl|ruby|"
    r"iex|Invoke-Expression)\b")

_SIMPLE_READ_ONLY = {
    "pwd", "echo", "whoami", "hostname", "date", "uname", "ver", "systeminfo",
    "ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "egrep",
    "fgrep", "rg", "sort", "which", "where", "whereis", "stat", "file",
    "diff", "cmp", "cut", "tr", "nl", "od", "hexdump", "strings", "jq", "du",
    "df", "free", "ps", "tasklist", "printenv", "tree", "find", "ipconfig",
    "ifconfig", "netstat", "arp", "getmac",
}

_INTERPRETER_HEADS = {
    "python", "node", "deno", "bun", "go", "rustc", "cargo", "java",
    "javac", "dotnet", "ruby", "php",
}

_VERSION_FLAGS = {"--version", "-v", "-version", "-V", "--help", "-h", "-?"}

_NETWORK_HEADS = {
    "curl", "wget", "ssh", "scp", "sftp", "ftp", "telnet", "nc", "ncat",
    "ping", "tracert", "traceroute", "nslookup", "dig", "host", "rsync",
}

_POWERSHELL_READ_ONLY = {
    "get-childitem", "gci", "get-content", "gc", "get-item", "gi",
    "get-location", "gl", "get-process", "gps", "get-service", "gsv",
    "get-date", "get-help", "help", "get-command", "gcm", "get-member", "gm",
    "get-alias", "gal", "get-variable", "gv", "get-psdrive", "get-module",
    "get-history", "get-host", "get-culture", "get-uiculture", "get-timezone",
    "get-executionpolicy", "get-filehash", "get-itemproperty", "gp",
    "get-acl", "get-eventlog", "get-winevent", "get-ciminstance",
    "get-wmiobject", "get-computerinfo", "get-volume", "get-disk",
    "get-partition", "get-netadapter", "get-netipconfiguration",
    "get-netipaddress", "get-dnsclientserveraddress", "get-smbshare",
    "get-hotfix", "get-localuser", "get-localgroup", "get-localgroupmember",
    "get-scheduledtask", "test-path", "resolve-path", "rvpa",
    "select-string", "sls", "select-object", "select", "where-object",
    "measure-object", "measure", "sort-object",
    "group-object", "group", "compare-object", "compare",
    "format-table", "ft", "format-list", "fl", "format-wide", "fw",
    "out-string", "out-host", "write-output", "write-host",
}


def _basename(token) -> str:
    name = str(token or "").strip().strip("\"'")
    name = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name.lower().removesuffix(".exe")


def _segments(command) -> list[str]:
    return [part.strip() for part in _SEGMENT_SPLIT_RE.split(str(command or ""))
            if part.strip()]


def _tokens(segment: str) -> list[str]:
    return [token.strip("\"'") for token in _TOKEN_RE.findall(segment)]


def _wrapper_inner(segment: str) -> str:
    tokens = _tokens(segment)
    if not tokens:
        return ""
    head = _basename(tokens[0])
    args = tokens[1:]
    lowered = [token.lower() for token in args]
    if head in {"powershell", "pwsh"}:
        for index, token in enumerate(lowered):
            if token in {"-command", "-c"}:
                return " ".join(args[index + 1:])
        return ""
    if head == "cmd":
        for index, token in enumerate(lowered):
            if token == "/c":
                return " ".join(args[index + 1:])
        return ""
    if head in {"bash", "sh", "zsh", "dash", "fish", "ksh"}:
        for index, token in enumerate(args):
            flag = token.lower()
            if flag == "-c" or (flag.startswith("-")
                                and not flag.startswith("--")
                                and flag.endswith("c")):
                return " ".join(args[index + 1:])
        return ""
    return ""


def _is_read_only_shell(command) -> bool:
    text = str(command or "").strip()
    if not text:
        return False
    if any(marker in text for marker in ("`", "$(", "${", ">", "<")):
        return False
    segments = _segments(text)
    if not segments:
        return False
    return all(_segment_is_read_only(segment) for segment in segments)


def _segment_is_read_only(segment: str) -> bool:
    tokens = _tokens(segment)
    if not tokens:
        return False
    head = _basename(tokens[0])
    args = tokens[1:]
    lowered = [token.lower() for token in args]

    if head in {"powershell", "pwsh"}:
        return _powershell_read_only(args)
    if head == "cmd":
        return _cmd_read_only(args)
    if head in {"bash", "sh", "zsh", "dash", "fish", "ksh"}:
        return _posix_shell_read_only(args)
    if head == "git":
        return _git_read_only(args)
    if head in _INTERPRETER_HEADS or re.fullmatch(r"python[\d.]*", head):
        return bool(args) and all(token in _VERSION_FLAGS for token in lowered)
    if head in {"pip", "pip3"}:
        return _pip_read_only(args)
    if head in {"npm", "pnpm", "yarn"}:
        return _node_package_read_only(args)
    if head == "uv":
        return _uv_read_only(args)
    if head == "docker":
        return _docker_read_only(args)
    if head == "kubectl":
        return _kubectl_read_only(args)
    if head == "gh":
        return _gh_read_only(args)
    if head in _POWERSHELL_READ_ONLY:
        return True
    if head in _SIMPLE_READ_ONLY:
        return _simple_read_only(head, lowered)
    return False


def _simple_read_only(head: str, args: list[str]) -> bool:
    if head == "find":
        return not any(token.startswith(("-exec", "-execdir", "-ok", "-okdir",
                                         "-delete", "-fprint", "-fls"))
                       for token in args)
    if head == "sort":
        return not any(token in {"-o", "--output"} or token.startswith("--output=")
                       for token in args)
    if head == "date":
        return not any(token in {"-s", "--set"} or token.startswith("--set=")
                       for token in args)
    if head == "hostname":
        return not any(not token.startswith("-") for token in args)
    return True


def _powershell_read_only(args: list[str]) -> bool:
    lowered = [token.lower() for token in args]
    if any(token in {"-e", "-ec", "-enc", "-encodedcommand", "/e", "/enc"}
           or token.startswith("-encodedcommand") for token in lowered):
        return False
    for index, token in enumerate(lowered):
        if token in {"-command", "-c"}:
            return _is_read_only_shell(" ".join(args[index + 1:]))
        if token in {"-file", "-f"}:
            return False
    return bool(lowered) and all(
        token in {"-noprofile", "-noninteractive", "-nologo", "-help", "-?", "--help"}
        for token in lowered)


def _cmd_read_only(args: list[str]) -> bool:
    lowered = [token.lower() for token in args]
    for index, token in enumerate(lowered):
        if token == "/c":
            return _is_read_only_shell(" ".join(args[index + 1:]))
        if token == "/k":
            return False
    return bool(lowered) and all(token in {"/?", "/help"} for token in lowered)


def _posix_shell_read_only(args: list[str]) -> bool:
    for index, token in enumerate(args):
        flag = token.lower()
        if flag == "-c" or (flag.startswith("-")
                            and not flag.startswith("--")
                            and flag.endswith("c")):
            return _is_read_only_shell(" ".join(args[index + 1:]))
    return bool(args) and all(token.lower() in {"--version", "--help"}
                              for token in args)


_GIT_GLOBAL_WITH_VALUE = {
    "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
    "--exec-path",
}
_GIT_GLOBAL_FLAGS = {
    "--no-pager", "--paginate", "-p", "--bare", "--no-replace-objects",
    "--literal-pathspecs", "--version", "--help", "-v",
}


def _git_args(args: list[str]) -> list[str]:
    tokens = list(args)
    index = 0
    while index < len(tokens):
        token = tokens[index].lower()
        if token in _GIT_GLOBAL_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(prefix + "=") for prefix in
               _GIT_GLOBAL_WITH_VALUE if prefix.startswith("--")):
            index += 1
            continue
        if token in _GIT_GLOBAL_FLAGS:
            index += 1
            continue
        break
    return tokens[index:]


def _git_read_only(args: list[str]) -> bool:
    rest = _git_args(args)
    if not rest:
        return True
    sub = rest[0].lower()
    tail = [token.lower() for token in rest[1:]]
    if sub in {"status", "log", "diff", "show", "rev-parse", "describe",
               "ls-files", "ls-tree", "cat-file", "rev-list", "shortlog",
               "blame", "grep", "reflog", "count-objects", "check-ignore",
               "check-attr", "merge-base", "name-rev", "for-each-ref",
               "show-ref", "help", "version"}:
        return True
    if sub in {"--version", "-v"}:
        return True
    if sub == "branch":
        if not tail:
            return True
        if any(token in {"-d", "-m", "-c", "-f", "--delete", "--move", "--copy",
                         "--force", "-u", "--set-upstream-to", "--unset-upstream",
                         "--edit-description"} for token in tail):
            return False
        if any(token in {"-l", "--list"} for token in tail):
            return True
        return all(token.startswith("-") for token in tail)
    if sub == "tag":
        if not tail:
            return True
        if any(token in {"-d", "--delete", "-a", "--annotate", "-s", "--sign",
                         "-f", "--force", "-m", "--message", "-u", "--local-user"}
               for token in tail):
            return False
        if any(token in {"-l", "--list"} or re.fullmatch(r"-n\d*", token)
               for token in tail):
            return True
        return all(token.startswith("-") for token in tail)
    if sub == "config":
        return any(token in {"--get", "--get-all", "--get-regexp", "--list", "-l"}
                   for token in tail)
    if sub == "remote":
        if not tail or all(token in {"-v", "--verbose"} for token in tail):
            return True
        return tail[0] in {"show", "get-url"}
    if sub == "stash":
        return bool(tail) and tail[0] in {"list", "show"}
    if sub == "worktree":
        return bool(tail) and tail[0] == "list"
    if sub == "submodule":
        return not tail or tail[0] in {"status", "summary"}
    if sub == "symbolic-ref":
        return len([token for token in tail if not token.startswith("-")]) == 1
    return False


def _pip_read_only(args: list[str]) -> bool:
    if not args:
        return False
    sub = args[0].lower()
    if sub in {"list", "show", "freeze", "check", "--version", "-v", "-V",
               "--help", "-h"}:
        return True
    if sub == "config":
        return len(args) == 1 or (len(args) > 1
                                  and args[1].lower() in {"get", "list", "debug"})
    return False


def _node_package_read_only(args: list[str]) -> bool:
    if not args:
        return False
    sub = args[0].lower()
    if sub in {"ls", "list", "view", "info", "outdated", "why", "root", "bin",
               "prefix", "--version", "-v", "--help"}:
        return True
    if sub == "config":
        return len(args) == 1 or (len(args) > 1
                                  and args[1].lower() in {"get", "list", "ls"})
    if sub == "pkg":
        return len(args) > 1 and args[1].lower() in {"get", "list"}
    return False


def _uv_read_only(args: list[str]) -> bool:
    if not args:
        return False
    sub = args[0].lower()
    if sub in {"--version", "-v", "-V", "--help", "tree"}:
        return True
    if sub == "pip":
        return len(args) > 1 and args[1].lower() in {"list", "show", "freeze",
                                                     "check"}
    if sub == "python":
        return len(args) > 1 and args[1].lower() in {"list", "find"}
    if sub == "cache":
        return len(args) > 1 and args[1].lower() == "dir"
    return False


def _docker_read_only(args: list[str]) -> bool:
    if not args:
        return False
    sub = args[0].lower()
    if sub in {"ps", "images", "logs", "inspect", "version", "info",
               "--version", "-v"}:
        return True
    if sub == "compose":
        return len(args) > 1 and args[1].lower() in {"ps", "logs", "config"}
    return False


def _kubectl_read_only(args: list[str]) -> bool:
    if not args:
        return False
    sub = args[0].lower()
    if sub in {"get", "describe", "logs", "version", "explain",
               "api-resources", "api-versions"}:
        return True
    if sub == "config":
        return len(args) > 1 and args[1].lower() in {"view", "current-context",
                                                     "get-contexts"}
    return False


def _gh_read_only(args: list[str]) -> bool:
    if not args:
        return False
    sub = args[0].lower()
    if sub in {"status", "--version"}:
        return True
    if len(args) < 2:
        return False
    action = args[1].lower()
    if sub in {"pr", "run", "workflow", "release", "gist"}:
        return action in {"list", "view", "diff", "checks", "status"}
    if sub == "issue":
        return action in {"list", "view"}
    if sub == "repo":
        return action == "view"
    if sub in {"search", "auth"}:
        return True
    return False


def _sensitive_command(command) -> bool:
    text = str(command or "")
    for token in re.findall(r"[^\s\"'|;&<>()]+", text):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            value = token.split("=", 1)[1]
            if value and _is_sensitive(value):
                return True
        if not token.startswith("-") and _is_sensitive(token):
            return True
    return False


def _has_network_command(command) -> bool:
    for segment in _segments(command):
        tokens = _tokens(segment)
        if not tokens:
            continue
        head = _basename(tokens[0])
        args = [token.lower() for token in tokens[1:]]
        inner = _wrapper_inner(segment)
        if inner and _has_network_command(inner):
            return True
        if head in _NETWORK_HEADS or head == "gh":
            return True
        if head == "git" and args and args[0] in {"clone", "fetch", "pull",
                                                  "push"}:
            return True
        if head in {"npm", "pnpm", "yarn"} and args and args[0] in {
                "install", "i", "ci", "add", "update", "upgrade"}:
            return True
        if head in {"pip", "pip3"} and args and args[0] in {"install", "download"}:
            return True
        if head == "uv" and args and (
                args[0] in {"add", "sync"}
                or (args[0] == "pip" and len(args) > 1
                    and args[1] == "install")):
            return True
        if head == "docker" and args and args[0] in {"pull", "push", "login",
                                                     "build"}:
            return True
        if head in {"winget", "choco", "scoop", "apt", "apt-get", "dnf", "yum",
                    "pacman", "brew"} and args and args[0] in {
                "install", "update", "upgrade", "search", "remove",
                "uninstall", "purge"}:
            return True
        if head in {"pipenv", "poetry"} and args and args[0] in {
                "install", "add", "update"}:
            return True
    return False


def _git_risks(args: list[str]) -> list[str]:
    rest = _git_args(args)
    if not rest:
        return []
    sub = rest[0].lower()
    tail = [token.lower() for token in rest[1:]]
    if sub == "rm":
        return ["Deletes files"]
    if sub == "reset" and "--hard" in tail:
        return ["Discards uncommitted changes"]
    if sub == "clean" and any(
            token == "--force"
            or (token.startswith("-") and not token.startswith("--")
                and "f" in token[1:])
            for token in tail):
        return ["Deletes untracked files"]
    if sub == "push" and any(
            token in {"--force", "-f"} or token.startswith("--force-with-lease")
            for token in tail):
        return ["Force-pushes a branch"]
    if sub == "branch" and any(token in {"-d", "--delete"} for token in tail):
        return ["Deletes a branch"]
    if sub == "stash" and tail and tail[0] in {"drop", "clear"}:
        return ["Drops stashed changes"]
    if sub in {"filter-branch", "filter-repo"}:
        return ["Rewrites Git history"]
    paths = [token for token in tail if not token.startswith("-")]
    if sub in {"checkout", "restore"} and paths == ["."] and "--staged" not in tail:
        return ["Discards uncommitted changes"]
    return []


def _segment_risks(segment: str) -> list[str]:
    tokens = _tokens(segment)
    if not tokens:
        return []
    head = _basename(tokens[0])
    args = tokens[1:]
    lowered = [token.lower() for token in args]

    if head in {"powershell", "pwsh"} and any(
            token in {"-e", "-ec", "-enc", "-encodedcommand"}
            or token.startswith("-encodedcommand") for token in lowered):
        return ["Executes an encoded command"]
    inner = _wrapper_inner(segment)
    if inner:
        return list(_shell_risks(inner))

    if head in {"rm", "rmdir", "remove-item", "ri", "shred", "sdelete"}:
        forced = any(
            token in {"-f", "--force", "-r", "--recursive"}
            or (token.startswith("-") and not token.startswith("--")
                and ("r" in token[1:] or "f" in token[1:]))
            for token in lowered)
        return ["Recursively or forcibly deletes files" if forced
                else "Deletes files"]
    if head in {"del", "erase", "rd"}:
        forced = any(token.startswith("/")
                     and any(flag in token[1:] for flag in "sfq")
                     for token in lowered)
        return ["Recursively or forcibly deletes files" if forced
                else "Deletes files"]
    if head in {"format", "diskpart", "fdisk", "sfdisk", "parted", "gdisk"} \
            or head.startswith("mkfs"):
        return ["Formats or partitions a disk"]
    if head == "dd" and any(token.startswith("if=") for token in lowered):
        return ["Writes raw data to a device"]
    if head in {"shutdown", "reboot", "poweroff", "halt", "restart-computer",
                "stop-computer"}:
        return ["Shuts down or restarts the machine"]
    if head in {"taskkill", "killall", "pkill", "stop-process"}:
        return ["Force-kills processes"]
    if head == "kill" and any(token in {"-9", "-kill"} for token in lowered):
        return ["Force-kills processes"]
    if head in {"sudo", "doas", "runas"} or (
            head == "start-process" and "runas" in lowered):
        return ["Requests administrator privileges"]
    if head == "reg" and lowered and lowered[0] in {"add", "delete", "import",
                                                    "copy", "restore"}:
        return ["Modifies the Windows registry"]
    if head == "regedit":
        return ["Modifies the Windows registry"]
    if head == "sc" and lowered and lowered[0] in {"delete", "stop", "config",
                                                   "create", "failure"}:
        return ["Changes services or user accounts"]
    if head == "net" and lowered and lowered[0] in {"stop", "user",
                                                    "localgroup"}:
        return ["Changes services or user accounts"]
    if head == "systemctl" and lowered and lowered[0] in {
            "stop", "disable", "mask", "reboot", "poweroff", "halt", "kill",
            "isolate", "rescue", "emergency"}:
        return ["Changes services or user accounts"]
    if head == "wmic" and "delete" in lowered:
        return ["Changes system configuration"]
    if head in {"bcdedit", "bootrec", "vssadmin", "wbadmin"}:
        return ["Changes system or boot configuration"]
    if head == "cipher" and "/w" in lowered:
        return ["Securely erases data"]
    if head == "chmod" and any(token in {"777", "0777"} for token in lowered):
        return ["Makes files world-writable"]
    if head == "chown" and any(token in {"-r", "--recursive"} for token in lowered):
        return ["Changes ownership recursively"]
    if head in {"eval", "iex", "invoke-expression"}:
        return ["Executes an evaluated command"]
    if head in {"apt", "apt-get", "dnf", "yum", "pacman", "brew", "choco",
                "winget", "scoop"} and lowered and lowered[0] in {
            "install", "remove", "uninstall", "purge", "upgrade"}:
        return ["Installs or removes system software"]
    if head == "git":
        return _git_risks(args)
    return []


def _shell_risks(command) -> list[str]:
    text = str(command or "")
    risks: list[str] = []
    for segment in _segments(text):
        for reason in _segment_risks(segment):
            if reason not in risks:
                risks.append(reason)
    if _DOWNLOAD_EXEC_RE.search(text) and \
            "Executes code downloaded from the network" not in risks:
        risks.append("Executes code downloaded from the network")
    if _RAW_DEVICE_RE.search(text) and "Writes raw data to a device" not in risks:
        risks.append("Writes raw data to a device")
    return risks


def _shell_request(args: dict, project_root: str, mode: str):
    command = _text(args.get("command"))
    if not command:
        return None
    cwd_value = _first(args, "cwd")
    resolved_root = _resolve(project_root, project_root)
    cwd = _resolve(cwd_value, project_root) if cwd_value else resolved_root
    outside_cwd = bool(cwd_value and resolved_root and cwd
                       and not _within(cwd, resolved_root))
    read_only = _is_read_only_shell(command)
    sensitive = _sensitive_command(command)
    risks = _shell_risks(command)
    network = _has_network_command(command)

    if mode == "ask" and read_only and not sensitive and not outside_cwd:
        return None

    reasons: list[str] = []
    if not read_only:
        reasons.append("Runs a shell command")
    reasons.extend(risks)
    if mode == "ask" and network and \
            "Executes code downloaded from the network" not in reasons:
        reasons.append("Accesses the network")
    if outside_cwd:
        reasons.append(f"Runs outside the project ({cwd_value})")
    if sensitive:
        reasons.append("Touches sensitive files")
    if mode == "auto":
        reasons = [reason for reason in reasons if reason != "Runs a shell command"]
        if not reasons:
            return None
    if not reasons:
        reasons.append("Runs a shell command")
    return _request(mode, "execute_shell", _ACTION_SUMMARIES["execute_shell"],
                    command, reasons)


def _request(mode: str, action: str, summary: str, detail: str,
             reasons: list[str]) -> dict:
    return {
        "mode": mode,
        "action": action,
        "summary": summary,
        "detail": detail,
        "reasons": reasons,
    }
