"""
A simple thread pool for managing a limited number of QThreads.
"""
import logging
from PySide6.QtCore import QThread

class ThreadPool:
    def __init__(self, max_threads=5):
        self.max_threads = max_threads
        self.pool = []
        self.available = []

    def get_thread(self) -> QThread:
        """Gets an available thread or creates a new one."""
        if self.available:
            return self.available.pop()

        if len(self.pool) < self.max_threads:
            thread = QThread()
            self.pool.append(thread)
            return thread

        logging.warning("ThreadPool: No available threads. Consider increasing max_threads.")
        return QThread() # Fallback to creating a temporary thread

    def release_thread(self, thread: QThread):
        """Returns a thread to the pool."""
        if thread in self.pool and thread not in self.available:
            self.available.append(thread)

    def cleanup(self, timeout_ms: int = 1500):
        """Quits pool threads with a bounded wait each (never hangs shutdown)."""
        for thread in self.pool:
            try:
                thread.quit()
            except Exception:
                pass
        for thread in self.pool:
            try:
                if not thread.wait(timeout_ms):
                    logging.warning("ThreadPool: a thread did not finish in time; continuing shutdown.")
            except Exception:
                pass
