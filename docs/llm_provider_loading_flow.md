# Architecture: LLM Provider Discovery and Loading

Logos supports several LLM providers, but a normal launch must not pay the cost
of importing every vendor SDK. The provider layer therefore separates
**discovery** (cheap, source-only) from **import** (on demand, one provider at a
time).

The current implementation is "lazy discovery + active-provider import", plus a
background warm-up. The second phase (`import_real_provider_classes()` in
[`src/llm/__init__.py`](../src/llm/__init__.py)) is started on a daemon thread
by `start_provider_warmup()` ~1.5 s after the main window is shown; it imports
every provider module once, off the GUI thread. `LLMWorker.prepare_provider()`
still imports the selected provider on demand if the warm-up has not reached it
yet.

---

## Startup sequence

```
main.py
  ├─ import llm.model_config_manager
  │    ├─ import settings_manager
  │    │    └─ import llm  ──► lazy discovery of provider files
  │    └─ import llm.mode_capabilities / context.tokens
  ├─ ModelConfigManager.initialize()
  │    └─ merge cached models.json into the provider config
  ├─ settings_manager = SettingsManager()
  │    └─ ensure defaults for the active provider (imports that one module)
  ├─ AppController()
  │    ├─ LLMWorker(...)
  │    ├─ worker.moveToThread(thread)
  │    └─ thread.started ──► LLMWorker.prepare_provider()
  │         └─ get_provider_class(active) ──► import active provider only
  └─ QTimer.singleShot(1500, start_provider_warmup)
       └─ daemon thread ──► import_real_provider_classes()
            └─ replaces every proxy with the real class
```

---

## Phase 1 — source-only discovery

Entry point: [`src/llm/__init__.py`](../src/llm/__init__.py), executed the first
time any module imports the `llm` package.

`_discover_provider_modules_lazily()`:

1. Scans `src/llm/providers/` for modules whose name ends with `_provider.py`.
2. Reads each file's source and parses it with `ast` — **the module is not
   imported**, so no SDK, client library, or network-capable code runs.
3. Tries to extract literal return values of `get_provider_id()` and
   `get_display_name()` from the parsed source. The scan only visits
   module-level functions; provider methods are class members, so in practice
   the loader falls back to the filename:
   - provider id = filename without `_provider` (`x-ai_provider.py` → `x-ai`);
   - display name = id with underscores replaced and title-cased
     (`huggingface` → `Huggingface`).
4. Creates a `ProviderInfo` and a `LazyProviderProxy` per module, stored in
   `PROVIDER_METADATA` and `PROVIDERS`.

A `LazyProviderProxy` delegates the static configuration methods
(`get_display_name`, `get_provider_config`, `get_default_settings`, and any
other attribute) to the real class. The first such call imports that single
module and caches the class in the proxy. `get_provider_id()` is answered from
metadata without an import.

**Result:** the UI can list providers immediately, and no vendor SDK is loaded
during a normal startup unless it belongs to the active provider.

> The module docstring in `src/llm/__init__.py` still describes the old
> two-phase design. The executable behavior above is authoritative.

---

## Base configuration assembly

`SettingsManager` builds a deliberately lightweight class-level
`PROVIDER_CONFIG` from `PROVIDER_METADATA` only:

```python
{
    "display_name": "<from metadata>",
    "models": [],   # filled later by ModelConfigManager
}
```

It does **not** call provider-specific `get_provider_config()` here, because
that would import every provider module during startup. The static provider
configuration (`api_url`, cancellation flags, streaming flags) is merged later,
at the points where it is actually needed:

- `LLMWorker._create_llm_provider()` — when instantiating the active provider;
- `LLMProviderWidget` — when the Settings page is built.

Provider defaults are also lazy. `SettingsManager.__init__` calls
`_ensure_provider_defaults(active_provider)`: the proxy imports that one module
and writes its `get_default_settings()` into `settings.json` if the provider has
no saved entry yet. Other providers receive defaults when they are selected or
when their Settings page is built.

API keys are not stored in `settings.json`. `SettingsManager` keeps them in the
OS keyring and injects them into the in-memory settings on load.

---

## Model configuration merge

`ModelConfigManager.initialize()` runs before `SettingsManager()` is created and
before the UI exists. It:

1. Deep-copies `SettingsManager.PROVIDER_CONFIG`.
2. Loads `cache/models.json` (downloaded from the models repository; see
   [Dynamic LLM Model Management](./system_architecture.md#dynamic-llm-model-management)).
3. Merges each provider's `models` or `models_by_provider` list into the copy.
4. Applies built-in fallback lists when a cached provider list is empty or
   incomplete, so dropdowns never go blank.
5. Installs `mode_capabilities` into `ModeCapabilities` (which models support
   chat/terminal/browser vs Vision/Computer Use) and `model_metadata` into
   `ModelMetadata` (context window, reasoning, input modalities).
6. Stores the result as `ModelConfigManager.PROVIDER_CONFIG`.

The Settings UI and the model-switch popup read model lists from
`ModelConfigManager.PROVIDER_CONFIG`. `SettingsManager.PROVIDER_CONFIG` remains
the lightweight startup copy.

"Update Models" re-downloads `models.json`, rejects junk payloads (fewer than
two usable providers), rewrites the cache, and re-runs the merge. Provider
modules are not involved.

---

## Phase 2 — active provider import

Entry point: `LLMWorker.prepare_provider()`, connected to
`QThread.started` in [`src/controller.py`](../src/controller.py).

```python
def prepare_provider(self):
    if self.provider:
        return
    self.provider = self._create_llm_provider(self.settings)
    self.provider_ready = self.provider is not None
```

`_create_llm_provider()`:

1. Reads `active_provider` and that provider's saved settings.
2. Refuses to instantiate if no API key is present.
3. Calls `get_provider_class(active_provider)`. For a proxy this imports the
   module on first use, finds the concrete `BaseLLMProvider` subclass, and
   replaces the proxy's cached class; `PROVIDERS` itself still holds the proxy.
4. Merges, in order: `provider_class.get_provider_config()` → the lightweight
   `SettingsManager.PROVIDER_CONFIG` entry → the user's saved settings →
   `name` / selected `model`.
5. Instantiates the provider.

Switching to another provider calls the same factory for the new id, importing
only that module. The previous provider instance is discarded.

Opening Settings used to be the other lazy-import trigger: `LLMProviderWidget`
iterates all `PROVIDERS` and calls `get_provider_config()`, `get_ui_schema()`,
and `get_default_settings()` to build every provider page, which imported all
provider modules on the GUI thread (several seconds). Now Settings never pays
that cost on open: the window is created immediately, and if the warm-up has
not finished yet, the General page shows a centered `BusySpinner` (a rotating
theme-accent arc, ~15% of the window height) with a "Loading provider
settings…" caption below it, and the Vision model selector is hidden because it
has no configured providers to list. A 150 ms timer waits for
`providers_ready()` and then builds `LLMProviderWidget` on the GUI thread
(imports are already done, so this takes well under a second) and reveals the
Vision selector. Saving while the spinner is still shown keeps the stored
`active_provider` and `llm_providers` values instead of overwriting them.

---

## Why not import everything up front?

The vendor SDKs (OpenAI, Anthropic, Google GenAI, xAI, Hugging Face, plus
`requests`/`httpx`) are heavy and can perform slow top-level initialization
(measured cold: anthropic ~9 s, openai ~2 s, google ~1 s). Importing all of
them in the worker delayed the first request by seconds for providers the user
may never select. Source-only discovery keeps startup and model switching fast;
the eager import now runs on a background thread after the window is up instead
of blocking any user action. If Settings is opened before that background pass
finishes, only then does the General page wait — behind a busy indicator.

The trade-off: any code that needs static provider configuration must trigger a
single-module import through the proxy. That is why `SettingsManager` stores
only metadata and why the worker explicitly merges
`provider_class.get_provider_config()` before instantiation.
