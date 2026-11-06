"""Small clickable thumbnails shared by attachments and tool steps.

A picture is shown the same way wherever it appears in the transcript: a
rounded tile capped at 200 px whose bytes are embedded in the document. That
embedding matters - a screenshot file is pruned by rotation and an attachment
can be moved, while a chat message keeps its picture.

The tile is a ``data:`` URI, not a file URL, so a re-render (a resize, a clock
tick while the turn runs) never goes back to disk. Encoding is cached by path
and modification time, because a step block re-renders on every second of a
running job and PIL must not run each time. A file that is gone (a screenshot
pruned by rotation, a deleted attachment) yields nothing instead of a broken
image.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageOps

THUMB_MAX = 200
THUMB_RADIUS = 10


@lru_cache(maxsize=48)
def _encode(path: str, mtime_ns: int):
    """(data URI, width, height) of one file, or None when it cannot be read."""
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((THUMB_MAX, THUMB_MAX), Image.Resampling.LANCZOS)
            image = image.convert("RGBA")
            width, height = image.size
            radius = min(THUMB_RADIUS, width // 4, height // 4)
            if radius > 0:
                scale = 4
                mask = Image.new("L", (width * scale, height * scale), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, width * scale - 1, height * scale - 1),
                    radius=radius * scale, fill=255)
                mask = mask.resize(
                    (width, height), Image.Resampling.LANCZOS)
                alpha = ImageChops.multiply(image.getchannel("A"), mask)
                image.putalpha(alpha)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
    except Exception as e:
        logging.warning(f"Could not build a thumbnail for {path}: {e}")
        return None
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}", width, height


def thumbnail(path: str):
    """(data URI, width, height) for an image on disk, or None.

    The cache is keyed by ``(path, mtime)``, so a file that was overwritten is
    re-encoded while an unchanged one is not read again.
    """
    path = str(path or "").strip()
    if not path:
        return None
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    return _encode(os.path.abspath(path), stamp)
