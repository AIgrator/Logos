# Hybrid Web Search (Terminal Mode)

Terminal web access uses two tools and a "Plan A / Plan B" strategy:

- `web_search(query)` — try the active provider's **native** search first, then
  fall back to the app's **local** search stack.
- `web_fetchs(urls)` — always local: download and extract readable text from a
  list of URLs.

There is no web-search API key in Settings. The local fallback uses the `ddgs`
package directly and needs no credentials.

This hybrid applies to **Terminal mode** (any session that holds the terminal
capability). Chat has no tool loop: a chat turn can only use the provider's own
server-side search, and only OpenAI, Anthropic and xAI offer it (Google does
not). Browser mode navigates pages with Playwright. None of them goes through
`WebTaskThread`.

---

## Execution path

1. The terminal agent emits a tool call such as
   `{"type": "terminal_tool_code", "payload": {"name": "web_search", "arguments": {"query": "..."}}}`.
2. [`TerminalHandler._execute_action()`](../src/handlers/terminal_handler.py)
   intercepts `web_search` / `web_fetchs` before the normal executor and calls
   `_handle_web_request()`.
3. `_handle_web_request()` cancels any previous web thread, starts a new
   `WebTaskThread` (a `QThread`), and shows the "Accessing web..." timer. The UI
   thread is never blocked.
4. The thread emits through `ToolExecRelay`:
   - `web_plan_a_failed(provider_name)` — shows "Native search with X failed.
     Trying local fallback...";
   - `web_finished(action_name, result, args)`;
   - `web_error(action_name, error, args)`.
5. On success, the result is shown as a collapsible "Web Search Results" /
   "Web Content" block, logged, appended to command history as
   `**Tool Result:**`, and the agent loop continues.

Starting a new web request while one is running cancels the old thread. The
result of the new request is the only one delivered.

---

## Plan A — native provider search

`WebTaskThread` calls `provider.web_search(query=...)`. The base
implementation in [`src/llm/base_provider.py`](../src/llm/base_provider.py)
returns `None`, which means "not supported" and sends the request to Plan B.

| Provider | Native implementation |
| --- | --- |
| OpenAI | Uses the dedicated `gpt-5-search-api` Chat Completions model, regardless of the selected chat model. |
| Anthropic | Server tools `web_search_20250305` (max 3 uses) and `web_fetch_20250910` (max 5 uses, citations enabled), with the `web-fetch-2025-09-10` beta header. Uses the selected model. |
| Google | `genai_types.Tool(google_search=genai_types.GoogleSearch())` with the selected Gemini model. Grounding requires a paid key; a free key returns `429 RESOURCE_EXHAUSTED`. |
| OpenRouter | Appends `:online` to the selected model id, enabling the provider's web plugin. |
| xAI | Direct HTTP request with `search_parameters.data_sources = ["web"]` (Live Search); citations are appended to the returned text. xAI announced Live Search deprecation for 2025-12-15, so treat this path as at risk. |
| Hugging Face | No override; base returns `None`. |

Plan A is considered successful only when the provider returns a non-empty
string. Any exception — including `FatalError` such as quota exhaustion — is
caught by `WebTaskThread`, reported in the UI, and followed by Plan B. A
provider that wants to opt out should return `None` rather than raise.

Provider `web_search` implementations must classify SDK errors as
`FatalError` / `RetryableError` like any other provider call, but the web thread
does not retry Plan A; it moves to the local fallback.

---

## Plan B — local search

Implemented in
[`src/computer/tools/web/web_search.py`](../src/computer/tools/web/web_search.py):

1. `ddgs.DDGS().text(query, region='wt-wt', safesearch='moderate', timelimit='y')`.
2. Take the first 5 results.
3. Fetch each page with `trafilatura.fetch_url()` and extract the main text
   (`include_comments=False`, `include_tables=False`).
4. Summarize each page to 10 sentences with sumy's LSA summarizer
   (English tokenizer and stop words).
5. Join the summaries with `---`.

The result is plain text: search snippets are not passed through; only pages
that were fetched and extracted contribute content. If no page can be
processed, the tool returns a short "could not retrieve" message.

Runtime dependencies: `ddgs`, `trafilatura`, `sumy`, `nltk`. The code downloads
the NLTK `punkt` tokenizer on first use if it is missing. The heavy imports are
inside the function, so Terminal startup does not pay for them.

If Plan B itself fails, the UI shows "Local fallback for ... also failed", the
error is recorded as a tool result, and the agent receives it as the next
observation.

---

## `web_fetchs`

`web_fetchs` never tries a provider. It is implemented in
[`src/computer/tools/web/web_fetchs.py`](../src/computer/tools/web/web_fetchs.py):
for each URL, `trafilatura.fetch_url()` + `trafilatura.extract()`, then join
non-empty results with `---`. It rejects a missing or non-list `urls` argument.

Use it when the model already has URLs from search results, documentation, or
the user.

---

## Adding native search to a provider

Override `web_search(self, query: str) -> str | None` in the provider module:

- Return synthesized text on success.
- Return `None` (or an empty string) when the provider has no native search or
  produced nothing; the thread will use Plan B.
- Raise `FatalError` / `RetryableError` on API failures. The web thread catches
  them and falls back, but the classification still feeds the shared error
  logging and any future caller.
- Do not add UI code or log to files; the provider base wrapper journals calls.

No settings change is required for Plan B. Do not add a `ddgs_api_key` setting:
the current implementation does not read one.
