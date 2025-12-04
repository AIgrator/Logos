import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "web_fetchs",
        "description": "Downloads and extracts the main content from a list of URLs.",
        "arguments": {
            "urls": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "A list of URLs to download and extract content from.",
                "required": True
            }
        },
        "returns": {
            "type": "STRING",
            "description": "The concatenated clean text content from all provided URLs."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def web_fetchs(self, urls: list) -> str:
    """
    Fetches and cleans content from a list of URLs.
    """
    if not urls or not isinstance(urls, list):
        raise ValueError("Action 'web_fetchs' failed: Missing or invalid 'urls' argument. It must be a non-empty list of strings.")

    # Import trafilatura inside the function for lazy loading
    import trafilatura
    # Silence trafilatura's verbose logging to reduce noise in the terminal
    logging.getLogger('trafilatura').setLevel(logging.WARNING)

    logging.info(f"Performing web fetch for URLs: {urls}")

    try:
        all_content = []
        for url in urls:
            logging.info(f"Downloading and extracting content from: {url}")
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                content = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                if content:
                    all_content.append(content)

        if not all_content:
            return "Error: Could not retrieve any content from the provided URL(s)."

        return "\n\n---\n\n".join(all_content)

    except Exception as e:
        logging.error(f"Error during web_fetchs for URLs '{urls}': {e}")
        raise RuntimeError(f"Failed to fetch or process URL(s): {e}") from e
