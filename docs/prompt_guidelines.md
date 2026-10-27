# Prompt Guidelines

All prompt files live in `prompts/` and are written in English. Prompts are
loaded lazily and cached for the lifetime of the process, so editing a `.txt`
file takes effect after an application restart.

The active system is the unified agent loop (Terminal, Browser) plus the native
Computer Use loop (Vision). There is no separate strategic-planning /
tactical-execution / evaluation prompt chain anymore.

---

## Active prompt map

### Terminal agent

| File | Role |
| --- | --- |
| `prompts/terminal/system.txt` | Role, agent loop, rules, tool list, response format. |
| `prompts/terminal/user.txt` | Mission, OS, working directory, directory listing, command history. |

Assembled by `Brain.get_terminal_prompts()` in
[`src/brain.py`](../src/brain.py), called from
[`src/handlers/terminal_handler.py`](../src/handlers/terminal_handler.py).

Placeholders:

- system: `{available_tools}`, `{response_format_instructions}`
- user: `{user_request}`, `{os_info}`, `{current_directory}`,
  `{directory_listing_command}`, `{directory_listing}`, `{history_str}`

`{available_tools}` is the generated `cache/terminal/tools.txt`;
`{response_format_instructions}` is the generated instructions + example for
the `terminal_tool_code` schema. Both are produced from the terminal tool
manifest by [`src/computer/tool_registry.py`](../src/computer/tool_registry.py).
Never edit the cache files by hand.

`{current_directory}` and `{directory_listing}` currently receive short
instructional phrases (for example "Use the `dir` command to see the directory
listing"), not actual values. The model obtains real state by running commands.

### Browser agent

| File | Role |
| --- | --- |
| `prompts/browser/system.txt` | Role, agent loop, browser rules, tool list, response format. |
| `prompts/browser/user.txt` | Mission, OS, current URL, command history. |

Assembled by `Brain.get_browser_prompts()`.

Placeholders:

- system: `{available_tools}`, `{response_format_instructions}`
- user: `{user_request}`, `{os_info}`, `{current_url}`, `{history_str}`

`{available_tools}` comes from `cache/browser/tools.txt`; the response format
comes from `cache/browser/response_formats.json` for `browser_tool_code`.

### Vision agent (native Computer Use)

| File | Role |
| --- | --- |
| `prompts/vision/cua_system.txt` | Operating rules for the native computer-use loop. |

Loaded by `VisionHandler._cua_system_prompt()` and sent as the `instructions`
field of a fresh CUA chain. There are no placeholders. The handler appends
runtime safety paragraphs based on the live Vision settings:

- `allow_system_surfaces` — permits Start menu / Windows Search to launch apps;
- `allowed_apps` — the checked Applications list is appended to the prompt and
  becomes the enum of the native `launch_app` function tool;
- `restrict_to_allowed_apps` — limits all interaction (and therefore launches)
  to Vision `allowed_apps`;
- `allow_shell_commands` — permits opening a terminal for read-only ground-truth
  checks; destructive commands still require approval.

Calling `ask_user` for approval is defined by the prompt and the provider tool
contract, not by the settings above.

**Fail closed:** if this file is missing or empty, Vision refuses to start
rather than running without its safety rules.

### Failure assessment notes

| File | When used |
| --- | --- |
| `prompts/common/assessment_general.txt` | Consecutive failures reached `max_consecutive_errors`; the model must assess before another action. |
| `prompts/common/assessment_timeout.txt` | Same, when recent failures include a timeout death. |
| `prompts/terminal/assessment_timeout.txt` | Terminal-specific override for the timeout variant. |

Loaded by `BaseAgentHandler._assessment_note()`. The per-mode file wins over the
common file. The note is appended verbatim to the command history; it contains
no placeholders. Browser uses the common files. Vision has its own pause message
and does not use assessment turns.

### JSON self-correction

| File | Role |
| --- | --- |
| `prompts/correction/system.txt` | Role: fix malformed JSON, output only JSON. |
| `prompts/correction/user.txt` | Original request, expected schema/example, validation error, malformed response. |

Assembled by `Brain.get_self_correction_prompts()` and used by `LLMWorker` after
the first Pydantic validation failure. Placeholders:
`{original_request}`, `{schema_example}`, `{validation_error}`,
`{malformed_response}`.

---

## Legacy prompts (not loaded)

These files remain in the repository but no runtime code path reaches them.
They belong to removed systems and should not be extended:

- `prompts/vision/wait_origin_system.txt`, `wait_origin_user.txt`
- `prompts/vision/wait_check_system.txt`, `wait_check_user.txt`
- `prompts/vision/error_explanation_system.txt`, `error_explanation_user.txt`
- `prompts/terminal/web_search_system.txt`, `web_search_user.txt`
- `prompts/terminal/web_fetch_system.txt`, `web_fetch_user.txt`

`Brain` still exposes getters/builders for the Vision files
(`get_wait_origin_prompts`, `get_wait_check_prompts`,
`get_error_explanation_prompts`) and for the web files
(`get_web_search_prompts`, `get_web_fetch_prompts`), but they have no callers.
The old `wait_element` / plan-evaluation flow and the LLM-summarized web
delegation were replaced by the native CUA loop and the local
`ddgs` + `trafilatura` tools respectively.

---

## Writing rules

1. **English only.** Prompts are part of the model contract and are mixed with
   English schemas and tool descriptions.

2. **System prompt = stable rules.** Define role, loop, constraints, available
   tools, and the exact response format. Do not put per-request data there.

3. **User prompt = dynamic context.** Mission, environment, history, and
   attachments go here. Keep sections clearly labelled and ordered from goal to
   details.

4. **Placeholders use single braces** (`{history_str}`) and are substituted with
   `str.replace()` in `Brain`, never `str.format()`. JSON examples contain
   literal braces, so `format()` would break them.

5. **Do not hardcode tools or schemas.** `{available_tools}` and
   `{response_format_instructions}` are generated from
   `src/computer/modes/<mode>.py` manifests and Pydantic schemas. If a tool is
   added or renamed, the prompt block updates automatically on the next registry
   rebuild.

6. **One action per turn.** Terminal and Browser prompts must require a single
   tool call in a strict JSON envelope. Tool errors are returned to the model as
   text so it can correct itself; the prompt should tell it to read the error
   before retrying.

7. **No stale architecture terms.** Do not reintroduce "strategic plan",
   "tactical step", "evaluation", `[DONE]` markers, or screenshot-annotation
   instructions. The current loops are: plan → act → observe → repeat for
   Terminal/Browser, and native provider turn → execute → screenshot → repeat
   for Vision.

8. **Assessment notes are facts, not orders.** They tell the model to assess
   significance against the user's goal and then communicate; they do not
   prescribe a specific next tool.

9. **Missing-file behavior matters.** Terminal/Browser prompt builders return
   `(None, None)` when a required file is missing, and the handler reports an
   error. Vision refuses to start. Keep that behavior in mind when renaming
   files.

10. **Caching.** `Brain._get_cached_prompt()` and the CUA system-prompt cache
    keep the first read for the whole process. Restart after editing.
