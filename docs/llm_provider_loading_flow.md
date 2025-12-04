# Architecture: Two-Phase LLM Provider Loading for Fast Startup

The system uses a sophisticated, two-phase mechanism for discovering and integrating LLM providers. This approach is designed to ensure the application starts as quickly as possible, with the user interface (UI) appearing almost instantly, while deferring the "heavy" work of loading provider modules to a background thread.

### Phase 1: Lazy Discovery (On Initial Import)

-   **Entry Point**: `src/llm/__init__.py`
-   **Trigger**: The first time any part of the application imports the `llm` package.
-   **Process**:
    1.  The `_discover_provider_modules_lazily()` function is executed.
    2.  It scans the `src/llm/providers/` directory for files ending in `_provider.py`.
    3.  **Crucially, it does NOT import these modules.** Instead, it reads the raw source code of each file and uses Abstract Syntax Tree (AST) parsing to find and extract the `provider_id` and `display_name`.
    4.  For each provider found, it creates a lightweight `LazyProviderProxy` object. This object stores the metadata but does not hold a reference to the actual provider class.
    5.  The global `PROVIDERS` dictionary is populated with these `LazyProviderProxy` objects.

-   **Result**: The application starts very quickly because no heavy modules (like `anthropic`, `google-generativeai`, etc.) are loaded into memory. The UI has immediate access to the list of all available providers and their names.

### Step 2: Base Configuration Assembly

-   **Entry Point**: `src/settings_manager.py`
-   **Process**:
    1.  `SettingsManager` imports the `PROVIDERS` dictionary, which at this stage contains only `LazyProviderProxy` objects.
    2.  It iterates through this dictionary. When it calls static methods like `get_display_name()` or `get_provider_config()`, it's calling them on the proxy object.
    3.  The proxy object smartly handles these calls:
        -   For `get_display_name()`, it returns the metadata extracted via AST, avoiding an import.
        -   For methods like `get_provider_config()` or `get_default_settings()`, the proxy will perform a **just-in-time, single-module import** to get the necessary data. This is still very fast as it only loads one small module at a time as needed.
    4.  Based on this information, `SettingsManager` builds the `PROVIDER_CONFIG` and `DEFAULTS['llm_providers']` structures.

### Phase 2: Eager Import (In Background Thread)

-   **Entry Point**: `src/llm/llm_worker.py`
-   **Trigger**: When the `LLMWorker` starts in its dedicated background thread.
-   **Process**:
    1.  The `prepare_provider` method in `LLMWorker` makes an explicit call to `llm.import_real_provider_classes()`.
    2.  This function iterates through all provider modules again, but this time it **fully imports** each one.
    3.  As each module is imported, it finds the actual `BaseLLMProvider` subclass within it.
    4.  It then **replaces** the corresponding `LazyProviderProxy` object in the global `PROVIDERS` dictionary with the real, fully-loaded provider class.

-   **Result**: By the time the user needs to make their first LLM call, the `PROVIDERS` dictionary has been seamlessly updated in the background with the actual, operational provider classes, ready for instantiation.

### Step 3 & 4: Model Loading and UI Generation

These steps proceed as before, but with the added benefit of the fast initial startup. The UI Settings window can be opened immediately, as it can build itself using the metadata from the `LazyProviderProxy` objects without waiting for the full import to complete.

**Conclusion:** This two-phase architecture provides the best of both worlds: an extremely fast application start and a fully dynamic, modular provider system. It solves the critical circular dependency problem and ensures the main thread is never blocked by slow module imports.