# src/llm/model_config_manager.py
import json
import logging
import os
import time
import copy
from settings_manager import SettingsManager

logger = logging.getLogger(__name__)

# Measure module import time for debugging but only log via logger
_import_start_ts = time.perf_counter()
logger.debug(f"[ModelConfigManager] import start: {_import_start_ts}")

class ModelConfigManager:
    """
    Manages the LLM provider configuration by taking the dynamically generated
    base configuration from SettingsManager and merging it with an up-to-date
    model list from a local cache or remote source.
    """
    # The new, correct URL for fetching the models list from the dedicated repository.
    MODELS_URL = "https://aigrator.github.io/LLM-models/models.json"
    # The old URL, kept for reference or local testing.
    # MODELS_URL = f"file://{os.path.abspath('models.json')}"
    CACHE_PATH = os.path.abspath('cache/models.json')

    # This will hold the final, merged provider configuration.
    PROVIDER_CONFIG = None

    @staticmethod
    def initialize():
        """
        Initializes the provider configuration. This should be called once at startup.
        It loads the model configuration from cache and merges it with the base config.
        """
        if ModelConfigManager.PROVIDER_CONFIG is None:
            ts = time.perf_counter()
            logger.info("Initializing ModelConfigManager...")
            ModelConfigManager._load_and_merge_configs()
            dur = time.perf_counter() - ts
            logger.info(f"ModelConfigManager.initialize took {dur:.3f}s")

    @staticmethod
    def update_models_from_source():
        """
        Downloads the latest models.json from the MODELS_URL, saves it to the cache,
        and then reloads the provider configuration.

        Returns:
            tuple[bool, str]: A tuple of (success, message).
        """
        logger.info(f"Attempting to update models from {ModelConfigManager.MODELS_URL}...")
        try:
            os.makedirs(os.path.dirname(ModelConfigManager.CACHE_PATH), exist_ok=True)

            if ModelConfigManager.MODELS_URL.startswith('file://'):
                source_path = ModelConfigManager.MODELS_URL[len('file://'):]
                import shutil
                shutil.copy(source_path, ModelConfigManager.CACHE_PATH)
                message = "Successfully copied models from local source."
            else:
                # import requests lazily so import-time cost is reduced
                import requests

                logger.info("Starting network request to download models.json...")
                network_start_time = time.perf_counter()

                response = requests.get(ModelConfigManager.MODELS_URL, timeout=30)

                network_end_time = time.perf_counter()
                network_duration = network_end_time - network_start_time
                logger.info(f"Network request finished. Duration: {network_duration:.4f} seconds.")

                response.raise_for_status()
                with open(ModelConfigManager.CACHE_PATH, 'w', encoding='utf-8') as f:
                    json.dump(response.json(), f, indent=2)
                message = "Successfully downloaded and cached models."

            logger.info(message)
            ModelConfigManager._load_and_merge_configs()
            return True, message

        except Exception as e:
            message = f"Failed to update models: {e}"
            logger.error(message, exc_info=True)
            return False, message

    @staticmethod
    def _load_and_merge_configs():
        """
        Loads the provider configuration. It starts with the dynamic base from
        SettingsManager and merges the model lists from the cached models.json.
        """
        # 1. Start with a deep copy of the dynamically generated base configuration
        t_start = time.perf_counter()
        base_config = copy.deepcopy(SettingsManager.PROVIDER_CONFIG)
        logger.debug(f"Loading base provider config from SettingsManager.PROVIDER_CONFIG. Keys: {list(base_config.keys())}")
        t_deepcopy = time.perf_counter()
        logger.debug(f"Deep copy took {t_deepcopy - t_start:.4f}s")

        # 2. Try to load the cached models.json
        cached_models = {}
        if os.path.exists(ModelConfigManager.CACHE_PATH):
            try:
                t_open = time.perf_counter()
                with open(ModelConfigManager.CACHE_PATH, encoding='utf-8') as f:
                    cached_models = json.load(f)
                t_read = time.perf_counter()
                logger.debug(f"Reading cached models took {t_read - t_open:.4f}s. Keys: {list(cached_models.keys())}")
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"Failed to load or parse {ModelConfigManager.CACHE_PATH}: {e}. Model lists may be empty.")
        else:
            logger.warning(f"Cache file not found at {ModelConfigManager.CACHE_PATH}. Model lists will be empty.")

        # 3. Merge the model data into the base configuration
        t_merge_start = time.perf_counter()
        for provider_id, provider_data in base_config.items():
            if provider_id in cached_models:
                model_info = cached_models[provider_id]
                logger.debug(f"Merging models for provider '{provider_id}'. Found in cache.")
                # Safely update models and models_by_provider if they exist in the cached file
                if 'models' in model_info:
                    provider_data['models'] = model_info['models']
                if 'models_by_provider' in model_info:
                    provider_data['models_by_provider'] = model_info['models_by_provider']
            else:
                logger.debug(f"Provider '{provider_id}' not found in cached models. No merge performed.")

        t_merge_end = time.perf_counter()
        ModelConfigManager.PROVIDER_CONFIG = base_config
        total_dur = time.perf_counter() - t_start
        logger.info("ModelConfigManager initialized successfully with merged configuration.")
        logger.info(f"_load_and_merge_configs: deepcopy {t_deepcopy - t_start:.4f}s, read {t_read - t_open if 't_read' in locals() else 0:.4f}s, merge {t_merge_end - t_merge_start:.4f}s, total {total_dur:.4f}s")

    # Mark module import end
    _import_end_ts = time.perf_counter()
    logger.debug(f"[ModelConfigManager] import complete, duration: {_import_end_ts - _import_start_ts:.4f}s")


