"""Bundled provider brand glyphs, drawn wherever a provider is named.

The glyphs come from ``assets/providers/`` (``@lobehub/icons-static-svg``,
MIT, see the LICENSE file there) and are rendered through QtSvg with their
``currentColor`` swapped for the caller's tint, so one file serves the dark and
the light theme. Shared by the Providers settings page (tiles) and the
composer's model button, which shows who answers instead of a generic bolt.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Brand glyphs from @lobehub/icons-static-svg (MIT), see assets/providers/LICENSE.
PROVIDER_ICONS = {
    "anthropic": "anthropic.svg",
    "openai": "openai.svg",
    "google": "gemini.svg",
    "openrouter": "openrouter.svg",
    "huggingface": "huggingface.svg",
    "x-ai": "xai.svg",
}


def icon_dir() -> Path:
    """Folder holding the bundled glyphs, in a checkout or a frozen build."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "assets" / "providers"
    return Path(__file__).resolve().parents[2] / "assets" / "providers"


def provider_glyph(provider_id: str, color: str, size: int = 21) -> QPixmap | None:
    """Renders the provider's brand glyph in ``color``; None when unknown."""
    file_name = PROVIDER_ICONS.get(provider_id)
    if not file_name:
        return None
    try:
        source = (icon_dir() / file_name).read_text(encoding="utf-8")
    except OSError:
        return None
    source = source.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(source.encode("utf-8")))
    if not renderer.isValid():
        return None
    ratio = 2.0
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def provider_icon(provider_id: str, color: str, size: int = 16) -> QIcon | None:
    """The same glyph as a ``QIcon``, for buttons; None when unknown."""
    pixmap = provider_glyph(provider_id, color, size)
    return QIcon(pixmap) if pixmap is not None else None
