"""
Background worker for running screen analysis in a separate thread.
"""
import logging
from PySide6.QtCore import QObject, Signal, Slot
from vision.analyzer import VisionAnalyzer

class AnalyzerWorker(QObject):
    """
    A worker that runs the screen analysis process in a background thread
    to avoid blocking the main UI.
    """
    analysis_complete = Signal(object, object, object)  # screenshot, annotated_frame, parsed_content
    error = Signal(str)

    def __init__(self, vision_analyzer: VisionAnalyzer, screenshot):
        super().__init__()
        self.analyzer = vision_analyzer
        self.screenshot = screenshot

    @Slot()
    def run(self):
        """
        The main worker method that performs the screen analysis.
        """
        logging.info("AnalyzerWorker started...")
        try:
            screenshot, annotated_frame, parsed_content = self.analyzer.analyze_screen(self.screenshot)
            self.analysis_complete.emit(screenshot, annotated_frame, parsed_content)
        except Exception as e:
            logging.error(f"Error during screen analysis in worker: {e}", exc_info=True)
            self.error.emit(str(e))
