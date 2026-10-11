"""Portable special-folder resolution (per-OS, no hardcoded profile paths).

The model writes portable tokens ({DESKTOP}, {HOME}, {WORKDIR},
{DOCUMENTS}, {DOWNLOADS}); the executor expands them on the real machine.
Each OS resolves them its own way:

- Windows: shell known folders via SHGetKnownFolderPath (OneDrive-aware;
  never guess '<home>\\Desktop').
- Linux: XDG user dirs (~/.config/user-dirs.dirs), fallback to ~/... .
- macOS: ~/Desktop, ~/Documents, ~/Downloads (standard locations).

When adding a new platform, add a resolver here — never in prompts.
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)

PATH_TOKENS = ("{DESKTOP}", "{HOME}", "{WORKDIR}", "{DOCUMENTS}", "{DOWNLOADS}")

# --- Windows KNOWNFOLDERIDs (copied verbatim from MS Learn) ---------------
_FID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_FID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"
_FID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"
_FID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def _user_home() -> str:
    try:
        home = os.path.expanduser("~")
        return home if home and home != "~" else ""
    except Exception:
        return ""


def _existing(path: str) -> str:
    try:
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return ""


def _windows_known_folder(folder_id: str) -> str:
    """Absolute path of a Windows known folder. WINDOWS ONLY."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD),
                        ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        def _guid(s: str) -> GUID:
            parts = s.strip("{}").split("-")
            if len(parts) != 5 or [len(p) for p in parts] != [8, 4, 4, 4, 12]:
                raise ValueError(f"Malformed GUID: {s}")
            d1, d2, d3 = int(parts[0], 16), int(parts[1], 16), int(parts[2], 16)
            d4 = bytes.fromhex(parts[3] + parts[4])
            return GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))

        buf = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            _guid(folder_id), 0, None, ctypes.byref(buf))
        if hr != 0 or not buf.value:
            return ""
        path = buf.value
        ctypes.windll.ole32.CoTaskMemFree(buf)
        return path
    except Exception as e:
        logger.warning(f"Known-folder lookup failed for {folder_id}: {e}")
        return ""


def _xdg_user_dir(key: str, fallback_subdir: str) -> str:
    """Resolve an XDG user dir on Linux (e.g. XDG_DESKTOP_DIR).

    Follows the user-dirs.dirs format: lines `XDG_*_DIR="VALUE"` where VALUE
    is `$HOME/...` or absolute, backslash-escaped, `#` starts a comment.
    """
    import re
    home = _user_home()
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")) if home else ""
    config_file = os.path.join(config_home, "user-dirs.dirs") if config_home else ""
    try:
        if config_file and os.path.isfile(config_file):
            with open(config_file, encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    name, _, value = line.partition("=")
                    name, value = name.strip(), value.strip()
                    if not (name.startswith("XDG_") and name.endswith("_DIR") and name == key):
                        continue
                    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                        value = value[1:-1]
                    else:
                        continue
                    # Unescape backslash sequences per the reference parser.
                    value = re.sub(r"\\(.)", r"\1", value)
                    if value.startswith("$HOME/"):
                        value = os.path.join(home, value[6:])
                    elif not value.startswith("/"):
                        continue
                    if os.path.isdir(value):
                        return value
    except Exception:
        pass
    return _existing(os.path.join(home, fallback_subdir)) if home else ""


def resolve_special_folders() -> dict:
    """Map portable tokens to real absolute paths on this machine."""
    home = _user_home()
    if sys.platform == "win32":
        desktop = (_windows_known_folder(_FID_DESKTOP)
                   or _existing(os.path.join(home, "Desktop"))
                   or r"C:\Users\Public\Desktop")
        documents = (_windows_known_folder(_FID_DOCUMENTS)
                     or _existing(os.path.join(home, "Documents")))
        downloads = (_windows_known_folder(_FID_DOWNLOADS)
                     or _existing(os.path.join(home, "Downloads")))
    elif sys.platform == "darwin":
        # macOS standard locations (no OneDrive-style redirection).
        desktop = _existing(os.path.join(home, "Desktop"))
        documents = _existing(os.path.join(home, "Documents"))
        downloads = _existing(os.path.join(home, "Downloads"))
    else:
        # Linux and others: XDG user dirs with ~/ fallbacks.
        desktop = _xdg_user_dir("XDG_DESKTOP_DIR", "Desktop")
        documents = _xdg_user_dir("XDG_DOCUMENTS_DIR", "Documents")
        downloads = _xdg_user_dir("XDG_DOWNLOAD_DIR", "Downloads")
    return {
        "{DESKTOP}": desktop,
        "{HOME}": home,
        "{DOCUMENTS}": documents,
        "{DOWNLOADS}": downloads,
    }


def expand_path_tokens(command: str, working_directory: str | None) -> str:
    """Replace portable location tokens with real absolute paths."""
    if not command or "{" not in command:
        return command
    mapping = resolve_special_folders()
    mapping["{WORKDIR}"] = working_directory or ""
    for token, value in mapping.items():
        if token in command:
            command = command.replace(token, value)
    return command
