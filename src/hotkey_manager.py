# src/hotkey_manager.py
import logging
from pynput import keyboard
from PySide6.QtCore import QObject

logger = logging.getLogger(__name__)

class HotkeyManager(QObject):
    """Manages global hotkeys for the application using pynput."""

    def __init__(self, controller, settings_manager):
        super().__init__()
        self.controller = controller
        self.settings_manager = settings_manager
        self.listener = None
        self._last_registered_hotkeys = {}

        self.register_hotkeys()
        self.settings_manager.settings_changed.connect(self.on_settings_changed)

    def on_settings_changed(self, new_settings):
        """Re-registers hotkeys if they have changed in the settings."""
        new_hotkeys = new_settings.get('hotkeys', self.settings_manager.DEFAULTS['hotkeys'])
        if self._last_registered_hotkeys != new_hotkeys:
            logger.info("Hotkey settings changed, re-registering...")
            self.register_hotkeys()

    def _to_pynput_format(self, keys):
        """Converts a list of keys like ['Ctrl', 'Shift', 'A'] to pynput format '<ctrl>+<shift>+a'."""
        # Pynput uses specific names for modifier keys
        key_map = {
            'ctrl': 'ctrl',
            'alt': 'alt',
            'shift': 'shift',
            'cmd': 'cmd',
            'win': 'cmd', # Alias for Windows/Super key
            'super': 'cmd',
        }

        formatted_keys = []
        for key in keys:
            lower_key = key.lower()
            if lower_key in key_map:
                # Format as modifier
                formatted_keys.append(f"<{key_map[lower_key]}>")
            else:
                # It's a regular key
                formatted_keys.append(lower_key)

        return "+".join(formatted_keys)

    def register_hotkeys(self):
        """Stops the old listener and registers new hotkeys from settings."""
        self.stop() # Stop any existing listener

        hotkeys_from_settings = self.settings_manager.get('hotkeys', {})

        actions = {
            "toggle_mode": self.controller.request_hotkey_toggle_mode,
            "pause_resume": self.controller.request_hotkey_pause_resume,
            "stop_execution": self.controller.request_hotkey_stop_execution,
            "pause_for_feedback": self.controller.request_hotkey_pause_for_feedback,
            "cancel_request": self.controller.request_hotkey_cancel_request,
            "regenerate_plan": self.controller.request_hotkey_regenerate_plan,
            "edit_plan": self.controller.request_hotkey_edit_plan,
            "cancel_plan": self.controller.request_hotkey_cancel_plan,
            "run_plan": self.controller.request_hotkey_run_plan
        }

        hotkey_map = {}
        for action_name, callback in actions.items():
            hotkey_setting = hotkeys_from_settings.get(action_name)
            if isinstance(hotkey_setting, list) and hotkey_setting:
                try:
                    pynput_str = self._to_pynput_format(hotkey_setting)
                    hotkey_map[pynput_str] = callback
                    logger.info(f"Prepared hotkey '{pynput_str}' for '{action_name}'.")
                except Exception as e:
                    logger.error(f"Failed to format hotkey for '{action_name}': {hotkey_setting}. Error: {e}")

        if hotkey_map:
            try:
                self.listener = keyboard.GlobalHotKeys(hotkey_map)
                self.listener.start()
                logger.info("Hotkey listener started with new hotkeys.")
            except Exception as e:
                # This can happen on Wayland or if there are permission issues
                logger.error(f"Failed to start GlobalHotKeys listener: {e}", exc_info=True)
                self.listener = None

        self._last_registered_hotkeys = hotkeys_from_settings.copy()

    def stop(self):
        """Stops the hotkey listener thread."""
        if self.listener and self.listener.is_alive():
            logger.info("Stopping hotkey listener.")
            self.listener.stop()
            self.listener.join() # Wait for the thread to fully terminate
        self.listener = None

    def __del__(self):
        """Ensure the listener is stopped when the manager is deleted."""
        self.stop()
