import logging
import os
import requests

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "download_file",
        "description": "Downloads a file from a given URL and saves it to a specified local path. This is suitable for downloading archives, images, or any other binary or text files directly.",
        "arguments": {
            "url": {"type": "STRING", "description": "The direct URL of the file to download.", "required": True},
            "save_path": {"type": "STRING", "description": "The local path (including filename) where the downloaded file should be saved.", "required": True}
        },
        "returns": {
            "type": "STRING",
            "description": "A success or error message."
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def download_file(self, url: str, save_path: str) -> str:
    """
    Downloads a file from a URL and saves it to a local path.
    """
    if not url or not save_path:
        raise ValueError("Action 'download_file' failed: Missing 'url' or 'save_path' argument.")


    try:
        logging.info(f"Attempting to download file from '{url}' to '{save_path}'")
        response = requests.get(url, stream=True)
        response.raise_for_status()  # Raise an exception for HTTP errors

        # Ensure the directory exists
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return f"Successfully downloaded file from '{url}' to '{save_path}'."

    except requests.exceptions.RequestException as e:
        logging.error(f"Error downloading file from '{url}': {e}")
        return f"Error: Failed to download file. Details: {e}"
    except Exception as e:
        logging.error(f"An unexpected error occurred while saving the file to '{save_path}': {e}")
        return f"Error: An unexpected error occurred: {e}"
