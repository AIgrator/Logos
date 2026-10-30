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
  │    ├─ sync custom providers from settings.json (register, re-merge models)
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
   `get_display_name()` from the parsed source. The scan walks every function
   definition, including class-level `@staticmethod`s, so real names are read
   without importing the SDK: `OpenAI`, `Anthropic`, `Google`, `OpenRouter`,
   `Hugging Face`, `xAI`. Only when a method does not return a plain string
   literal does the loader fall back to the filename:
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

**Result:** the UI can list providers immediately with their real names, and no
vendor SDK is loaded during a normal startup unless it belongs to the active
provider.

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
- `LLMProviderWidget` — in the installer's initial setup, where every provider
  page is built up front;
- `ProviderDialog` — only for the provider being connected or edited.

The in-app Providers page itself reads only `PROVIDER_METADATA` and
`ModelConfigManager.PROVIDER_CONFIG`, so opening Settings imports no vendor SDK.

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
   [Dynamic LLM Model Management](./system_architecture.md#model-and-provider-management)).
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

"Update model lists" (the link at the bottom of the Providers page) re-downloads
`models.json`, rejects junk payloads (fewer than
two usable providers), rewrites the cache, and re-runs the merge. Provider
modules are not involved.

---

## Custom providers (OpenAI or Anthropic protocol)

Besides the built-in modules, users can define providers at runtime from
Settings → **Providers** → **Add custom provider…**. Definitions are stored in
`settings.json["custom_providers"]` (id, display name, `protocol`, base URL,
model list, optional headers); per-provider runtime settings (`model`,
`api_key`, `max_tokens`, `require_json_output`) live in `llm_providers[id]` like
any other provider.

`protocol` selects the wire format:

- `openai` (default, also used for legacy definitions without the field) —
  `CustomOpenAIProvider` speaks OpenAI **Chat Completions**: `POST
  {base}/chat/completions`, Bearer auth when a key is set, user headers applied
  last, images as `image_url` data URLs, forced function tools from
  `response_formats.json` in structured modes, and SSE streaming for Chat.
- `anthropic` — `CustomAnthropicProvider` speaks Anthropic **Messages**: `POST
  {base}/v1/messages` (a base already ending in `/messages` or `/v1` is used
  as-is), `anthropic-version: 2023-06-01`, `x-api-key` plus Bearer auth when a
  key is set, `system` at the top level, content blocks (`text`, `thinking` →
  reasoning, `tool_use`), forced `tool_choice` for structured modes, and SSE
  `message_start` / `content_block_delta` / `message_delta` events. This covers
  DeepSeek, Kimi, GLM, LiteLLM and similar Anthropic-compatible gateways.
  Native Anthropic extras (Computer Use, built-in web search, strict tool
  schemas, adaptive thinking) are not offered on these endpoints.

`SettingsManager.sync_custom_providers()` runs during `SettingsManager.__init__`
(before `_ensure_provider_defaults`) and after every Settings apply:

1. Normalizes definitions with `src/llm/custom_providers.py`
   (`normalize_definition`); invalid entries are dropped, unknown protocols fall
   back to `openai`, and ids must match `[a-z0-9_-]{1,32}` and not collide with
   a built-in provider.
2. Calls `register_custom_providers()`, which builds a
   `CustomOpenAIProvider` or `CustomAnthropicProvider` subclass per definition
   and registers it in `PROVIDERS` / `SettingsManager.PROVIDER_CONFIG`. Stale
   custom ids are removed from both registries.
3. Re-runs `ModelConfigManager._load_and_merge_configs()` so the custom
   provider's display name, models and label map appear in the Settings page
   and the model-switch popup without a restart.

Custom ids never pass through the lazy discovery and are not touched by the
background warm-up, which only imports built-in modules.

Custom providers are general-purpose only. `ModeCapabilities` rejects them for
Vision (no native Computer Use), even though they bypass the explicit `general`
model list that `models.json` publishes for built-ins.

Readiness uses `provider_is_configured()` instead of a raw API-key check:
built-in providers still require a key, while a registered custom provider is
usable without one, which is what local servers expect. The worker, controller
startup checks, model-switch popup and Vision model selector all go through
this helper.

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
2. Refuses to instantiate unless `provider_is_configured()` accepts the
   provider: built-in providers need an API key, while registered custom
   providers may be keyless (local servers).
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
provider modules on the GUI thread (several seconds). The in-app page no longer
does that: Settings → **Providers** is built from `PROVIDER_METADATA` and
`ModelConfigManager.PROVIDER_CONFIG`, so opening Settings imports nothing and
shows immediately. A provider form exists only inside the Connect/Edit dialog.
`ProvidersPage._ensure_providers_ready()` waits for the background warm-up and,
while it is still running, shows a small modal `_ProviderLoadingDialog`
("Loading provider settings…", with Cancel) driven by a 150 ms timer; the warm-up
is started if it has not been scheduled. The page also owns the **Vision
(Computer Use)** row: a provider combo listing the configured providers that
have at least one model for which `ModeCapabilities.supports(pid, model,
"vision")` is true, and an editable model combo for their Vision models. A
stored choice whose provider is disconnected or no longer capable stays visible
as "(unavailable)" instead of being dropped. The Settings → Vision page now only
holds the agent-loop tunables and points here.

The Providers page stages its edits in the settings manager
(`set_many(..., save=False, emit=False)`). Connect, Use, custom-provider
add/edit/delete, the active-provider switch and the Vision-model choice apply
immediately in memory so the page stays in sync, and each change emits
`ProvidersPage.changed`. The full-window settings page listens and autosaves
the collected settings (`active_provider`, `llm_providers`, `custom_providers`,
`mode_models` and the other sections) after its 400 ms debounce; after each
write it calls `providers_page.commit()`, which removes keyring keys belonging
to deleted custom providers. There is no OK/Cancel — leave settings with Back or
Esc. The installer's initial setup still uses
`LLMProviderWidget`, which builds every provider page up front.

---

## Why not import everything up front?

The vendor SDKs (OpenAI, Anthropic, Google GenAI, xAI, Hugging Face, plus
`requests`/`httpx`) are heavy and can perform slow top-level initialization
(measured cold: anthropic ~9 s, openai ~2 s, google ~1 s). Importing all of
them in the worker delayed the first request by seconds for providers the user
may never select. Source-only discovery keeps startup and model switching fast;
the eager import now runs on a background thread after the window is up instead
of blocking any user action. If a provider is connected before that background
pass finishes, only then does the provider dialog wait — behind a small busy
indicator.

The trade-off: any code that needs static provider configuration must trigger a
single-module import through the proxy. That is why `SettingsManager` stores
only metadata and why the worker explicitly merges
`provider_class.get_provider_config()` before instantiation.
