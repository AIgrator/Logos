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

If you prefer to install dependencies manually, follow these steps:

1. **PyTorch with CUDA (if your GPU supports it):**
   ```bash
   uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
   ```

2. **Other dependencies:**
   ```bash
   uv pip install -r requirements.txt
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

**Step 2: Select OCR Languages**
Choose the languages the assistant will use to recognize text on the screen. Immediately after, the application will automatically download all necessary neural network models. This may take a few minutes.

> [!NOTE]
> Some language models are incompatible with each other. To learn more about how to correctly select languages, read [**this guide**](./docs/easyocr_language_model_guide.md).

**Step 3: Final Setup and Model Download**
In the final step, you can choose additional integration options, such as:
* 1. Creating a global `logos` command.
* 2. Creating a desktop shortcut. 
* 3. Adding an item to the context menu to open a folder in the application's context, which is convenient for terminal mode.

## Launching the Application

To run the application, use the following command:
```bash
python main.py
```

If you installed using the setup script (setup_windows.ps1 or setup_ubuntu.sh), a shorter command becomes available:
```bash
logos
```

### Working in a Specific Directory (Terminal Mode)

The program accepts a folder path as an argument, which becomes the working directory for the terminal mode. All file operations and shell commands are executed relative to this folder.

You can specify the working directory for the **terminal mode** by passing the folder path as an argument. All file operations and shell commands will be executed relative to this folder.

If you have integrated the application with the Windows context menu (an option available during the first setup), you can right-click any folder and select "Open with Logos" to launch the assistant in that directory.

**Linux/macOS:**
```bash
python main.py /home/user/my_project
```

**Windows:**
```powershell
python main.py C:\Users\User\Documents\my_project
```
If no path is specified, the working directory will be the one from which the application was launched.

### Modes of Operation

The application has three main modes, each designed for different types of tasks:

*   **Chat Mode:** A simple chat with the LLM. You can converse and send files for analysis. The LLM can independently perform web searches or fetch page context if it deems it necessary. This is a **streaming** mode, where responses are generated in real-time.

*   **Vision Mode:** In this mode, the assistant uses computer vision models to "see" the screen, understand the graphical user interface, and interact with it using the mouse and keyboard. This is a **stateful** mode that operates based on a **plan**. Before executing a task, the LLM creates a step-by-step plan that you can review, edit, and approve. It is ideal for automating tasks in GUI applications.

*   **Terminal Mode:** In this mode, the assistant acts as an AI agent in the command line. This is also a **stateful** mode, but it operates **without a predefined plan**. The LLM autonomously determines the next step, executes the command, and analyzes the result, iteratively moving towards the goal. It does not have access to the visual interface but can execute shell commands, work with the file system, run scripts, and interact with web resources. Long-running commands can be launched as background jobs (`run_in_background`, `poll_shell_job` / `stop_shell_job` / `wait_seconds`). This mode is designed for tasks related to development, file management, and console automation.

*   **Browser Mode:** In this mode, the assistant drives a real Chromium browser via accessibility snapshots: it sees numbered element references (`browser_snapshot`), clicks/fills/presses them, reads pages, manages tabs, and takes screenshots with overlaid reference badges. Like Terminal Mode, it is **stateful** and plan-free — the agent loops observe → act → assess until the goal is reached or it escalates to the user.

### Supported LLM Providers

Logos supports a wide range of models from leading AI providers. You can easily switch between them in the settings.

*   **OpenRouter**
*   **Anthropic**
*   **Google**
*   **OpenAI**
*   **xAI (Grok)**
*   **Hugging Face** (via [Inference Providers](https://huggingface.co/docs/inference-providers) router)

> [!NOTE]
> **Hugging Face billing.** The HF provider routes requests through `router.huggingface.co` to third-party inference partners. Model names use the format `Org/Model` (auto-routing) or `Org/Model:provider` (e.g. `Qwen/Qwen3.8-27B:novita`, `mistralai/Mistral-7B-Instruct-v0.2:featherless-ai`). Routed requests are billed to your Hugging Face account: free accounts receive no monthly credits, so a paid credit balance or PRO subscription is required — otherwise the router answers `402 Payment Required`. The provider is fully wired into the app (streaming, retries, error cards), but free server-side chat is not available.

> **Stay updated!** We constantly monitor for new model releases. You can get the latest supported models by simply clicking the **"Update models"** button in the settings window. This ensures you always have access to the most modern and powerful LLMs.


### Adding Files to Context

In all modes, you can add file content to the context by simply typing `@` and starting to type the file name. The assistant will suggest matching files. For more advanced searches using special characters, refer to the [**file search guide**](./docs/file_search_guide.md).

## Features (Tools)

The assistant comes with a set of built-in tools, grouped by their mode of operation:

#### Chat Mode Tools
*   `web_search`, `web_fetchs` (internet search and page context retrieval).

#### Vision Mode Tools
*   **Mouse**: `click`, `double_click`, `drag_and_drop`, `scroll`, `scroll_page`.
*   **Keyboard**: `write_text`, `press_key`, `hotkey`, `key_down`, `key_up`.
*   **System**: `get_clipboard_content`, `launch_app`, `wait_element`, `execute_shell`, `finish`.
*   **File System (read/write)**: `list_directory`, `read_files`, `write_file`.
*   **User Interaction**: `communicate_to_user`, `display_message`, `show_warning`.

#### Terminal Mode Tools
*   **File System**: `list_directory`, `read_files`, `write_file`, `search_file_content`, `find_files`, `create_directory`, `delete_files`, `move_files`, `copy_files`, `path_exists`.
*   **File Editing**: `replace`, `replace_many`.
*   **System**: `execute_shell` (incl. background jobs), `poll_shell_job`, `stop_shell_job`, `wait_seconds`.
*   **Web Tools**: `web_search`, `web_fetchs`, `download_file`.
*   **User Interaction**: `display_message`, `communicate_to_user`.
*   **General**: `finish`, `get_clipboard_content`.

#### Browser Mode Tools
*   **Navigation**: `browser_goto`, `browser_navigate_back`, `browser_tabs`, `browser_close`.
*   **Observation**: `browser_snapshot` (numbered element refs), `browser_screenshot` (with ref badges), `browser_read`, `browser_find`.
*   **Interaction**: `browser_click`, `browser_fill`, `browser_select_option`, `browser_hover`, `browser_press`, `browser_handle_dialog`, `browser_wait_for`.
*   **User Interaction**: `communicate_to_user`, `finish`.

---


### Known Issues

In the process of working with LLMs, situations may arise where the model returns an incorrectly formatted JSON response, which is especially relevant for **Vision Mode**, where strictly structured data is required for plan building and execution.

Our application is equipped with an intelligent mechanism for correcting errors in LLM's JSON responses, described in the section [**"Reliable Handling of Structured LLM Responses"**](./docs/system_architecture.md#reliable-handling-of-structured-llm-responses) of the system architecture documentation.

However, if automatic correction fails, you may see an error message similar to the following:

```
Pydantic validation failed for schema 'strategic_plan':
1 validation error for Evaluation
payload.tasks
  list[dict] is not a valid list
```

If this occurs, try repeating your request or task for plan creation. Often, a retry helps the LLM form a correct response.


For a comprehensive understanding of the project's internal workings, data flows, and advanced concepts like self-correction mechanisms, please refer to the [**System Architecture Overview**](./docs/system_architecture.md).