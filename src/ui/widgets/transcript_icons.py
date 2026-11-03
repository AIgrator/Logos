# src/ui/widgets/transcript_icons.py
"""Small widgets drawn inside the transcript: the user-message copy button.

Qt's rich text can only carry images, and Windows fonts have no good glyph for
"copy" (the symbol fonts that do ship one render at the wrong weight), so the
icon is painted like the status badges in ``status_marks`` and registered as a
document resource (``logos-icon:copy``). That keeps it crisp at any device
pixel ratio and tinted with the active theme.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QUrl, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QTextDocument

ICON_PREFIX = "logos-icon:"
ICON_SIZE = 12

# Which theme style key tints each icon.
_ICON_COLORS = {
    "copy": "COLLAPSIBLE_META",
    "copied": "STATUS_OK_COLOR",
}

_CACHE: dict[tuple[str, str, float], QImage] = {}


def _draw_icon(painter: QPainter, name: str, color: str, ratio: float) -> None:
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(ratio, ratio)
    pen = QPen(QColor(color))
    pen.setWidthF(1.1)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if name == "copied":
        painter.drawPolyline([QPointF(2.4, 6.4), QPointF(4.9, 8.9),
                              QPointF(9.6, 3.3)])
    else:
        # Front sheet plus the two edges of the sheet behind it, the way a
        # copy glyph reads at 12 px: overlapping full rectangles blur into a
        # ring at this size.
        painter.drawRoundedRect(QRectF(2.4, 3.6, 7.0, 8.0), 1.3, 1.3)
        painter.drawPolyline([QPointF(4.6, 1.6), QPointF(9.4, 1.6),
                              QPointF(9.4, 6.4)])
    painter.end()


def icon_image(name: str, color: str, ratio: float = 1.0) -> QImage:
    """Icon image for one name, cached per color and device ratio."""
    if name not in _ICON_COLORS:
        name = "copy"
    ratio = max(1.0, float(ratio or 1.0))
    key = (name, color, round(ratio, 2))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    pixels = max(8, int(round(ICON_SIZE * ratio)))
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    _draw_icon(painter, name, color, ratio)
    image.setDevicePixelRatio(ratio)
    _CACHE[key] = image
    return image


def register_icon_images(doc, theme_manager) -> None:
    """Binds the transcript icons to a document, tinted for the current theme."""
    screen = QGuiApplication.primaryScreen()
    ratio = float(screen.devicePixelRatio()) if screen is not None else 1.0
    for name, style_key in _ICON_COLORS.items():
        try:
            color = theme_manager.get_style(style_key)
        except Exception:
            color = "#8b949e"
        doc.addResource(QTextDocument.ResourceType.ImageResource,
                        QUrl(f"{ICON_PREFIX}{name}"),
                        icon_image(name, color, ratio))
