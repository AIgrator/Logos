# PyInstaller spec for Logos AI Assistant (one-dir build).
#
# Notes:
# - One-dir (not one-file): faster cold start, AV-friendlier.
# - Splash: assets/ico.png placeholder until the animated GIF lands.
# - Browsers (Playwright Chromium, ~170MB) are NOT bundled: on first
#   Browser-mode use the app reports how to install them
#   (`playwright install chromium` or bundled ms-playwright dir next
#   to the EXE via PLAYWRIGHT_BROWSERS_PATH). Keeps the base build lean.
# - keyring uses the ctypes-based Windows backend (no pywin32 needed).
# - Heavy ML stack (torch/OCR/YOLO) was removed from dependencies, which
#   is what makes this build viable at all (was ~9GB unstartable).

import os

block_cipher = None

datas = [
    ("assets", "assets"),
    ("prompts", "prompts"),
    ("cache/models.json", "cache"),
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "keyring.backends.Windows",
        "plyer" if False else "plyer.platforms.win.notification",
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

# Drop the conditional hack above (kept explicit for clarity).
a.hiddenimports = [h for h in a.hiddenimports if isinstance(h, str)]

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
    [],
    exclude_binaries=True,
    name="Logos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/ico.ico",
    splash=splash,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    splash.binaries,
    strip=False,
    upx=False,
    name="Logos",
)
