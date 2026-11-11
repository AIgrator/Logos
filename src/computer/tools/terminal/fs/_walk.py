"""Shared walk rules for the read-only filesystem tools.

``find_files`` and ``search_file_content`` crawl a project tree, so both skip
the folders that never hold project source. A live ``/init`` run asked for
``**/*.toml``, got a venv's ``site-packages`` back, and the history budget then
pruned that result together with the file reads the model still needed - which
is why it re-read the same files for the rest of the turn.
"""
from __future__ import annotations

import fnmatch
import os

#: Directories a recursive tool never descends into.
SKIP_DIRS = {
    '__pycache__', '.git', '.hg', '.svn', '.venv', 'venv', 'env',
    'node_modules', '.idea', '.vscode', '.mypy_cache', '.pytest_cache',
    '.ruff_cache', 'build', 'dist', '.tox', '.eggs', 'site-packages',
}

#: A single call never returns more entries than this. The transcript is one
#: document, and a four-figure list buries the turn that asked for it.
MAX_RESULTS = 500


def walk_entries(root: str, include_dirs: bool = False):
    """Yields ``(absolute_path, parts)`` for every entry below *root*.

    ``parts`` are the components relative to *root*, so a caller matches them
    against a glob pattern without touching the disk again. Directories named
    in :data:`SKIP_DIRS` are neither descended into nor reported.
    """
    for current, dirs, files in os.walk(root):
        kept = sorted(d for d in dirs if d.lower() not in SKIP_DIRS)
        dirs[:] = kept
        rel = os.path.relpath(current, root)
        prefix = [] if rel == os.curdir else rel.split(os.sep)
        if include_dirs:
            for name in kept:
                yield os.path.join(current, name), prefix + [name]
        for name in sorted(files):
            yield os.path.join(current, name), prefix + [name]


def matches(parts: list[str], pattern: str) -> bool:
    """Matches path *parts* against a glob *pattern*.

    ``**`` spans any number of folders, a single-segment pattern such as
    ``*.py`` matches that name at any depth (``find -name`` behaviour), and
    every other pattern is matched folder by folder.
    """
    segments = [segment for segment in pattern.replace('\\', '/').split('/')
                if segment not in ('', '.')]
    if not segments or not parts:
        return False
    if len(segments) == 1:
        return fnmatch.fnmatchcase(parts[-1], segments[0])
    return _match_segments(parts, segments)


def _match_segments(parts: list[str], segments: list[str]) -> bool:
    if not segments:
        return not parts
    head = segments[0]
    if head == '**':
        return (_match_segments(parts, segments[1:])
                or (bool(parts) and _match_segments(parts[1:], segments)))
    if not parts:
        return False
    return (fnmatch.fnmatchcase(parts[0], head)
            and _match_segments(parts[1:], segments[1:]))
