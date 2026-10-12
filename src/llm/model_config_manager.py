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

    # Fallback model lists used when the remote cache is missing, incomplete,
    # or over-filtered (e.g. only 1 junk model). Keeps UI dropdowns usable.
    # Values mirror provider get_default_settings() + known-stable models.
    FALLBACK_MODELS = {
        "google": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"],
        "openai": ["gpt-5-mini", "gpt-5", "gpt-5-nano", "gpt-5.6-sol", "gpt-6-sol"],
        "anthropic": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-4-5"],
        "x-ai": ["grok-4.7", "grok-4.6", "grok-4.5", "grok-4-1-fast-reasoning"],
        # Hugging Face Router (OpenAI-compatible). Suffix ":provider" pins the
        # inference provider; ids without suffix are auto-routed by HF.
        "huggingface": [
            "Qwen/Qwen3.8-27B:novita",
            "Qwen/Qwen2.5-VL-3B-Instruct",
            "openai/gpt-oss-120b",
            "zai-org/GLM-4.5V",
            "meta-llama/Llama-3.1-8B-Instruct",
        ],
        "openrouter": {
            "Anthropic": ["claude-sonnet-5.5", "claude-haiku-5.5", "claude-opus-5.5"],
            "Google": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"],
            "OpenAI": ["gpt-5-mini", "gpt-5", "gpt-5.6-sol"],
            "X-AI": ["grok-4.7", "grok-4.6", "grok-4-1-fast-reasoning"],
            "Meta": ["muse-spark-1.3", "muse-glimmer-30b", "llama-4-maverick"],
        },
    }

    @staticmethod
    def _is_valid_models_payload(data: dict) -> bool:
        """Rejects junk payloads (e.g. only openrouter/1 model) so they never
        overwrite a good cache. Requires >=2 providers with non-empty lists."""
        if not isinstance(data, dict) or not data:
            return False
        usable_providers = 0
        for _pid, info in data.items():
            if not isinstance(info, dict):
                continue
            models = info.get("models") or []
            by_provider = info.get("models_by_provider") or {}
            total = len(models) + sum(len(v) for v in by_provider.values() if isinstance(v, list))
            if total > 0:
                usable_providers += 1
        return usable_providers >= 2

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
                payload = response.json()
                if not ModelConfigManager._is_valid_models_payload(payload):
                    message = (
                        "Downloaded models.json looks like junk "
                        f"(keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}), "
                        "keeping existing cache."
                    )
                    logger.warning(message)
                    # Still reload from existing (good) cache so UI stays populated.
                    ModelConfigManager._load_and_merge_configs()
                    return False, message
                with open(ModelConfigManager.CACHE_PATH, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, indent=2)
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
            logger.info(f"No cached models at {ModelConfigManager.CACHE_PATH} (e.g. cache was cleared). Using built-in model lists; press 'Update Models' to fetch the latest.")

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

        # 4. Fallback: fill empty providers so UI dropdowns never end up blank
        # (remote generator currently over-filters to 1 junk model).
        for provider_id, provider_data in base_config.items():
            fb = ModelConfigManager.FALLBACK_MODELS.get(provider_id)
            if provider_id == "openrouter":
                by_provider = provider_data.get("models_by_provider") or {}
                total = sum(len(v) for v in by_provider.values() if isinstance(v, list))
                if isinstance(fb, dict):
                    if total == 0:
                        provider_data["models_by_provider"] = copy.deepcopy(fb)
                        logger.warning("openrouter cache empty, applied FALLBACK_MODELS.")
                    else:
                        # Merge missing groups so junk cache (only Meta) still gets
                        # Anthropic/Google/OpenAI/X-AI dropdown entries.
                        merged = copy.deepcopy(fb)
                        for grp, lst in by_provider.items():
                            if isinstance(lst, list):
                                merged[grp] = sorted(set(merged.get(grp, []) + lst))
                        if merged != by_provider:
                            provider_data["models_by_provider"] = merged
                            logger.warning("openrouter cache partial, merged FALLBACK groups.")
            else:
                if not provider_data.get("models") and isinstance(fb, list):
                    provider_data["models"] = list(fb)
                    logger.warning(f"Provider '{provider_id}' cache empty, applied FALLBACK_MODELS.")

        t_merge_end = time.perf_counter()
        ModelConfigManager.PROVIDER_CONFIG = base_config
        total_dur = time.perf_counter() - t_start
        logger.info("ModelConfigManager initialized successfully with merged configuration.")
        logger.info(f"_load_and_merge_configs: deepcopy {t_deepcopy - t_start:.4f}s, read {t_read - t_open if 't_read' in locals() else 0:.4f}s, merge {t_merge_end - t_merge_start:.4f}s, total {total_dur:.4f}s")

    # Mark module import end
    _import_end_ts = time.perf_counter()
    logger.debug(f"[ModelConfigManager] import complete, duration: {_import_end_ts - _import_start_ts:.4f}s")


