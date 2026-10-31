# Logos AI Assistant

This is a desktop assistant application that uses Large Language Models (LLMs) to perform user tasks on a computer. The application operates in four main modes: **Chat Mode**, **Vision Mode**, **Terminal Mode**, and **Browser Mode**.

For detailed technical information about the system architecture, please refer to the [**System Architecture Overview for Developers**](./docs/system_architecture.md).

## Installation

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
* 3. Adding an item to the context menu to open a folder in the application's context, which is convenient for terminal mode.

The Windows installer offers the same integrations as tasks (checked by default), so an installed build goes straight from the provider step to the application.

## Launching the Application

To run the application, use the following command:
```bash
python main.py
```

If you installed using the setup script (setup_windows.ps1 or setup_ubuntu.sh), a shorter command becomes available:
```bash
logos
```

### Working in a Specific Directory

The program accepts a folder path as an argument and opens that folder as the active project. The project root is the working directory for **Terminal** and **Browser** modes, the root for `@`-file search and indexing, and the place where project data is stored in `.logos/`.

If you have integrated the application with the Windows context menu (an option during installation or in the first setup of a source checkout), you can right-click any folder and select "Open with Logos" to launch the assistant in that directory.

**Linux/macOS:**
```bash
python main.py /home/user/my_project
```

**Windows:**
```powershell
python main.py C:\Users\User\Documents\my_project
```
If no path is specified, the remembered active project is used. When there is no usable remembered project, the full-screen **Home** page opens and lets you pick or add one.

### Modes of Operation

The application has four main modes, each designed for different types of tasks:

*   **Chat Mode:** A simple chat with the LLM. You can converse and send files for analysis. When the selected provider supports native web search, the model can search the web on its own during the response; Chat does not run a local tool loop. This is a **streaming** mode, where responses are generated in real-time.

*   **Vision Mode:** In this mode, the assistant uses the selected provider's native **Computer Use** capability to see the screen and interact with it using the mouse and keyboard. It runs on the same observe → act → assess agent loop as Terminal and Browser mode and requires a CUA-capable provider (OpenAI, Anthropic, or Google). It is ideal for automating tasks in GUI applications.

*   **Terminal Mode:** In this mode, the assistant acts as an AI agent in the command line. This is also a **stateful** mode; no plan is fixed in advance, but for multi-step work the assistant keeps a short checklist of its own (`update_plan`), shown as a compact `Step N / M` pill above the composer that reveals the full plan on hover and updates as it goes. The LLM autonomously determines the next step, executes the command, and analyzes the result, iteratively moving towards the goal. It does not have access to the visual interface but can execute shell commands, work with the file system, run scripts, and interact with web resources. Long-running commands can be launched as background jobs (`run_in_background`, `poll_shell_job` / `stop_shell_job` / `wait_seconds`). This mode is designed for tasks related to development, file management, and console automation.

*   **Browser Mode:** In this mode, the assistant drives a real Chromium browser via accessibility snapshots: it sees numbered element references (`browser_snapshot`), clicks/fills/presses them, reads pages, manages tabs, and takes screenshots with overlaid reference badges. Like Terminal Mode, it is **stateful** and works step by step — the agent loops observe → act → assess until the goal is reached or it escalates to the user.

### Transcript

Terminal and Browser turns render as one compact activity timeline instead of a stream of separate cards. While the model works it says one short line about each step and, when a task needs it, shows a 3–6 step plan as a `Step N / M` pill above the composer (hover it for the whole checklist) and marks steps as it goes; the pill vanishes when the turn ends and is not restored from the journal. Every tool call becomes a row with its own status and duration; arguments, written file contents and output expand on click, while errors and denied actions open automatically. A background job merges into the step that launched it and keeps an **Open log** link. A finished turn stays as one **Done** block with the total elapsed time, and reopening the session restores the same blocks from its journal. Chat keeps a plain message stream, and Vision keeps its Computer-use card.

### Supported LLM Providers

Logos supports a wide range of models from leading AI providers. You can easily switch between them in the settings.

*   **OpenRouter**
*   **Anthropic**
*   **Google**
*   **OpenAI**
*   **xAI (Grok)**
*   **Hugging Face** (via [Inference Providers](https://huggingface.co/docs/inference-providers) router)
*   **Custom OpenAI- or Anthropic-compatible endpoints** (local servers or self-hosted gateways)

> [!NOTE]
> **Custom providers.** Settings → **Providers** → **Add custom provider…** lets you add any server that speaks the OpenAI Chat Completions API or the Anthropic Messages API — LM Studio, Ollama, llama.cpp, vLLM, DeepSeek, Kimi, GLM, LiteLLM, or a gateway. Pick the API format in the dialog, then enter a provider ID, display name, base URL and the model IDs to expose; the API key and extra headers are optional. Custom providers work in Chat, Terminal and Browser modes. They are not offered in Vision mode, which requires a model with native Computer Use support (OpenAI, Anthropic or Google). The same page's **Vision (Computer Use)** row chooses the model Vision mode switches to when the active model cannot drive the screen.

> [!NOTE]
> **Hugging Face billing.** The HF provider routes requests through `router.huggingface.co` to third-party inference partners. Model names use the format `Org/Model` (auto-routing) or `Org/Model:provider` (e.g. `Qwen/Qwen3.8-27B:novita`, `mistralai/Mistral-7B-Instruct-v0.2:featherless-ai`). Routed requests are billed to your Hugging Face account: free accounts receive no monthly credits, so a paid credit balance or PRO subscription is required — otherwise the router answers `402 Payment Required`. The provider is fully wired into the app (streaming, retries, error cards), but free server-side chat is not available.

> **Stay updated!** We constantly monitor for new model releases. You can get the latest supported models by clicking **Update model lists** at the bottom of Settings → Providers. This ensures you always have access to the most modern and powerful LLMs.

> [!NOTE]
> **Reasoning effort.** When the model catalogue publishes effort levels for a model, a badge next to the model picker shows the active level (for example **High**). Click it to choose another level; the choice is stored per provider and model and sent with every request. Models without published levels hide the badge.


### Adding Files to Context

In all modes, you can add file content to the context by simply typing `@` and starting to type the file name. The assistant suggests matching files with a fuzzy, case-sensitive search over the active project; wildcards and glob patterns are not supported. See the [**file search guide**](./docs/file_search_guide.md) for details.

### Context and Memory Settings

As a session grows, Logos keeps the full history on disk and sends the model a trimmed view plus a rolling summary. The composer shows how full the context window is; click the indicator (or the ring in the right panel) to open a **Context window** popover with the token breakdown and a **Compress context** button. See the [**Context settings guide**](./docs/context_settings.md) for what each option in Settings → Context does, its default, and when to change it.

### Skills

Terminal and Browser can follow Agent Skills — instruction packages in the open `SKILL.md` format shared with Claude Code, Codex, Cursor and Qoder. A small built-in set works out of the box; project and user skills are discovered automatically too. Settings → Skills can install more from the public ClawHub catalogue. See the [**skills guide**](./docs/skills.md) for the folder layout, precedence rules and limits.

### MCP servers

Terminal can use tools from [Model Context Protocol](https://modelcontextprotocol.io) servers — local processes or remote Streamable HTTP endpoints that advertise their own tools. Servers are configured in Settings → MCP Servers or in the project's `.mcp.json`, project servers must be trusted before they start, and every call passes the Terminal access policy. See the [**MCP guide**](./docs/mcp.md) for configuration, secrets, permissions, limits and troubleshooting.

### Terminal access permissions

Every Terminal action passes an access policy before it runs. Settings → Terminal → Access permissions selects **Ask every time** (the default: read-only commands and edits inside the project run silently, everything else asks), **Auto approve** (only risky actions ask) or **Full access** (never asks). The same choice is available in the Terminal composer: the chip next to the attachment button shows the current mode and opens the three options with a short explanation each, so it can be switched without opening Settings; **Full access** is painted red. A denied action appears as a warning card and is reported to the model, which is told not to retry it.

### Workspace panel

The collapsible right panel is available in every mode. **Session** shows context statistics and a recap of the current session; **Activity** lists loaded skills, invoked tools and attached files; **Files** is a read-only tree of the active project; **Git** appears in Terminal mode and lists uncommitted changes with line counts. Clicking a file opens it in the detected editor.

### Suggested next actions

After a Terminal, Browser or Vision answer, the assistant can offer up to three clickable next-step suggestions. Clicking one sends it as a normal message; suggestions are never sent on their own, and you can ignore them.

## Features (Tools)

The assistant comes with a set of built-in tools, grouped by their mode of operation:

#### Chat Mode Tools
Chat mode does not execute client-side tools. Web search, when available, is performed natively by the provider inside the response stream.

#### Vision Mode Tools
Vision mode does not use a local tool manifest. Actions come from the provider's native Computer Use interface and are executed by Logos:

*   **Pointer**: `click`, `double_click`, `triple_click`, `move`, `drag`, `left_mouse_down`, `left_mouse_up`, `scroll`, `cursor_position`.
*   **Keyboard**: `type`, `keypress`, `hold_key`.
*   **Observation**: `screenshot`, `zoom`.
*   **Control**: `wait`, `launch_app` (starts an app from the allowed Applications list), plus the `ask_user` function tool for questions that need a human answer.

#### Terminal Mode Tools
*   **File System**: `list_directory`, `read_files`, `write_file`, `search_file_content`, `find_files`, `create_directory`, `delete_files`, `move_files`, `copy_files`, `path_exists`.
*   **File Editing**: `replace`, `replace_many`.
*   **System**: `execute_shell` (incl. background jobs), `poll_shell_job`, `stop_shell_job`, `wait_seconds`.
*   **Web Tools**: `web_search`, `web_fetchs`, `download_file`.
*   **MCP**: `mcp_list`, `mcp_get`, `mcp_call` for tools exposed by configured MCP servers (see the [MCP guide](./docs/mcp.md)).
*   **Skills**: `use_skill` loads an installed Agent Skill on demand (see the [skills guide](./docs/skills.md)).
*   **User Interaction**: `display_message`, `communicate_to_user`.
*   **General**: `finish`, `get_clipboard_content`.

#### Browser Mode Tools
*   **Navigation**: `browser_goto`, `browser_navigate_back`, `browser_tabs`, `browser_close`.
*   **Observation**: `browser_snapshot` (numbered element refs), `browser_screenshot` (with ref badges), `browser_read`, `browser_find`.
*   **Interaction**: `browser_click`, `browser_fill`, `browser_select_option`, `browser_hover`, `browser_press`, `browser_handle_dialog`, `browser_wait_for`.
*   **Attachments**: `read_files`, `list_directory`, `search_file_content` (read-only filesystem access for files attached by reference).
*   **Skills**: `use_skill` loads an installed Agent Skill on demand (see the [skills guide](./docs/skills.md)).
*   **User Interaction**: `communicate_to_user`, `finish`.

---


### Known Issues

In the process of working with LLMs, situations may arise where the model returns an incorrectly formatted JSON response. This is most relevant for the agent modes (**Terminal** and **Browser**), which require strictly structured tool calls.

Our application is equipped with an intelligent mechanism for correcting errors in LLM's JSON responses, described in the section [**"Reliable Handling of Structured LLM Responses"**](./docs/system_architecture.md#reliable-handling-of-structured-llm-responses) of the system architecture documentation.

However, if automatic correction fails, you may see a validation error card that names the tool schema, for example:

```
Pydantic validation failed for schema 'terminal_tool_code':
...
```

If this occurs, repeat the request. Often, a retry helps the LLM form a correct response.


For a comprehensive understanding of the project's internal workings and data flows, please refer to the [**System Architecture Overview**](./docs/system_architecture.md).