import logging
import pyperclip

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "get_clipboard_content",
        "description": "Retrieves text from the system clipboard. Note: This tool can only handle text and will return an empty string if the clipboard contains images, files, or other non-textual data.",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of why the clipboard content is being retrieved (e.g., 'Get the copied text to verify it').", "required": True}
        },
        "returns": {
            "type": "STRING",
            "description": "The text content of the clipboard."
        }
    },
    flags={"platform": "any", "mode": "any"}
)
def get_clipboard_content(self, **kwargs):
    try:
        logging.info("Retrieving content from clipboard.")
        content = pyperclip.paste()
        return content
    except Exception as e:
        logging.error(f"Error getting clipboard content: {e}")
        return f"Error: Could not retrieve clipboard content. Details: {e}"
