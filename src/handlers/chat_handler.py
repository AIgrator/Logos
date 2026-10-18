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
        self._chat_seq = 0  # per-message counter for log correlation
        # Model that started the current reply, so a mid-stream model switch
        # never relabels an in-flight answer with the new model's name.
        self._active_actor = None

    def start_chat(self, user_text: str, attachments: list):
        """Starts a new chat interaction."""
        if self.controller.thinking_timer.isActive():
            # New message while generating: cancel the in-flight request.
            # The worker runs queued slots on one thread, so it finishes the
            # cancelled call first and then picks up the new one; a stale
            # chunk is wiped by the thinking-line reset on first new chunk.
            if self.controller.llm_worker:
                self.controller.llm_worker.cancel()
            logging.info("New chat message during generation; previous request cancelled.")
        self.controller._set_state(ControllerState.CHATTING)

        self._active_actor = self.controller._get_current_model_name()
        self.ui.show_thinking_ui(True, "Thinking", actor=self._active_actor)
        self.controller.thinking_time = 0
        self.controller.thinking_timer.start(1000)

        self._chat_seq += 1
        self.controller._send_llm_request(
            user_text,
            attachments,
            None,  # No system prompt in chat mode
            self.conversation_history,
            None,  # No schema for chat mode
            "chat",
            self._chat_seq
        )

    def handle_response_chunk(self, chunk: str):
        """Handles a chunk of a streaming response."""
        self.ui.append_to_stream(chunk)

    def handle_chat_response(self, full_response: str):
        """Handles the final, complete response from the LLM."""
        self.controller.thinking_timer.stop()

        # Construct the final, clean HTML content before changing any UI state.
        # Model prose may be Markdown — render it (headings, lists, bold, code)
        # instead of dumping the raw syntax into the chat.
        from src.ui.text_render import render_text
        model_name = self._active_actor or self.controller._get_current_model_name()
        final_html = render_text(
            full_response, prefix_html=f"<b>{model_name}:</b> ", markdown_ok=True)

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

        # Answer the next queued message, if the user typed while we streamed.
        self.controller._drain_chat_queue()

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

        # Even after a fatal error, keep the queue draining (don't wedge it).
        self.controller._drain_chat_queue()
