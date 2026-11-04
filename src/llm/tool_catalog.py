"""Where the provider layer finds tool schemas.

Two sets of files describe a tool-call envelope:

* ``cache/<mode>/response_formats.json`` - written by ``ToolRegistry`` from each
  mode's manifest, one file per mode (terminal, vision, browser).
* ``cache/session/<handler>/response_formats.json`` - the same envelope rebuilt
  from the **open session's grants** (see
  ``ToolRegistry.generate_session_profile``). A session can hold the terminal
  and the browser capability at once, so its envelope is wider than the mode's.

The session copies are merged **last**, so they win for the envelope names they
define. Vision has no session copy: it answers with provider-native tool calls.

Providers cache the formatted tool list, so :func:`catalogue_stamp` lets them
notice that a session attached or dropped a capability and rebuild instead of
sending yesterday's contract.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

#: Per-mode files, in load order.
MODE_FILES = (
    'cache/terminal/response_formats.json',
    'cache/vision/response_formats.json',
    'cache/browser/response_formats.json',
)

#: Session files, merged after the mode files (they override the same names).
SESSION_FILES = (
    'cache/session/terminal/response_formats.json',
    'cache/session/browser/response_formats.json',
)

SCHEMA_FILES = MODE_FILES + SESSION_FILES


def schema_files() -> tuple[str, ...]:
    return SCHEMA_FILES


def load_schemas() -> dict:
    """Every response format, session-granted definitions taking precedence."""
    schemas: dict = {}
    for path in SCHEMA_FILES:
        try:
            with open(path, encoding='utf-8') as handle:
                loaded = json.load(handle)
        except FileNotFoundError:
            logger.warning("Could not find schema file: '%s'. Skipping.", path)
            continue
        except (OSError, json.JSONDecodeError) as error:
            logger.error("Failed to load or parse '%s': %s", path, error)
            continue
        if isinstance(loaded, dict):
            schemas.update(loaded)
    return schemas


def catalogue_stamp() -> tuple:
    """Cheap fingerprint of the schema files (mtime + size per file)."""
    stamp = []
    for path in SCHEMA_FILES:
        try:
            info = os.stat(path)
            stamp.append((path, info.st_mtime_ns, info.st_size))
        except OSError:
            stamp.append((path, 0, 0))
    return tuple(stamp)


def strict_schema(schema_name: str, flat: bool = False):
    """The constrained-decoding schema for one envelope, or None.

    The session files are searched first: they carry the granted tool set, while
    the mode file for the same name is the wider-or-narrower default.
    """
    if not schema_name:
        return None
    key = "strict_schema_flat" if flat else "strict_schema"
    for path in SESSION_FILES + MODE_FILES:
        try:
            with open(path, encoding='utf-8') as handle:
                schemas = json.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as error:
            logger.error("Failed to load '%s' for a strict schema: %s", path, error)
            continue
        info = schemas.get(schema_name) if isinstance(schemas, dict) else None
        if isinstance(info, dict) and info.get(key):
            return info[key]
    return None
