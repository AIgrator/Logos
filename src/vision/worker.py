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

    @staticmethod
    def _internet_available(host: str = "huggingface.co", timeout: float = 5.0) -> bool:
        """Fast connectivity pre-check (DNS + TCP, no HTTP)."""
        import socket
        try:
            socket.create_connection((host, 443), timeout=timeout).close()
            return True
        except OSError:
            return False

    def _enable_hf_offline_if_cached(self):
        """Set HF_HUB_OFFLINE=1 when local models exist.

        Explicit user setting (env) always wins. Auto-detection only kicks
        in when the OmniParser model files are already on disk, so first
        installs still download normally.
        """
        import os
        if os.environ.get("HF_HUB_OFFLINE"):
            logging.info(f"HF_HUB_OFFLINE already set to '{os.environ['HF_HUB_OFFLINE']}'.")
            return
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        needed = [
            os.path.join(base, 'models', 'omniparser', 'icon_detect', 'model.pt'),
            os.path.join(base, 'models', 'omniparser', 'icon_caption_florence', 'model.safetensors'),
        ]
        if all(os.path.isfile(p) for p in needed):
            if not self._internet_available():
                logging.warning("No internet (huggingface.co unreachable): starting Vision from local model cache.")
            os.environ["HF_HUB_OFFLINE"] = "1"
            logging.info("Local vision models found; enabled HF_HUB_OFFLINE=1 (no huggingface.co requests).")
        else:
            if not self._internet_available():
                raise RuntimeError(
                    "Нет интернета (huggingface.co недоступен), а локальные модели еще не скачаны. "
                    "Проверьте соединение и перезапустите программу — модели загрузятся автоматически."
                )
            logging.info("Local vision models incomplete; Hugging Face Hub stays online for downloads.")

    def run(self):
        """
        The main worker method that performs heavy imports and initialization.
        """
        logging.info("VisionWorker started: Importing and initializing vision modules in background...")
        try:
            # 1. Deferred heavy imports
            from vision.manager import VisionManager

            # 2. Offline shortcut: if local models are already downloaded but
            # huggingface.co is unreachable (region block / no internet),
            # force transformers/huggingface_hub to use the local cache
            # instead of retrying downloads for a minute on every start.
            # Fresh installs (no local models yet) stay online.
            self._enable_hf_offline_if_cached()

            # 3. Create the instance inside the thread
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
