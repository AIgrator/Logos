"""
This module contains the concrete implementation for interacting with a local desktop environment.
It wraps libraries like pyautogui to perform actions.
"""
import logging
import threading
import time
import os
import importlib
from functools import partial


def tool_meta(tool=None, flags=None):
    def decorator(fn):
        fn.__tool_meta__ = {
            "tool": tool or {},
            "flags": flags or {},
        }
        return fn
    return decorator

class Computer:
    """
    Implements the ComputerInterface for a local desktop environment.
    This class contains the "muscles" of the agent, directly interacting with the OS.
    It uses lazy loading for pyautogui and pyperclip to speed up startup time.
    """
    def __init__(self, settings_manager=None, mode: str = "vision", working_directory: str = None):
        self.vision_manager = None
        self.settings_manager = settings_manager
        self.working_directory = working_directory
        self._pyautogui = None
        self._pyperclip = None
        # Cooperative cancellation + live progress for long-running tools
        # (used by execute_shell; set by whoever runs the tools).
        self.cancel_event = threading.Event()
        self.progress_callback = None
        from src.computer.shell_jobs import ShellJobManager
        self.shell_jobs = ShellJobManager()
        self._load_mode_tools(mode)

    def _resolve_path(self, path: str) -> str:
        """Resolves a relative path against the working directory."""
        if os.path.isabs(path):
            return path
        if self.working_directory:
            return os.path.join(self.working_directory, path)
        return path

    def _load_mode_tools(self, mode: str):
        """
        Dynamically loads tool functions for the specified mode and binds them as methods.
        """
        start = time.perf_counter()
        import_start = start
        try:
            mode_module_name = f'src.computer.modes.{mode}'
            mode_module = importlib.import_module(mode_module_name)
            import_duration = time.perf_counter() - import_start
            logging.info(f"  - Import of mode module '{mode}' took {import_duration:.3f}s")

            if hasattr(mode_module, 'get_tools_metadata'):
                meta_start = time.perf_counter()
                raw_metadata = mode_module.get_tools_metadata()
                meta_duration = time.perf_counter() - meta_start
                logging.info(f"  - get_tools_metadata() for mode '{mode}' took {meta_duration:.3f}s")
                for tool_name, tool_func in raw_metadata.items():
                    # Bind the tool function as a method of this instance
                    setattr(self, tool_name, partial(tool_func, self))
        except Exception as e:
            logging.error(f"Error loading tools for mode '{mode}': {e}", exc_info=True)
            raise RuntimeError(f"Failed to load tools for mode '{mode}': {e}") from e
        finally:
            elapsed = time.perf_counter() - start
            logging.info(f"LocalDesktopComputer._load_mode_tools(mode={mode}) took {elapsed:.3f}s")

    @property
    def pyautogui(self):
        """Lazy-loads and returns the pyautogui module."""
        if self._pyautogui is None:
            import pyautogui
            self._pyautogui = pyautogui
        return self._pyautogui

    @property
    def pyperclip(self):
        """Lazy-loads and returns the pyperclip module."""
        if self._pyperclip is None:
            import pyperclip
            self._pyperclip = pyperclip
        return self._pyperclip

    def set_vision_manager(self, vision_manager):
        """
        Injects the initialized VisionManager instance.
        """
        self.vision_manager = vision_manager

    def get_vision_manager(self):
        """
        Returns the VisionManager instance.
        """
        return self.vision_manager

    def take_screenshot(self, window):
        """
        Takes a clean screenshot using the vision manager.
        """
        if not self.vision_manager:
            raise RuntimeError("VisionManager has not been initialized or set.")
        return self.vision_manager.get_clean_screenshot(window)

    def analyze_screenshot(self, screenshot, settings_manager):
        """
        Analyzes a screenshot using the vision manager.
        """
        if not self.vision_manager:
            raise RuntimeError("VisionManager has not been initialized or set.")
        return self.vision_manager.analyze_screen(None, settings_manager, screenshot=screenshot)

    def compress_image_to_bytes(self, image):
        """
        Compresses an image to bytes using the vision manager.
        """
        if not self.vision_manager:
            raise RuntimeError("VisionManager has not been initialized or set.")
        return self.vision_manager.compress_image_to_bytes(image)
