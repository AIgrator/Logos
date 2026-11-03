# src/ui/widgets/status_marks.py
"""Circled status badges for the transcript's activity rows.

The marks are drawn, not typed. A font's check glyph is heavy, sits on the
baseline like a read receipt and comes in one color per theme, while a thin
outlined circle reads as a state badge: the shape carries the state and the
color only supports it. Drawing also keeps the badge crisp at any device pixel
ratio and lets its color follow the active theme, which a bundled image could
not do without a file per state and theme.

The transcript is one ``QTextDocument`` per block, so the images are registered
as document resources (``<img src="logos-status:ok">``) instead of files: no
asset paths to break in a frozen build and no SVG plugin to depend on.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QUrl, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QPainter,
    QPen,
    QTextDocument,
)

STATUS_PREFIX = "logos-status:"
# Logical size in device-independent pixels: the transcript body text is 13 px
# and the badge should sit in the line without enlarging it.
MARK_SIZE = 13

# Theme style key that tints each state. A transcript is mostly successful
# steps, so "ok" is calmer than the alert green used for real warnings.
_STATE_COLORS = {
    "ok": "STATUS_OK_COLOR",
    "error": "STATUS_ERROR_COLOR",
    "running": "COLLAPSIBLE_TITLE",
    "denied": "WARNING_COLOR",
    "stopped": "COLLAPSIBLE_META",
}

STATUS_STATES = tuple(_STATE_COLORS)

_cache: dict[tuple[str, str, float], QImage] = {}


def _draw_mark(image: QImage, status: str, color: str, ratio: float) -> None:
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(ratio, ratio)
    pen = QPen(QColor(color))
    pen.setWidthF(1.3)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # 13 px box, circle centred with a hair of padding for the stroke.
    painter.drawEllipse(QRectF(1.2, 1.2, 10.6, 10.6))
    if status == "ok":
        pen.setWidthF(1.6)
        painter.setPen(pen)
        painter.drawPolyline([QPointF(4.35, 6.7), QPointF(6.05, 8.4),
                              QPointF(8.75, 5.5)])
    elif status == "error":
        pen.setWidthF(1.45)
        painter.setPen(pen)
        painter.drawLine(QPointF(4.7, 4.7), QPointF(8.3, 8.3))
        painter.drawLine(QPointF(8.3, 4.7), QPointF(4.7, 8.3))
    elif status == "denied":
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.drawLine(QPointF(6.5, 4.05), QPointF(6.5, 7.05))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(5.75, 8.5, 1.5, 1.5))
    elif status == "stopped":
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.drawLine(QPointF(4.4, 6.5), QPointF(8.6, 6.5))
    # "running" is the bare ring: the row is still open, nothing happened yet.
    painter.end()


def mark_image(status: str, color: str, ratio: float = 1.0) -> QImage:
    """Badge image for one state, cached per color and device ratio."""
    if status not in _STATE_COLORS:
        status = "running"
    ratio = max(1.0, float(ratio or 1.0))
    key = (status, color, round(ratio, 2))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    pixels = max(8, int(round(MARK_SIZE * ratio)))
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    _draw_mark(image, status, color, ratio)
    image.setDevicePixelRatio(ratio)
    _cache[key] = image
    return image


def _device_ratio() -> float:
    screen = QGuiApplication.primaryScreen()
    return float(screen.devicePixelRatio()) if screen is not None else 1.0


def register_status_images(doc, theme_manager) -> None:
    """Binds the badges to a document, tinted for the current theme.

    ``ChatWidget._new_document`` calls this for every transcript document, so
    a render only has to emit ``<img src="logos-status:<state>">``.
    """
    ratio = _device_ratio()
    for status, style_key in _STATE_COLORS.items():
        try:
            color = theme_manager.get_style(style_key)
        except Exception:
            color = "#8b949e"
        image = mark_image(status, color, ratio)
        doc.addResource(QTextDocument.ResourceType.ImageResource,
                        _url(status), image)


def _url(status: str) -> QUrl:
    return QUrl(f"{STATUS_PREFIX}{status}")
