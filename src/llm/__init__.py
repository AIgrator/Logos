"""
llm package initialization.
This module implements a two-phase provider discovery for fast startup.

Phase 1 (Lazy Discovery): On initial import, it scans provider module files
to extract minimal metadata (provider_id, display_name) via AST parsing without
importing the provider modules themselves. This populates `PROVIDER_METADATA` and
fills the `PROVIDERS` dictionary with `LazyProviderProxy` objects, allowing the UI
to display a list of providers instantly.

Phase 2 (Eager Import): A public function, `import_real_provider_classes`, is provided.
This function should be called later (e.g., in a background thread) to perform the
full import of all provider modules, replacing the proxy objects in `PROVIDERS` with
the actual provider classes.
"""
import ast
import importlib
import inspect
import logging
import os
import time
import pkgutil
from dataclasses import dataclass
from types import ModuleType
from typing import Optional, Union
from src.llm.base_provider import BaseLLMProvider

logger = logging.getLogger(__name__)


def _extract_provider_metadata_from_source(path: str):
    """Parse a provider module source and try to extract values returned from
    get_provider_id() and get_display_name() if they return string literals.
    This avoids importing the module and executing heavy top-level imports.
    """
    provider_id = None
    display_name = None
    try:
        with open(path, encoding='utf-8') as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ('get_provider_id', 'get_display_name'):
                for sub in node.body:
                    if isinstance(sub, ast.Return):
                        if isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str):
                            if node.name == 'get_provider_id':
                                provider_id = sub.value.value
                            else:
                                display_name = sub.value.value
    except Exception:
        pass
    return provider_id, display_name


@dataclass
class ProviderInfo:
    provider_id: str
    module_name: str
    display_name: str | None = None

    def __repr__(self):
        return f"ProviderInfo(provider_id={self.provider_id}, module_name={self.module_name}, display_name={self.display_name})"


class LazyProviderProxy:
    """Proxy wrapper that lazily imports and resolves the concrete provider class
    on first access. It delegates static methods to the loaded provider class.
    """
    def __init__(self, info: ProviderInfo):
        self.info = info
        self._provider_class = None

    def _load_class(self):
        if self._provider_class is None:
            module = importlib.import_module(self.info.module_name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseLLMProvider) and obj is not BaseLLMProvider and not inspect.isabstract(obj):
                    self._provider_class = obj
                    break
            if self._provider_class is None:
                raise ImportError(f"Could not find provider class in module {self.info.module_name}")
        return self._provider_class

    def get_provider_id(self):
        return self.info.provider_id

    def get_display_name(self):
        if self.info.display_name:
            return self.info.display_name
        return self._load_class().get_display_name()

    def get_provider_config(self):
        return self._load_class().get_provider_config()

    def get_default_settings(self):
        return self._load_class().get_default_settings()

    def __getattr__(self, name):
        cls = self._load_class()
        return getattr(cls, name)


# --- Phase 1: Lazy Discovery (runs on import) ---
PROVIDER_METADATA: dict[str, ProviderInfo] = {}
PROVIDERS: dict[str, LazyProviderProxy | type[BaseLLMProvider]] = {}

def _discover_provider_modules_lazily():
    """
    Performs the initial lightweight discovery to populate metadata and proxies
    for a fast application start.
    """
    providers_dir = os.path.join(os.path.dirname(__file__), 'providers')
    if not os.path.isdir(providers_dir):
        return
    prefix = f"{__name__}.providers."
    for _, module_name, _ in pkgutil.iter_modules([providers_dir], prefix=prefix):
        if not module_name.endswith('_provider'):
            continue

        file_name = module_name.split('.')[-1] + '.py'
        file_path = os.path.join(providers_dir, file_name)
        provider_id, display_name = _extract_provider_metadata_from_source(file_path)

        inferred_id = module_name.split('.')[-1].replace('_provider', '')
        if provider_id is None:
            provider_id = inferred_id
        if display_name is None:
            display_name = inferred_id.replace('_', ' ').title()

        info = ProviderInfo(provider_id=provider_id, module_name=module_name, display_name=display_name)
        PROVIDER_METADATA[provider_id] = info
        if provider_id not in PROVIDERS:
            PROVIDERS[provider_id] = LazyProviderProxy(info)

# --- Phase 2: Eager Import (called explicitly) ---
_are_real_providers_imported = False

def import_real_provider_classes():
    """
    Dynamically discovers and registers the actual LLM provider classes,
    replacing the lazy proxies in the `PROVIDERS` dictionary.
    This is intended to be called after app startup to avoid circular dependencies.
    """
    global _are_real_providers_imported
    if _are_real_providers_imported:
        return

    discover_start = time.perf_counter()
    logger.debug("Starting real provider class import")

    prefix = f"{__name__}.providers."
    providers_dir = os.path.join(os.path.dirname(__file__), 'providers')

    for _, module_name, _ in pkgutil.iter_modules([providers_dir], prefix=prefix):
        if not module_name.endswith('_provider'):
            continue
        try:
            module = importlib.import_module(module_name)
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseLLMProvider) and obj is not BaseLLMProvider and not inspect.isabstract(obj):
                    provider_id = obj.get_provider_id()
                    if provider_id:
                        PROVIDERS[provider_id] = obj # Replace proxy with real class
                        logger.info(f"Registered real provider class for: {provider_id}")
                    else:
                        logger.warning(f"Provider class {name} in {module_name} is missing a provider_id.")
        except Exception as e:
            logger.error(f"Failed to import or inspect provider module {module_name}: {e}", exc_info=True)

    _are_real_providers_imported = True
    duration = time.perf_counter() - discover_start
    logger.info(f"Real provider class import completed in {duration:.4f}s")


# --- Utility Functions ---
def get_provider_class(provider_id: str):
    """Return the provider class (actual class) for a given provider_id, importing it if needed."""
    if provider_id not in PROVIDERS:
        raise KeyError(f"Unknown provider: {provider_id}")

    provider = PROVIDERS[provider_id]
    if isinstance(provider, LazyProviderProxy):
        return provider._load_class()
    return provider

def get_provider_info(provider_id: str) -> ProviderInfo:
    return PROVIDER_METADATA.get(provider_id)

# --- Main Execution on Import ---
# Run the fast, lazy discovery when the package is first imported.
_discover_provider_modules_lazily()
