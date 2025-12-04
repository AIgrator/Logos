"""
This module defines the background worker for initializing the vision system.
All heavy imports and initializations are performed here to keep the UI responsive.
"""
import logging
from PySide6.QtCore import QObject, Signal

class VisionWorker(QObject):
    """
    A worker that imports, creates, and initializes the vision system in a
    background thread to avoid blocking the main UI thread.
    """
    vision_ready = Signal(object)
    finished = Signal()
    error = Signal(str)

    def __init__(self, settings: dict):
        """
        Args:
            settings (dict): The application settings required for initialization.
        """
        super().__init__()
        self.settings = settings

    def run(self):
        """
        The main worker method that performs heavy imports and initialization.
        """
        logging.info("VisionWorker started: Importing and initializing vision modules in background...")
        try:
            # 1. Deferred heavy imports
            from vision.manager import VisionManager

            # 2. Create the instance inside the thread
            vision_manager = VisionManager()
            vision_manager.update_capture_settings(self.settings)

            # 3. Perform the heavy initialization
            vision_manager.initialize_parser_async(self.settings)

            # 4. Emit the ready instance back to the main thread
            self.vision_ready.emit(vision_manager)
            self.finished.emit()

        except Exception as e:
            logging.error(f"Error initializing vision system in background: {e}", exc_info=True)
            self.error.emit(str(e))
