# settings_manager.py
import json
import locale
import logging
import os
import keyring
import copy
import shutil
import threading

from PySide6.QtCore import QObject, Signal as pyqtSignal

# This import will trigger the dynamic discovery in src/llm/__init__.py
from llm import PROVIDERS, PROVIDER_METADATA

logger = logging.getLogger(__name__)

# Define a unique service name for the application to store credentials
KEYRING_SERVICE_NAME = "Logos-ai-assistant"


class SettingsManager(QObject):
    # Language data categorized by model type for UI logic. This structure is crucial
    # for enforcing selection rules, as only one non-latin group can be selected at a time.
    EASYOCR_LANGUAGE_GROUPS = {
        'latin': {
            'type': 'latin',
            'languages': {
                'en': 'English', 'af': 'Afrikaans', 'bs': 'Bosnian', 'cs': 'Czech', 'cy': 'Welsh',
                'da': 'Danish', 'de': 'German', 'es': 'Spanish', 'et': 'Estonian', 'fr': 'French',
                'ga': 'Irish', 'hr': 'Croatian', 'hu': 'Hungarian', 'id': 'Indonesian', 'is': 'Icelandic',
                'it': 'Italian', 'la': 'Latin', 'lt': 'Lithuanian', 'lv': 'Latvian', 'mi': 'Maori',
                'ms': 'Malay', 'mt': 'Maltese', 'nl': 'Dutch', 'no': 'Norwegian', 'oc': 'Occitan',
                'pl': 'Polish', 'pt': 'Portuguese', 'ro': 'Romanian', 'rs_latin': 'Serbian (latin)',
                'sk': 'Slovak', 'sl': 'Slovenian', 'sq': 'Albanian', 'sv': 'Swedish', 'sw': 'Swahili',
                'tl': 'Tagalog', 'tr': 'Turkish', 'uz': 'Uzbek', 'vi': 'Vietnamese'
            }
        },
        'cyrillic': {
            'type': 'cyrillic',
            'languages': {
                'ru': 'Russian', 'abq': 'Abaza', 'ady': 'Adyghe', 'ava': 'Avar', 'be': 'Belarusian',
                'bg': 'Bulgarian', 'che': 'Chechen', 'dar': 'Dargwa', 'inh': 'Ingush', 'kbd': 'Kabardian',
                'lbe': 'Lak', 'lez': 'Lezghian', 'mn': 'Mongolian', 'rs_cyrillic': 'Serbian (cyrillic)',
                'tab': 'Tabassaran', 'tjk': 'Tajik', 'uk': 'Ukranian'
            }
        },
        'arabic': {'type': 'strict', 'languages': {'ar': 'Arabic', 'fa': 'Persian (Farsi)', 'ug': 'Uyghur', 'ur': 'Urdu'}},
        'bengali': {'type': 'strict', 'languages': {'bn': 'Bengali'}},
        'chinese': {'type': 'strict', 'languages': {'ch_sim': 'Simplified Chinese', 'ch_tra': 'Traditional Chinese'}},
        'devanagari': {
            'type': 'strict',
            'languages': {
                'hi': 'Hindi', 'mr': 'Marathi', 'ne': 'Nepali', 'as': 'Assamese', 'bh': 'Bihari',
                'bho': 'Bhojpuri', 'gom': 'Goan Konkani', 'mah': 'Magahi', 'mai': 'Maithili',
                'new': 'Newari', 'sck': 'Nagpuri', 'pi': 'Pali', 'ang': 'Angika'
            }
        },
        'japanese': {'type': 'strict', 'languages': {'ja': 'Japanese'}},
        'kannada': {'type': 'strict', 'languages': {'kn': 'Kannada'}},
        'korean': {'type': 'strict', 'languages': {'ko': 'Korean'}},
        'tamil': {'type': 'strict', 'languages': {'ta': 'Tamil'}},
        'telugu': {'type': 'strict', 'languages': {'te': 'Telugu'}},
        'thai': {'type': 'strict', 'languages': {'th': 'Thai'}},
        'other': {'type': 'strict', 'languages': {'az': 'Azerbaijani', 'ku': 'Kurdish'}}
    }

    # For backward compatibility and simple UI population, create a flat dictionary
    # in the format {DisplayName: code}, sorted by DisplayName.
    _flat_languages_by_code = {}
    for group_data in EASYOCR_LANGUAGE_GROUPS.values():
        for code, name in group_data['languages'].items():
            _flat_languages_by_code[code] = name

    EASYOCR_LANGUAGES = {
        name: code for code, name in sorted(
            _flat_languages_by_code.items(), key=lambda item: item[1]
        )
    }

    # --- Dynamic Provider Configuration ---
    PROVIDER_CONFIG = {}
    _llm_providers_defaults = {}

    # Build a lightweight provider configuration using the statically parsed metadata.
    # Avoid importing provider modules here so startup remains fast.
    for provider_id, meta in PROVIDER_METADATA.items():
        config = {
            "display_name": meta.display_name or provider_id,
            "models": [],  # This will be populated by ModelConfigManager
            # We intentionally avoid calling provider-specific get_provider_config() here.
        }
        PROVIDER_CONFIG[provider_id] = config
        # Placeholder defaults which may be populated on-demand later.
        _llm_providers_defaults[provider_id] = {}
    # --- End Dynamic Configuration ---


    DEFAULTS = {
        "prevent_multiple_instances": True,
        "active_provider": "google_gemini", # A sensible default
        "llm_providers": _llm_providers_defaults,
        "hotkeys": {
            "toggle_mode": "Ctrl+Shift+A",
            "cancel_request": "Ctrl+Shift+X",
            "regenerate_plan": "Ctrl+Shift+R",
            "edit_plan": "Ctrl+Shift+E",
            "cancel_plan": "Ctrl+Shift+D",
            "run_plan": "Ctrl+Shift+W",
            "pause_resume": "Ctrl+Alt+P",
            "stop_execution": "Ctrl+Alt+S",
            "pause_for_feedback": "Ctrl+Alt+F"
        },
        "compact_mode": {
            "width": 700,
            "height": 120,
            "position": "bottom_center"
        },
        "webp_quality": 80,
        "webp_lossless": True,
        "max_retries": 3,
        "retry_delay_seconds": 2,
        "omniparser": {
            "mode": "local",
            "api_url": "http://127.0.0.1:5000/parse",
            "model_path": "models/omniparser/icon_detect/model.pt",
            "caption_model_name": "florence2",
            "caption_model_path": "models/omniparser/icon_caption_florence",
            "box_threshold": 0.35,
            "text_threshold": 0.8,
            "containment_ratio": 0.8,
            "ocr_languages": ["en"],
            "save_trace_log": True,
            "save_pre_intent_screenshot": True,
            "save_post_screenshot": True,
            "trace_log_path": "logs/parser_trace.json",
            "font_path": "cv2_default",
            "font_size": 16,
            "text_padding": 2
        },
        "allowed_apps": [],
        "vision": {
            "wait_check_interval_ms": 5000,
            "post_action_delay_ms": 1000,
            "max_correction_attempts": 3,
            "allow_shell_commands": False,
            "grounding_backend": "local"
        },
        "browser": {
            "headless": False,
            "profile_dir": "cache/browser_profile",
            "default_timeout_ms": 30000,
            "block_ads": True
        },
        "terminal": {
            "shell_timeout_seconds": 60,
            "shell_detach_seconds": 25
        },
        "context": {
            # Agent history compaction (browser mode). All defaults are
            # no-ops: full history is sent. Enable to cut transmitted context.
            "history_keep_last_n": 0,
            "history_max_result_chars": 0,
            "history_dedup_snapshots": False,
            "history_trim_tracebacks": False
        },
        "theme": "dark",
        "search": {}
    }

    def __init__(self, settings_file='settings.json'):
        super().__init__()
        self.SETTINGS_FILE = settings_file
        self._settings = self._load_settings()
        self._initialize_app_paths()
        # Ensure the active provider has its defaults loaded lazily
        active = self._settings.get('active_provider', self.DEFAULTS.get('active_provider'))
        try:
            self._ensure_provider_defaults(active)
        except Exception:
            logger.debug(f"Failed to load provider defaults for '{active}' at startup; will load on demand.")

    def _initialize_app_paths(self):
        """
        Defines core application paths and stores them in the settings object.
        These are runtime paths and are not saved to settings.json.
        """
        # The project root is the parent directory of the 'src' directory where this file is located.
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        self._settings['APP_PROJECT_ROOT'] = project_root

        # Define other paths based on the project root
        cache_dir = os.path.join(project_root, 'cache')
        self._settings['APP_CACHE_DIR'] = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

        logs_dir = os.path.join(project_root, 'logs')
        self._settings['APP_LOGS_DIR'] = logs_dir
        os.makedirs(logs_dir, exist_ok=True)

        prompts_dir = os.path.join(project_root, 'prompts')
        self._settings['APP_PROMPTS_DIR'] = prompts_dir

        screenshots_dir = os.path.join(project_root, 'screenshots')
        self._settings['APP_SCREENSHOTS_DIR'] = screenshots_dir

        models_dir = os.path.join(project_root, 'models')
        self._settings['APP_MODELS_DIR'] = models_dir

        # Clean up screenshots directory in a background thread
        cleanup_thread = threading.Thread(target=self._cleanup_screenshots_task, daemon=True)
        cleanup_thread.start()

    def _cleanup_screenshots_task(self):
        """Clears and recreates the screenshot log directory."""
        screenshots_dir = self.get('APP_SCREENSHOTS_DIR')
        if not screenshots_dir:
            return
        try:
            if os.path.exists(screenshots_dir):
                shutil.rmtree(screenshots_dir)
            os.makedirs(screenshots_dir)
            logger.info(f"Screenshot log directory '{screenshots_dir}' is cleaned and ready.")
        except Exception as e:
            logger.error(f"Failed to setup screenshot log directory: {e}", exc_info=True)

    def _load_settings(self):
        """
        Loads settings from the JSON file, merges them with defaults, and then
        injects API keys from the secure keyring storage.
        """
        try:
            with open(self.SETTINGS_FILE, encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = self.DEFAULTS.copy()

        # Recursively update missing fields with defaults
        def merge_defaults(current_data, default_data):
            for key, default_value in default_data.items():
                if key not in current_data:
                    current_data[key] = default_value
                elif isinstance(default_value, dict) and isinstance(current_data.get(key), dict):
                    merge_defaults(current_data[key], default_value)

        merge_defaults(data, self.DEFAULTS)

        # --- Securely load API keys from keyring ---
        if "llm_providers" in data:
            for provider_id in data["llm_providers"]:
                try:
                    key = keyring.get_password(KEYRING_SERVICE_NAME, provider_id)
                    if key:
                        data["llm_providers"][provider_id]["api_key"] = key
                except Exception as e:
                    logger.error(f"Could not retrieve API key for '{provider_id}' from keyring: {e}")
        # --- End keyring integration ---

        return data

    def _ensure_provider_defaults(self, provider_id: str):
        """Ensure the provider's default settings are present in the settings.
        This method will import the provider module lazily if required.
        """
        if not provider_id:
            return
        try:
            provider = PROVIDERS.get(provider_id)
            if not provider:
                return
            # Get default settings via the lazy proxy (this will import provider module on-demand)
            defaults = provider.get_default_settings()
            if defaults:
                self._settings.setdefault('llm_providers', {})
                if provider_id not in self._settings['llm_providers']:
                    self._settings['llm_providers'][provider_id] = defaults
        except Exception as e:
            logger.debug(f"Could not load defaults for provider '{provider_id}': {e}")

    def _write_settings(self, settings_dict):
        """
        Extracts API keys to store them in the keyring, removes them from the
        settings dictionary, and then writes the sanitized dictionary to a file.
        """
        # Deep copy to avoid modifying the live settings object
        settings_to_write = copy.deepcopy(settings_dict)

        # --- Securely store API keys in keyring ---
        if "llm_providers" in settings_to_write:
            for provider_id, config in settings_to_write["llm_providers"].items():
                if "api_key" in config and config["api_key"]:
                    try:
                        keyring.set_password(KEYRING_SERVICE_NAME, provider_id, config["api_key"])
                        logger.info(f"Successfully stored API key for '{provider_id}' in keyring.")
                    except Exception as e:
                        logger.error(f"Could not store API key for '{provider_id}' in keyring: {e}")
                # ALWAYS remove the key before writing to the file
                if "api_key" in config:
                    del config["api_key"]
        # --- End keyring integration ---

        try:
            with open(self.SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(settings_to_write, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to write settings to {self.SETTINGS_FILE}: {e}")

    @staticmethod
    def create_default_settings_if_needed(settings_file='settings.json', lang_dict=None):
        """If settings.json does not exist, create it with ui_language determined by system language or 'en'."""
        if os.path.exists(settings_file):
            return
        # This method writes a minimal config; the full defaults are merged on first load.
        # No need to handle API keys here as they don't exist yet.
        sys_lang = None
        try:
            sys_lang = locale.getdefaultlocale()[0]
            if sys_lang:
                sys_lang = sys_lang.split('_')[0]
        except Exception:
            sys_lang = None
        if lang_dict is None:
            lang_dict = {'en': {}}
        ui_language = sys_lang if sys_lang and sys_lang in lang_dict else 'en'

        # Create a minimal settings file. The robust _load_settings will handle merging defaults.
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump({"ui_language": ui_language}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to create initial settings file at {settings_file}: {e}")


    def get(self, key, default=None):
        return self._settings.get(key, default)

    def set(self, key, value, save: bool = True):
        self.set_many({key: value}, save=save)

    def set_many(self, settings_dict: dict, save: bool = True):
        """Set multiple settings at once and emit a single signal.

        With save=False the change stays in memory only (no disk write, no
        settings_changed signal) — used for session-only switches such as
        the toolbar model switcher. Callers then refresh dependents manually.
        """
        self._settings.update(settings_dict)
        for key, value in settings_dict.items():
            # Avoid logging sensitive values like API keys
            log_value = '********' if 'api_key' in key or 'password' in key else value
            logger.info(f"[SettingsManager] Setting '{key}' to '{log_value}' (part of batch, save={save}).")
        if not save:
            return
        logger.info("[SettingsManager] Emitting single settings_changed signal for batch update.")
        self.save()

    def update(self, data: dict):
        self._settings.update(data)
        self.save()

    settings_changed = pyqtSignal(dict)
    def save(self):
        self._write_settings(self._settings)
        self.settings_changed.emit(self._settings)

    def all(self):
        return self._settings
