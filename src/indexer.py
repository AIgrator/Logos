"""
This module provides a file indexer that uses the `watchdog` library to
monitor a directory for real-time changes. It maintains an in-memory index
of file and directory paths, automatically updating it when the file system
is modified.
"""
import os
import logging
import fnmatch
import threading
import time
from PySide6.QtCore import QObject, Signal
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from rapidfuzz import process

class IndexerEventHandler(FileSystemEventHandler):
    """Handles file system events and triggers a re-index."""
    def __init__(self, indexer):
        super().__init__()
        self.indexer = indexer

    def on_any_event(self, event: FileSystemEvent):
        """
        Triggers a re-index for any file system event.
        A debounce mechanism is used to handle bursts of events.
        """
        if not self.indexer._is_ignored(event.src_path):
            self.indexer.schedule_reindex()

class Indexer(QObject):
    """
    A file indexer that watches for file system changes and keeps an up-to-date
    index of file and directory paths.
    """
    index_updated = Signal()

    def __init__(self, root_dir: str):
        super().__init__()
        self.root_dir = os.path.abspath(root_dir)
        self.paths: list[str] = []
        self.ignore_patterns: list[str] = []
        self._lock = threading.Lock()
        self._reindex_timer = None

        self.hardcoded_ignore_dirs: set[str] = {'.git', '.venv', 'venv', 'node_modules', '__pycache__'}

        self.observer = Observer()
        self.event_handler = IndexerEventHandler(self)

    def start(self):
        """
        Starts the initial index build in a background thread and then starts
        the file system observer.
        """
        t0 = time.perf_counter()
        logging.info("Starting initial file index build in background...")
        initial_build_thread = threading.Thread(target=self.build_index, daemon=True)
        initial_build_thread.start()

        self.observer.schedule(self.event_handler, self.root_dir, recursive=True)
        self.observer.start()
        logging.info(f"File system watcher started for: {self.root_dir}")
        logging.info(f"Indexer.start() completed in {time.perf_counter() - t0:.3f}s (observer scheduled, initial build thread launched)")

    def stop(self):
        """Stops the file system observer."""
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            logging.info("Stopped file system watcher.")
        if self._reindex_timer:
            self._reindex_timer.cancel()

    def schedule_reindex(self):
        """
        Schedules a re-index to occur after a short delay (debounce).
        This prevents rapid-fire re-indexing from multiple quick events.
        """
        with self._lock:
            if self._reindex_timer:
                self._reindex_timer.cancel()
            self._reindex_timer = threading.Timer(0.5, self.build_index)
            self._reindex_timer.start()

    def _load_gitignore(self):
        """Loads ignore patterns from .gitignore files."""
        self.ignore_patterns.clear()
        for root, _, files in os.walk(self.root_dir):
            if '.gitignore' in files:
                gitignore_path = os.path.join(root, '.gitignore')
                try:
                    with open(gitignore_path, encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                relative_path = os.path.relpath(root, self.root_dir)
                                if relative_path == '.':
                                    relative_path = ''
                                full_pattern = os.path.join(relative_path, line)
                                self.ignore_patterns.append(full_pattern)
                except Exception as e:
                    logging.warning(f"Could not read .gitignore at {gitignore_path}: {e}")

    def _is_ignored(self, path: str) -> bool:
        """
        Checks if a path should be ignored based on hardcoded rules and
        .gitignore patterns.
        """
        try:
            relative_path = os.path.relpath(path, self.root_dir)
        except ValueError:
            return True

        parts = set(relative_path.split(os.sep))
        if not self.hardcoded_ignore_dirs.isdisjoint(parts):
            return True

        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(relative_path, pattern):
                return True
        return False

    def build_index(self):
        """
        Scans the directory and builds the index. This method is thread-safe.
        """
        start_time = time.time()
        with self._lock:
            logging.info("Rebuilding file index...")
            self._load_gitignore()
            new_paths = []
            indexed_count = 0
            try:
                for root, dirs, files in os.walk(self.root_dir, topdown=True):
                    dirs[:] = [d for d in dirs if not self._is_ignored(os.path.join(root, d))]

                    for d in dirs:
                        dir_path = os.path.relpath(os.path.join(root, d), self.root_dir)
                        new_paths.append(dir_path.replace(os.sep, '/') + '/')
                        indexed_count += 1

                    for file in files:
                        file_path = os.path.relpath(os.path.join(root, file), self.root_dir)
                        if not self._is_ignored(os.path.join(root, file)):
                            new_paths.append(file_path.replace(os.sep, '/'))
                            indexed_count += 1
            except Exception as e:
                logging.error(f"An error occurred during index build: {e}")
                return

            # Sort directories before files, then alphabetically
            new_paths.sort(key=lambda x: (not x.endswith('/'), x.lower()))
            self.paths = new_paths

            duration = time.time() - start_time
            logging.info(f"Index updated: {indexed_count} items found in {duration:.2f} seconds.")
            self.index_updated.emit()

    def search(self, query: str, base_path: str = "") -> list[str]:
        """
        Searches the index for paths matching the query using fuzzy matching.
        """
        with self._lock:
            search_space = [p for p in self.paths if p.startswith(base_path)] if base_path else self.paths
            search_term = query[len(base_path):] if base_path else query

            if not search_term:
                results = []
                for p in search_space:
                    sub_path = p[len(base_path):]
                    if sub_path and '/' not in sub_path.strip('/'):
                        results.append(p)
                results.sort(key=lambda x: (not x.endswith('/'), x.lower()))
                return results

            # Use the standard process.extract without a custom scorer
            results_with_scores = process.extract(
                search_term,
                search_space,
                processor=lambda choice: choice[len(base_path):],
                limit=200,
                score_cutoff=50  # Lowered cutoff to be less strict
            )

            # The tuple is (choice, score, key)
            all_matches = [res[0] for res in results_with_scores]

            return all_matches
