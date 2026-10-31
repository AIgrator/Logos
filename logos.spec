# PyInstaller spec for Logos AI Assistant (one-file build).
#
# Notes:
# - One-file: a single dist/Logos.exe. It unpacks to a temp folder per launch
#   (sys._MEIPASS). On first launch main.py copies prompts/ and assets/ next
#   to the EXE (refreshed when the EXE changes), and settings.json, logs,
#   cache and screenshots live there too (see SettingsManager and
#   ModelConfigManager frozen branches), so the temp folder can vanish.
# - Splash: assets/ico.png placeholder until the animated GIF lands. Only
#   one-file mode supports the bootloader splash.
# - Browsers (Playwright Chromium, ~170MB) are NOT bundled: on first
#   Browser-mode use the app reports how to install them
#   (`playwright install chromium` or bundled ms-playwright dir next
#   to the EXE via PLAYWRIGHT_BROWSERS_PATH). Keeps the base build lean.
# - keyring uses the ctypes-based Windows backend (no pywin32 needed).
# - Heavy ML stack (torch/OCR/YOLO) was removed from dependencies, which
#   is what makes this build viable at all (was ~9GB unstartable).

import os

block_cipher = None

SRC = os.path.join(SPECPATH, "src")
PROVIDERS = os.path.join(SRC, "llm", "providers")


def _py_modules(directory, prefix):
    """Importable names under ``directory`` (providers and modes load by name)."""
    modules = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            parts = os.path.relpath(os.path.join(root, name), directory)[:-3].split(os.sep)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                modules.append(".".join([prefix, *parts]))
    return modules


# llm/__init__.py discovers providers with pkgutil over its own package folder
# and parses their source text for display names, so each package identity
# (llm and src.llm) needs both the hidden imports and the .py files on disk.
provider_sources = [
    (os.path.join(PROVIDERS, name), target)
    for target in ("llm/providers", "src/llm/providers")
    for name in sorted(os.listdir(PROVIDERS))
    if name.endswith(".py")
]

datas = [
    (os.path.join(SPECPATH, "assets"), "assets"),
    (os.path.join(SPECPATH, "prompts"), "prompts"),
    (os.path.join(SPECPATH, "skills"), "skills"),
    (os.path.join(SPECPATH, "cache", "models.json"), "cache"),
    (os.path.join(SPECPATH, "LICENSE"), "."),
    *provider_sources,
]

dynamic_imports = (
    _py_modules(PROVIDERS, "llm.providers")
    + _py_modules(PROVIDERS, "src.llm.providers")
    + [f"src.computer.modes.{mode}" for mode in ("chat", "terminal", "browser", "vision")]
)

a = Analysis(
    ["main.py"],
    # main.py appends <root>/src to sys.path at runtime, so modulegraph needs
    # both roots to see the app's top-level modules (llm, controller, ...) and
    # the src.* package imports.
    pathex=[SRC, SPECPATH],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "keyring.backends.Windows",
        "xai_sdk",
        *dynamic_imports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "torchvision",
        "transformers",
        "ultralytics",
        "easyocr",
        "cv2",
        "matplotlib",
        "pandas",
        "scipy",
        "sklearn",
        "tkinter",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

splash = Splash(
    "assets/ico.png",
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    text_size=12,
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    splash.binaries,
    [],
    name="Logos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/ico.ico",
    splash=splash,
)
