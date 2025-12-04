Overall Concept: Hybrid Search Model ("Plan A" / "Plan B")

We have implemented a "hybrid" model for performing web searches. The main idea is to always try to use the most efficient and integrated search method, but to have a reliable backup option.

This works on the "Plan A / Plan B" principle:

*   Plan A (Preferred): Use the native search built into the LLM provider itself. If a provider, like Google Gemini, has its own built-in search function, this is the best option, as it is optimized to work with the model.
*   Plan B (Fallback): If the provider does not have built-in search, or if "Plan A" fails for some reason, the system automatically switches to local search. This search is performed by our application directly via ddgs, using the key we added to the settings.

Now let's break down how this works step-by-step using Google as an example.

---

Step-by-step Process of Executing `web_search` with Google Provider

1.  Search Initiation (LLM)
    *   The agent in terminal mode decides it needs information from the internet.
    *   It forms and calls the `web_search` tool, for example: `web_search(query="current Google stock price")`.

2.  Interception and Orchestration (`TerminalHandler`)
    *   The tool call is intercepted in the `@src/handlers/terminal_handler.py` file by the `_execute_action` method.
    *   This method sees that `web_search` is being called, and instead of calling it directly, it passes control to a special method `_handle_web_request`. This method is our "orchestrator" for the hybrid model.

3.  Executing "Plan A": Native Google Search (`GoogleProvider`)
    *   `_handle_web_request` determines which LLM provider is currently active. In our case, it's `GoogleProvider`.
    *   It attempts to execute Plan A by calling `provider.web_search(query=...)`.
    *   Now let's look at what happens inside `@src/llm/providers/google_provider.py`:
        *   Unlike other providers, `GoogleProvider` has its own implementation of the `web_search` method.
        *   This method uses the built-in search function of the Gemini API. It creates a special tool `genai.Tool(google_search_retrieval=genai.GoogleSearchRetrieval())`.
        *   It sends a request to Google, which performs the search and immediately uses the found information to generate a response to the original query.
        *   If everything is successful, `GoogleProvider.web_search` returns a ready, coherent text response.

4.  Handling a Successful "Plan A"
    *   `_handle_web_request` in `TerminalHandler` receives the text response from `GoogleProvider`.
    *   It sees that "Plan A" worked and does not proceed to "Plan B".
    *   The search result is displayed to the user in the interface.
    *   Most importantly: this result is also added to the command history as **Tool Result:**, so that in the next step, the LLM knows what information it received and can continue its task.

---

What Happens if "Plan A" Fails?

Now imagine that we are using another provider that does not have built-in search, or the Google search API is temporarily unavailable.

1.  "Plan A" Failure
    *   `_handle_web_request` calls `provider.web_search(...)`.
    *   The base class `@src/llm/base_provider.py` implements this method so that it simply returns `None`. If a provider (e.g., Anthropic) has not overridden this method, the call will return `None`.
    *   Alternatively, if `GoogleProvider.web_search` returns an error, `_handle_web_request` will catch it.
    *   In both cases, `_handle_web_request` will understand that "Plan A" failed.

2.  Executing "Plan B": ddgs Fallback Search (`Computer`)
    *   `_handle_web_request` proceeds to execute Plan B.
    *   It calls `self.computer.web_search(query=original_prompt)`. Note that no API key is passed here.
    *   Now control is transferred to our refactored code in `@src/computer/computer.py`:
        *   The `web_search` method within `Computer` is now responsible for getting the key itself.
        *   It accesses `self.settings_manager`, which we passed in the constructor.
        *   It requests the `ddgs_api_key` setting from `settings.json`.
        *   Upon receiving the key, it performs a direct HTTP request to ddgs and returns the result.

3.  Handling a Successful "Plan B"
    *   `_handle_web_request` receives the result from `self.computer.web_search` and processes it in exactly the same way as a successful "Plan A": it shows it to the user and passes it into the context for the LLM.

Conclusion

Thus, we have built a flexible system:

*   For Google: We use its powerful built-in search, which is faster and more efficient.
*   For all other providers (and as a fallback for Google): We use our own search via ddgs. Thanks to refactoring, this search is now self-sufficient—it retrieves the API key from the settings itself, and `TerminalHandler` does not need to worry about where to get it. This makes the code cleaner and easier to extend in the future.