# Logos AI Assistant

This is a desktop assistant application that uses Large Language Models (LLMs) to perform user tasks on a computer. A session starts as a chat and gains capabilities as you need them: **Terminal**, **Browser** and **Vision** are attached to the open session from the composer's capability controls, and one timeline keeps every step.

For detailed technical information about the system architecture, please refer to the [**System Architecture Overview for Developers**](./docs/system_architecture.md).

## Download

Ready-to-run Windows builds are published in [**Releases**](https://github.com/AIgrator/Logos/releases/latest). You do not need Python or any dependencies for them.

| Asset | What it is |
| --- | --- |
| `LogosSetup.exe` | **Recommended.** Per-user installer (no administrator rights). Adds the Start Menu shortcut and, as optional tasks, the `logos` command, the Explorer "Open with Logos" folder menu and a desktop shortcut. Uninstalls from Settings → Installed apps. |
| `Logos.exe` | Portable single file. Nothing to install; just run it. |

Both builds keep settings, logs and cache in `%LOCALAPPDATA%\Logos`, not next to the executable, while each project keeps its own data (sessions, attachments, screenshots) in a `.logos` folder at its root. On first launch you pick a provider and paste its API key, so no extra files are needed.

> [!NOTE]
> Builds are published for Windows. On Linux, run from source (see below).

## Installation (from source)

To automatically install dependencies:

**Windows:**
```powershell
powershell ./setup_windows.ps1
```

**Linux (Ubuntu/Debian):**
```bash
bash ./setup_ubuntu.sh
```

> [!IMPORTANT]
> **Graphics Server Requirements:** The application currently supports only **X.Org (X11)**. Operation in a Wayland session is not supported.

### Manual Dependency Installation

If you prefer to install dependencies manually, sync the environment from the lockfile:

```bash
uv sync
```

## Building the Windows Executable and Installer

The release assets are built from a source checkout on Windows. Both tools are fetched on demand (`uv run --with ...`) and are deliberately **not** project dependencies.

**1. Single-file executable** (takes about six minutes):

```powershell
uv run --with pyinstaller pyinstaller logos.spec --noconfirm --clean
```

This produces `dist/Logos.exe`. The one-file build unpacks itself to a temporary folder at launch, so settings, logs and cache live in `%LOCALAPPDATA%\Logos`; project data stays in each project's `.logos` folder.

**2. Installer** (wraps that executable, needs [Inno Setup 6](https://jrsoftware.org/isdl.php)):

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\logos.iss
```

This produces `dist/LogosSetup.exe` (per-user install into `%LOCALAPPDATA%\Programs\Logos`, Start Menu shortcut, optional `PATH` entry, Explorer folder menu and desktop shortcut, and an uninstaller).

Compilation alone is not proof: launch the built file and make sure it starts, since the frozen app uses `%LOCALAPPDATA%\Logos` for settings, logs, cache and the materialized `prompts/` + `assets/`.

`build/` and `dist/` are git-ignored. Release binaries are attached to GitHub Releases, never committed to the repository (GitHub also rejects files above 100 MB, and each build is about 200 MB).

## Getting a Free API Key (Google Gemini Example)

The assistant requires an API key from an AI provider to function. If you don't have one yet, here's how to get one for free (the "gemini-2.5-flash" model is recommended):
1.  Go to the following link: [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2.  Log in to your Google account.
3.  Click "Create API key".
4.  Copy the generated key—you will need it during the first step of the setup wizard.

> **Secure API Key Storage**: We take the security of your data seriously. Instead of storing your API key in a plain text `settings.json` file, the application uses your operating system's native, secure storage (like Windows Credential Manager, macOS Keychain, or Secret Service in Linux) with the help of the `keyring` library.

## Setup and First Run

When you launch the application for the first time, if the `settings.json` file is missing, the **initial setup wizard** will automatically start. It will help you quickly prepare the application for use.

**Step 1: Configure AI Provider**
In this step, you will select your preferred AI provider from a list and paste the API key you obtained earlier.

**Step 2: Final Setup (source checkouts only)**
In the final step, you can choose additional integration options, such as:
* 1. Creating a global `logos` command.
* 2. Creating a desktop shortcut. 
* 3. Adding an item to the context menu to open a folder in the application's context, which is convenient for terminal work.

The Windows installer offers the same integrations as tasks (checked by default), so an installed build goes straight from the provider step to the application.

## Launching the Application

To run the application from the checkout, let `uv` use the project environment:
```bash
uv run main.py
```

If you installed using the setup script (setup_windows.ps1 or setup_ubuntu.sh), a shorter command becomes available:
```bash
logos
```

> [!TIP]
> A plain `python main.py` works too, but only after activating the virtual environment the setup script created (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Linux).

### Working in a Specific Directory

The program accepts a folder path as an argument and opens that folder as the active project. The project root is the working directory for the **Terminal** and **Browser** capabilities, the root for `@`-file search and indexing, and the place where project data is stored in `.logos/`.

If you have integrated the application with the Windows context menu (an option during installation or in the first setup of a source checkout), you can right-click any folder and select "Open with Logos" to launch the assistant in that directory. If Logos is already running, the folder opens in the existing window instead of starting a second copy.

**Linux/macOS:**
```bash
uv run main.py /home/user/my_project
```

**Windows:**
```powershell
uv run main.py C:\Users\User\Documents\my_project
```
If no path is specified, the remembered active project is used. When there is no usable remembered project, the full-screen **Home** page opens and lets you pick or add one.

### Capabilities and Panels

A session is not locked to one mode. It holds **capabilities** — what it is
allowed to do — and the composer's capability controls (Chat, Terminal, Browser,
Vision) decide both what is attached and which panel the next turn uses:

*   A check marks an attached capability; the highlighted control is the panel
    the next turn answers with.
*   Clicking a control opens that panel and attaches its capability to the
    **open** session. The journal, the transcript and the earlier steps stay;
    nothing is summarized or cleared.
*   Right-click → **Detach … from session** revokes a Terminal, Browser or
    Vision capability. The panel then falls back to the nearest one the session
    still holds.

The four panels differ in what they show and which tools the turn is given:

*   **Chat:** a plain chat with the LLM. You can converse and send files for analysis. When the selected provider supports native web search, the model can search the web on its own during the response. Responses stream in real-time.

*   **Vision:** the assistant uses the selected provider's native **Computer Use** capability to see the screen and interact with it using the mouse and keyboard. It follows the same observe → act → assess discipline as Terminal and Browser but is driven by the provider's Computer Use protocol, so it requires a CUA-capable provider (OpenAI, Anthropic, or Google). It is ideal for automating tasks in GUI applications.

*   **Terminal:** the assistant acts as an AI agent in the command line. No plan is fixed in advance, but for multi-step work the assistant keeps a short checklist of its own (`update_plan`), shown as a compact `Step N / M` pill above the composer that reveals the full plan on hover and updates as it goes. The LLM autonomously determines the next step, executes the command, and analyzes the result, iteratively moving towards the goal. It does not have access to the visual interface but can execute shell commands, work with the file system, run scripts, and interact with web resources. Long-running commands can be launched as background jobs with `execute_shell(run_in_background=true)` and then followed with `poll_shell_job` / `stop_shell_job` / `wait_seconds`. This panel is designed for tasks related to development, file management, and console automation.

*   **Browser:** the assistant drives a real Chromium browser via a DOM snapshot: it sees numbered element references (`browser_snapshot`), clicks/fills/presses them, reads pages, manages tabs, and takes screenshots with overlaid reference badges. Like Terminal, it is **stateful** and works step by step — the agent loops observe → act → assess until the goal is reached or it escalates to the user.

A session that holds Terminal and Browser at once lets one turn use both: the
model can start a dev server, open the page on the same shared browser and fix
what it sees without you switching anything. It can also attach a capability by
itself — `open_terminal` and `open_browser` are tools, not just UI.

> [!NOTE]
> **Browser downloads Chromium once.** The browser itself is not shipped with Logos (~320 MB download, ~700 MB on disk). The first time you open the Browser panel, the app explains this and downloads Chromium into `%LOCALAPPDATA%\ms-playwright` (`~/.cache/ms-playwright` on Linux); every later session reuses it. Settings → Browser shows the engine status and can start or repeat the download. Point `PLAYWRIGHT_BROWSERS_PATH` at another folder before launching to keep the cache elsewhere — the path is honoured both for the download and at runtime.

### Transcript

Terminal and Browser turns render as one compact activity timeline instead of a stream of separate cards. While the model works it says one short line about each step and, when a task needs it, shows a 3–6 step plan as a `Step N / M` pill above the composer (hover it for the whole checklist) and marks steps as it goes; the pill vanishes when the turn ends and is not restored from the journal. Every tool call becomes a step row with its own status and duration; arguments, written file contents and output expand on click, while errors and denied actions open automatically. Consecutive read-only or action steps fold into one `Ran N tools` line once they finish clean; a thought or note breaks the fold. A background job merges into the step that launched it and keeps an **Open log** link. A finished turn stays as one **Done** block folded down to its header (the answer below stays open), and reopening the session restores the same blocks collapsed from its journal. Chat keeps a plain message stream, and Vision keeps its Computer-use card.

### Supported LLM Providers

Logos supports a wide range of models from leading AI providers. You can easily switch between them in the settings.

> [!NOTE]
> **Models and sessions.** The toolbar model switcher changes the model for the current session and records it in that session's journal. Reopening a session applies its model silently, and a new session starts on the model you last used; Settings → Providers shows and changes that default. If the saved provider or model is no longer available, Logos keeps the current model and says so once.

*   **OpenRouter**
*   **Anthropic**
*   **Google**
*   **OpenAI**
*   **xAI (Grok)**
*   **Hugging Face** (via [Inference Providers](https://huggingface.co/docs/inference-providers) router)
*   **Custom OpenAI- or Anthropic-compatible endpoints** (local servers or self-hosted gateways)

> [!NOTE]
> **Custom providers.** Settings → **Providers** → **Add custom provider…** lets you add any server that speaks the OpenAI Chat Completions API or the Anthropic Messages API — LM Studio, Ollama, llama.cpp, vLLM, DeepSeek, Kimi, GLM, LiteLLM, or a gateway. Pick the API format in the dialog, then enter a provider ID, display name, base URL and the model IDs to expose; the API key and extra headers are optional. Custom providers work in the Chat, Terminal and Browser panels. They are not offered in Vision, which requires a model with native Computer Use support (OpenAI, Anthropic or Google). The same page's **Vision (Computer Use)** row chooses the model Vision uses when the active model cannot drive the screen.

> [!NOTE]
> **Hugging Face billing.** The HF provider routes requests through `router.huggingface.co` to third-party inference partners. Model names use the format `Org/Model` (auto-routing) or `Org/Model:provider` (e.g. `Qwen/Qwen3.8-27B:novita`, `mistralai/Mistral-7B-Instruct-v0.2:featherless-ai`). Routed requests are billed to your Hugging Face account: free accounts receive no monthly credits, so a paid credit balance or PRO subscription is required — otherwise the router answers `402 Payment Required`. The provider is fully wired into the app (streaming, retries, error cards), but free server-side chat is not available.

> **Stay updated!** We constantly monitor for new model releases. You can get the latest supported models by clicking **Update model lists** at the bottom of Settings → Providers. This ensures you always have access to the most modern and powerful LLMs.

> [!NOTE]
> **Reasoning effort.** When the model catalogue publishes effort levels for a model, a badge next to the model picker shows the active level (for example **High**). Click it to choose another level; the choice is stored per provider and model and sent with every request. Models without published levels hide the badge.


### Adding Files to Context

In every panel, you can add file content to the context by simply typing `@` and starting to type the file name. The assistant suggests matching files with a fuzzy, case-sensitive search over the active project; wildcards and glob patterns are not supported. See the [**file search guide**](./docs/file_search_guide.md) for details.

### Context and Memory Settings

As a session grows, Logos keeps the full history on disk and sends the model a trimmed view plus a rolling summary. The composer shows how full the context window is; click the indicator (or the ring in the right panel) to open a **Context window** popover with the token breakdown and a **Compress context** button. See the [**Context settings guide**](./docs/context_settings.md) for what each option in Settings → Context does, its default, and when to change it.

### Skills

Terminal and Browser can follow Agent Skills — instruction packages in the open `SKILL.md` format shared with Claude Code, Codex, Cursor and Qoder. A small built-in set works out of the box; project and user skills are discovered automatically too. Settings → Skills can install more from the public ClawHub catalogue. See the [**skills guide**](./docs/skills.md) for the folder layout, precedence rules and limits.

### MCP servers

Terminal can use tools from [Model Context Protocol](https://modelcontextprotocol.io) servers — local processes or remote Streamable HTTP endpoints that advertise their own tools. Servers are configured in Settings → MCP Servers or in the project's `.mcp.json`, project servers must be trusted before they start, and every call passes the Terminal access policy. See the [**MCP guide**](./docs/mcp.md) for configuration, secrets, permissions, limits and troubleshooting.

### Terminal access permissions

Every Terminal action passes an access policy before it runs. Settings → Terminal → Access permissions selects **Ask every time** (the default: read-only commands and edits inside the project run silently, everything else asks), **Auto approve** (only risky actions ask) or **Full access** (never asks). The same choice is available in the Terminal composer: the chip next to the attachment button shows the current access mode and opens the three options with a short explanation each, so it can be switched without opening Settings; **Full access** is painted red. A denied action appears as a warning card and is reported to the model, which is told not to retry it.

### Workspace panel

The collapsible right panel is always available. **Session** shows context statistics (including the capabilities the session holds) and a recap of the current session; **Activity** lists loaded skills, invoked tools and attached files; **Files** is a read-only tree of the active project; **Git** appears when the session holds the Terminal capability and lists uncommitted changes with line counts. Clicking a file opens it in the detected editor.

### Suggested next actions

After a Terminal, Browser or Vision answer, the assistant can offer up to three clickable next-step suggestions. Clicking one sends it as a normal message; suggestions are never sent on their own, and you can ignore them.

## Features (Tools)

The assistant comes with a set of built-in tools, grouped by the capability
that owns them. A session is granted `base` plus whatever it has attached.

Tools reach the model in two exposures. Most are declared up front, with their
full argument schema. A smaller set — the MCP meta-tools and the browser
inspection/edge-case actions — is only *named* in a short **Tools on demand**
section; the model calls `search_tools` to load one, and it can be called from
the next step on. This keeps the prompt small without hiding anything the
session is allowed to do.

#### Base Tools (every agent session)
*   **Files (read-only)**: `read_files`, `list_directory`, `search_file_content`.
*   **Capabilities**: `open_terminal` and `open_browser` attach the Terminal or Browser capability to the session from inside a turn; `search_tools` loads an on-demand tool.
*   **Control**: `update_plan`, `use_skill`, `communicate_to_user`, `finish`.

#### Vision Tools
Vision does not use a local tool manifest. Actions come from the provider's native Computer Use interface and are executed by Logos:

*   **Pointer**: `click`, `double_click`, `triple_click`, `move`, `drag`, `left_mouse_down`, `left_mouse_up`, `scroll`, `cursor_position`.
*   **Keyboard**: `type`, `keypress`, `hold_key`.
*   **Observation**: `screenshot`, `zoom`.
*   **Control**: `wait`, `launch_app` (starts an app from the allowed Applications list), plus the `ask_user` function tool for questions that need a human answer.

#### Terminal Tools
*   **File System**: `list_directory`, `read_files`, `write_file`, `search_file_content`, `find_files`, `create_directory`, `delete_files`, `move_files`, `copy_files`, `path_exists`.
*   **File Editing**: `replace`, `replace_many`.
*   **System**: `execute_shell` (incl. background jobs), `poll_shell_job`, `stop_shell_job`, `wait_seconds`.
*   **Desktop Capture**: `screenshot` grabs the whole primary display, one window (by part of its title or by PID) or a rectangle given by its top-left and bottom-right corners, saves the PNG under `<project>/.logos/screenshots/` and attaches it to the next turn so the model actually sees the result. Logos hides its own window during a screen or region capture, and `hide_app: false` keeps it in the picture.
*   **Web Tools**: `web_search`, `web_fetchs`, `download_file`.
*   **MCP**: `mcp_list`, `mcp_get`, `mcp_call` for tools exposed by configured MCP servers (see the [MCP guide](./docs/mcp.md)). These are offered on demand.
*   **Skills**: `use_skill` loads an installed Agent Skill on demand (see the [skills guide](./docs/skills.md)).
*   **User Interaction**: `display_message`, `communicate_to_user`.
*   **General**: `finish`, `get_clipboard_content`.

#### Browser Tools
*   **Navigation**: `browser_goto`, `browser_tabs`, `browser_close`.
*   **Observation**: `browser_snapshot` (numbered element refs; controls inside iframes are included and marked `(frame N: host)`), `browser_screenshot` (with ref badges), `browser_read` (page text plus `Frame N` lines for iframes).
*   **Interaction**: `browser_click` (by `ref`, or by `x`/`y` coordinates for canvas, image maps and widgets that have no element), `browser_fill`, `browser_press`, `browser_wait_for`.
*   **On demand**: `browser_navigate_back`, `browser_find`, `browser_frames`, `browser_select_option`, `browser_hover`, `browser_handle_dialog`, and the diagnostics pair `browser_console` (newest console messages and uncaught page errors, filterable by `level`) and `browser_network` (requests with method, URL, status and duration; `only_problems` keeps 4xx/5xx and failed ones). Both print a short tail and point at the full log of the browser session, which is kept in `<project>/.logos/browser_logs/` and read with `read_files` / `search_file_content`. `browser_eval` runs one JavaScript expression in the page or a frame; each call asks the user for approval.
*   **Attachments**: `read_files`, `list_directory`, `search_file_content` (read-only filesystem access for files attached by reference).
*   **Skills**: `use_skill` loads an installed Agent Skill on demand (see the [skills guide](./docs/skills.md)).
*   **User Interaction**: `communicate_to_user`, `finish`.

---


### Known Issues

In the process of working with LLMs, situations may arise where the model returns an incorrectly formatted JSON response. This is most relevant for the agent panels (**Terminal** and **Browser**), which require strictly structured tool calls.

Our application is equipped with an intelligent mechanism for correcting errors in LLM's JSON responses, described in the section [**"Reliable Handling of Structured LLM Responses"**](./docs/system_architecture.md#reliable-handling-of-structured-llm-responses) of the system architecture documentation.

However, if automatic correction fails, you may see a validation error card that names the tool schema, for example:

```
Pydantic validation failed for schema 'terminal_tool_code':
...
```

If this occurs, repeat the request. Often, a retry helps the LLM form a correct response.


For a comprehensive understanding of the project's internal workings and data flows, please refer to the [**System Architecture Overview**](./docs/system_architecture.md).