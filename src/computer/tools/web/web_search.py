import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "web_search",
        "description": "Performs a web search using DuckDuckGo Search to find contextually relevant information for an LLM.",
        "arguments": {
            "query": {"type": "STRING", "description": "The search query.", "required": True}
        },
        "returns": {
            "type": "STRING",
            "description": "The summarized answer from DuckDuckGo Search."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def web_search(self, query: str) -> str:
    """
    Performs a web search using DuckDuckGo Search, fetches content, and summarizes it.
    """
    if not query:
        raise ValueError("Action 'web_search' failed: Missing required 'query' argument.")

    # Import heavy libraries inside the function for lazy loading
    import nltk
    import trafilatura
    from ddgs import DDGS
    from sumy.nlp.stemmers import Stemmer
    from sumy.nlp.tokenizers import Tokenizer
    from sumy.parsers.plaintext import PlaintextParser
    from sumy.summarizers.lsa import LsaSummarizer
    from sumy.utils import get_stop_words

    # Silence verbose logging from underlying libraries to reduce noise
    logging.getLogger('trafilatura').setLevel(logging.WARNING)
    logging.getLogger('ddgs').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)

    try:
        # Ensure the 'punkt' tokenizer data is available for summarization.
        nltk.data.find('tokenizers/punkt')
    except LookupError:
        logging.info("NLTK 'punkt' tokenizer not found. Downloading...")
        nltk.download('punkt')

    logging.info(f"Performing web search for: '{query}'")

    try:
        with DDGS() as ddgs:
            # Fetch search results
            results = ddgs.text(query, region='wt-wt', safesearch='moderate', timelimit='y')

            if not results:
                logging.warning(f"Web search for '{query}' returned no results.")
                return "Web search did not return any results."

            logging.info(f"Found {len(results)} results. Processing top 5...")
            summarized_content = []
            for i, result in enumerate(results):
                if i >= 5: # Limit to top 5 results
                    break

                url = result.get('href')
                if not url:
                    continue

                try:
                    downloaded = trafilatura.fetch_url(url)
                    if downloaded:
                        content = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                        if content:
                            logging.info(f"Content successfully retrieved from: {url}")
                            parser = PlaintextParser.from_string(content, Tokenizer("english"))
                            stemmer = Stemmer("english")
                            summarizer = LsaSummarizer(stemmer)
                            summarizer.stop_words = get_stop_words("english")

                            summary = summarizer(parser.document, 10)  # Summarize to 10 sentences
                            summary_text = " ".join(str(sentence) for sentence in summary)
                            summarized_content.append(summary_text)
                        else:
                            logging.warning(f"Could not extract readable content from: {url}")
                    else:
                        logging.warning(f"Failed to retrieve content from: {url}")
                except Exception as e:
                    logging.error(f"An error occurred while processing the page at {url}: {e}", exc_info=True)

            if not summarized_content:
                return "Could not retrieve or summarize content from the web search results."

            return "\n\n---\n\n".join(summarized_content)

    except Exception as e:
        logging.error(f"An error occurred during the web search for '{query}': {e}")
        raise RuntimeError(f"Failed to execute web search: {e}") from e
