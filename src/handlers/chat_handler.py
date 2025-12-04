"""
Handles the logic for the simple chat mode.
"""
import logging
from states import ControllerState

class ChatHandler:
    def __init__(self, controller):
        self.controller = controller
        self.ui = controller.ui
        self.llm_worker = controller.llm_worker
        self.settings = controller.settings
        self.conversation_history = controller.conversation_history

    def start_chat(self, user_text: str, attachments: list):
        """Starts a new chat interaction."""
        self.controller._set_state(ControllerState.CHATTING)

        self.ui.show_thinking_ui(True, "Thinking")
        self.controller.thinking_time = 0
        self.controller.thinking_timer.start(1000)

        self.controller.llm_request_sent.emit(
            user_text,
            attachments,
            None,  # No system prompt in chat mode
            self.conversation_history,
            None  # No schema for chat mode
        )

    def handle_response_chunk(self, chunk: str):
        """Handles a chunk of a streaming response."""
        self.ui.append_to_stream(chunk)

    def handle_chat_response(self, full_response: str):
        """Handles the final, complete response from the LLM."""
        self.controller.thinking_timer.stop()

        # Construct the final, clean HTML content before changing any UI state
        model_name = self.controller._get_current_model_name()
        final_html = f"<b>{model_name}:</b> {full_response.replace('\n', '<br>')}"

        # Finalize the UI with the complete response FIRST, while is_streaming is still true
        self.ui.finalize_stream(final_html)

        # Now, turn off the thinking indicator
        self.ui.show_thinking_ui(False)

        self.controller._set_state(ControllerState.IDLE)
        self.conversation_history.append({'role': 'assistant', 'content': full_response})
        logging.info("Chat response finished.")

        # If the application window is active, focus the user input field
        if self.ui.main_window.isActiveWindow():
            self.ui.user_input.setFocus()

    def handle_fatal_error(self, error_message: str):
        """
        Handles an unrecoverable error from the LLM after all retries.
        The error_message is expected to be a pre-formatted HTML string
        from the error_formatter.
        """
        logging.error(f"ChatHandler displaying fatal error to user: {error_message}")

        # The error_message is already formatted as user-friendly HTML
        self.ui.add_message(error_message, force_scroll=True)
        self.controller._set_state(ControllerState.IDLE)
