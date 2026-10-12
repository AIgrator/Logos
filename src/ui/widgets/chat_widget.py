# ui/chat_widget.py
import logging
import sys
import uuid

from PySide6.QtCore import Qt, QPoint, QRect, QTime, QTimer, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QKeySequence,
    QPainter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QMenu

from src.ui.widgets.collapsible_block_formatter import CollapsibleBlockFormatter

# --- Basic Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
    stream=sys.stdout
)
# ---

class ChatWidget(QAbstractScrollArea):
    """
    A custom chat widget for displaying messages with controlled scrolling
    and support for text selection and copying via context menu.
    Includes autoscroll functionality during text selection.
    """
    retry_requested = Signal()

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.collapsible_formatter = CollapsibleBlockFormatter(self.theme_manager)
        self.messages = []
        self.document_margin = 5
        self.is_streaming = False
        self.selection_start = None
        self.selection_end = None
        self.is_selecting = False
        self.current_assistant_name = "Assistant"
        self.click_count = 0
        self.last_click_time = 0
        self._retry_countdown_msg_idx = -1

        # Autoscroll functionality
        self.autoscroll_timer = QTimer(self)
        self.autoscroll_timer.setInterval(25)
        self.autoscroll_timer.timeout.connect(self._perform_autoscroll)
        self.autoscroll_direction = 0

        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.viewport().setMouseTracking(True)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.setContentsMargins(10, 10, 10, 10)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)
        logging.info("ChatWidget initialized.")

    def set_assistant_name(self, name: str):
        """Sets the display name for the assistant for subsequent messages."""
        if name:
            self.current_assistant_name = name

    # --- Selection and Copying Logic ---

    def _get_selection_range(self):
        if not self.selection_start or not self.selection_end:
            return None, None
        if self.selection_start > self.selection_end:
            return self.selection_end, self.selection_start
        return self.selection_start, self.selection_end

    def _pos_to_char_index(self, pos: QPoint):
        scroll_y = self.verticalScrollBar().value()
        target_y = pos.y() + scroll_y
        current_y = 0
        for i, msg in enumerate(self.messages):
            doc_height = msg['doc'].size().height()
            if current_y <= target_y < current_y + doc_height:
                relative_pos = pos - QPoint(0, int(current_y - scroll_y))
                layout = msg['doc'].documentLayout()
                # Set the default text cursor for link interaction
                self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
                if layout.anchorAt(relative_pos):
                    self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                else:
                    self.viewport().setCursor(Qt.CursorShape.IBeamCursor)

                char_index = layout.hitTest(relative_pos, Qt.HitTestAccuracy.FuzzyHit)
                return (i, char_index)
            current_y += doc_height + self.document_margin

        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # --- Link Click Handling for Collapsible Blocks ---
            pos_in_viewport = event.pos()
            char_pos_tuple = self._pos_to_char_index(pos_in_viewport)
            if char_pos_tuple:
                msg_idx, _ = char_pos_tuple
                scroll_value = self.verticalScrollBar().value()
                sum_heights = sum(m['doc'].size().height() + self.document_margin for m in self.messages[:msg_idx])
                relative_pos = pos_in_viewport - QPoint(0, int(-scroll_value + sum_heights))
                anchor = self.messages[msg_idx]['doc'].documentLayout().anchorAt(relative_pos)

                if anchor and anchor.startswith("toggle:"):
                    parts = anchor.split(':')
                    if len(parts) == 3:
                        block_id, new_state = parts[1], parts[2]
                        self._toggle_collapsible_block(block_id, new_state)
                        return # Stop further processing

                if anchor and anchor.startswith("retry:"):
                    self.retry_requested.emit()
                    return # Stop further processing

            # --- Standard Text Selection Logic ---
            current_time = QTime.currentTime().msecsSinceStartOfDay()
            double_click_interval = QApplication.instance().doubleClickInterval()

            if current_time - self.last_click_time < double_click_interval:
                self.click_count += 1
            else:
                self.click_count = 1
            self.last_click_time = current_time

            if not (char_pos_tuple and char_pos_tuple[1] >= 0):
                self.selection_start = None
                self.selection_end = None
                self.viewport().update()
                super().mousePressEvent(event)
                return

            msg_idx, char_idx = char_pos_tuple
            doc = self.messages[msg_idx]['doc']
            cursor = QTextCursor(doc)
            cursor.setPosition(char_idx)

            if self.click_count == 1:
                self.is_selecting = True
                self.selection_start = char_pos_tuple
                self.selection_end = char_pos_tuple
            elif self.click_count == 3:
                self.is_selecting = False
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                self.selection_start = (msg_idx, cursor.selectionStart())
                self.selection_end = (msg_idx, cursor.selectionEnd())

            self.viewport().update()

        super().mousePressEvent(event)

    def _toggle_collapsible_block(self, block_id: str, new_state: str):
        """Finds a collapsible block by its ID, toggles its state, and re-renders it."""
        for msg in self.messages:
            if msg.get('id') == block_id:
                msg['state'] = new_state
                # Re-render the document for this specific message
                new_html = self.collapsible_formatter.format_block(
                    title=msg['title'],
                    content=msg['full_content'],
                    block_id=msg['id'],
                    state=msg['state']
                )
                msg['doc'].setHtml(new_html)
                self._update_scroll_bar()
                self.viewport().update()
                break

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.click_count = 2 # Explicitly set count for triple-click detection

            char_pos = self._pos_to_char_index(event.pos())
            if not (char_pos and char_pos[1] >= 0):
                super().mouseDoubleClickEvent(event)
                return

            msg_idx, char_idx = char_pos
            doc = self.messages[msg_idx]['doc']
            cursor = QTextCursor(doc)
            cursor.setPosition(char_idx)

            self.is_selecting = False
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
            self.selection_start = (msg_idx, cursor.selectionStart())
            self.selection_end = (msg_idx, cursor.selectionEnd())
            self.viewport().update()

        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.pos()
        if self.is_selecting:
            scroll_margin = 20
            if pos.y() < scroll_margin:
                self.autoscroll_direction = -1
                if not self.autoscroll_timer.isActive():
                    self.autoscroll_timer.start()
            elif pos.y() > self.viewport().height() - scroll_margin:
                self.autoscroll_direction = 1
                if not self.autoscroll_timer.isActive():
                    self.autoscroll_timer.start()
            else:
                self.autoscroll_direction = 0
                if self.autoscroll_timer.isActive():
                    self.autoscroll_timer.stop()

            char_pos = self._pos_to_char_index(pos)
            if char_pos and char_pos[1] >= 0:
                self.selection_end = char_pos
                self.viewport().update()
        else:
            # This will update the cursor to a hand if hovering over a link
            self._pos_to_char_index(pos)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_selecting = False
            self.autoscroll_direction = 0
            if self.autoscroll_timer.isActive():
                self.autoscroll_timer.stop()
        super().mouseReleaseEvent(event)

    def _perform_autoscroll(self):
        if self.autoscroll_direction == 0:
            return

        scroll_bar = self.verticalScrollBar()
        new_value = scroll_bar.value() + (self.autoscroll_direction * 10)
        scroll_bar.setValue(new_value)

        y_pos = 0 if self.autoscroll_direction == -1 else self.viewport().height()
        edge_pos = QPoint(self.mapFromGlobal(self.cursor().pos()).x(), y_pos)

        char_pos = self._pos_to_char_index(edge_pos)
        if char_pos and char_pos[1] >= 0:
            self.selection_end = char_pos
            self.viewport().update()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        copy_action = menu.addAction("Copy")
        start, end = self._get_selection_range()
        copy_action.setEnabled(start is not None and start != end)
        copy_action.triggered.connect(self.copy_selection)
        copy_all_action = menu.addAction("Copy All")
        copy_all_action.setEnabled(bool(self.messages))
        copy_all_action.triggered.connect(self.copy_all_text)
        menu.exec(event.globalPos())

    def copy_selection(self):
        start, end = self._get_selection_range()
        if start is None:
            return
        start_msg_idx, start_char_idx = start
        end_msg_idx, end_char_idx = end
        selected_parts = []
        for i in range(start_msg_idx, end_msg_idx + 1):
            doc = self.messages[i]['doc']
            sel_start = start_char_idx if i == start_msg_idx else 0
            sel_end = end_char_idx if i == end_msg_idx else doc.characterCount() - 1
            cursor = QTextCursor(doc)
            cursor.setPosition(sel_start)
            cursor.setPosition(sel_end, QTextCursor.KeepAnchor)
            selected_parts.append(cursor.selectedText())
        full_text = "\n".join(selected_parts)
        if full_text:
            QApplication.clipboard().setText(full_text)

    def copy_all_text(self):
        all_text_parts = []
        for msg in self.messages:
            if msg.get('type') == 'collapsible':
                all_text_parts.append(f"--- {msg.get('title', 'Collapsible Block')} ---\n{msg.get('full_content', '')}")
            else:
                all_text_parts.append(msg['doc'].toPlainText())

        full_text = "\n\n".join(all_text_parts)
        if full_text:
            QApplication.clipboard().setText(full_text)

    # --- Original Scrolling and Message Handling Logic ---

    def add_message(self, data, force_scroll=False):
        if self.is_streaming:
            self.is_streaming = False

        height_before_new_message = self._calculate_content_height()
        doc = QTextDocument(self)
        doc.setDocumentMargin(0) # Let the widget handle margins

        if isinstance(data, dict) and data.get('type') == 'collapsible':
            block_id = f"block-{uuid.uuid4()}"
            initial_state = 'collapsed'
            html_content = self.collapsible_formatter.format_block(
                title=data.get('title', 'Details'),
                content=data.get('content', ''),
                block_id=block_id,
                state=initial_state
            )
            doc.setHtml(html_content)
            self.messages.append({
                'type': 'collapsible',
                'id': block_id,
                'state': initial_state,
                'title': data.get('title', 'Details'),
                'full_content': data.get('content', ''),
                'doc': doc
            })
        elif isinstance(data, str):
            html_content = data
            doc.setHtml(html_content)
            self.messages.append({
                'type': 'simple',
                'raw_content': html_content, # Store original HTML for user message check
                'doc': doc
            })
        else:
            logging.warning(f"ChatWidget received add_message with unknown data type: {type(data)}")
            return

        self._update_scroll_bar()

        is_user_message = isinstance(data, str) and "<b>You:</b>" in data
        if force_scroll:
            self.scroll_to_bottom()
        elif is_user_message:
            QTimer.singleShot(0, lambda: self._execute_scroll(height_before_new_message))

        self.viewport().update()


    def _execute_scroll(self, position):
        self.verticalScrollBar().setValue(position)

    def scroll_to_bottom(self):
        """Public method to scroll the chat to the very bottom."""
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def scroll_to_last_user_message(self):
        """
        Finds the last message from the user ("You") and scrolls the viewport
        so that this message is at the top.
        """
        if not self.messages:
            return

        last_user_message_index = -1
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if msg.get('type') == 'simple' and msg.get('raw_content', '').strip().startswith("<b>You:</b>"):
                last_user_message_index = i
                break

        if last_user_message_index == -1:
            self.scroll_to_bottom()
            return

        scroll_position = 0
        viewport_width = self.viewport().width() - (self.contentsMargins().left() + self.contentsMargins().right())
        if viewport_width <= 0:
            return

        for i in range(last_user_message_index):
            doc = self.messages[i]['doc']
            doc.setTextWidth(viewport_width)
            scroll_position += doc.size().height() + self.document_margin

        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(scroll_position))

    def scroll_to_last_message(self):
        """Calculates the position of the last message and scrolls to it, pinning it to the top."""
        if not self.messages:
            return

        scroll_position = 0
        viewport_width = self.viewport().width() - (self.contentsMargins().left() + self.contentsMargins().right())
        if viewport_width <= 0:
            return

        for i in range(len(self.messages) - 1):
            doc = self.messages[i]['doc']
            doc.setTextWidth(viewport_width)
            scroll_position += doc.size().height() + self.document_margin

        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(scroll_position))

    def show_thinking_ui(self, show: bool, status_text: str = "Thinking", actor: str = None):
        """
        Displays or hides the 'thinking' UI.
        Uses the provided actor name or the pre-set assistant name and displays the current status.
        """
        actor_name = actor if actor is not None else self.current_assistant_name
        if show:
            if self.is_streaming and self.messages and "..." in self.messages[-1].get('raw_content', ''):
                last_message = self.messages[-1]
                new_content = f"<b>{actor_name}:</b> {status_text}... 0s"
                last_message['raw_content'] = new_content
                last_message['doc'].setHtml(new_content)
            else:
                if self.is_streaming:
                    self.is_streaming = False
                doc = QTextDocument(self)
                initial_content = f"<b>{actor_name}:</b> {status_text}... 0s"
                doc.setHtml(initial_content)
                self.messages.append({'type': 'simple', 'doc': doc, 'raw_content': initial_content})
                self.is_streaming = True
            self._update_scroll_bar()
        else:
            self.is_streaming = False

    def show_retry_countdown(self, attempt_num: int, max_attempts: int, wait_seconds: float):
        """Shows a visible countdown to the next automatic retry in the chat."""
        self.stop_retry_countdown()
        self._retry_countdown_attempt = attempt_num
        self._retry_countdown_max = max_attempts
        initial = max(1, int(wait_seconds))
        content = (f"<i>Provider busy — automatic retry in {initial}s "
                   f"(attempt {attempt_num}/{max_attempts})...</i>")
        doc = QTextDocument(self)
        doc.setHtml(content)
        self.messages.append({'type': 'simple', 'doc': doc, 'raw_content': content})
        self._retry_countdown_msg_idx = len(self.messages) - 1
        self._update_scroll_bar()
        self.viewport().update()

    def update_retry_countdown(self, remaining: float):
        """Updates the countdown message with the remaining seconds."""
        if self._retry_countdown_msg_idx < 0:
            return
        if self._retry_countdown_msg_idx >= len(self.messages):
            self._retry_countdown_msg_idx = -1
            self._retry_countdown_attempt = 0
            self._retry_countdown_max = 0
            return
        seconds_left = max(0, int(remaining))
        msg = self.messages[self._retry_countdown_msg_idx]
        if seconds_left > 0:
            content = msg['raw_content']
            import re
            new_content = re.sub(r"in \d+s", f"in {seconds_left}s", content)
            if new_content == content:
                new_content = (f"<i>Provider busy — automatic retry in {seconds_left}s "
                               f"(attempt {self._retry_countdown_attempt}/{self._retry_countdown_max})...</i>")
            msg['raw_content'] = new_content
            msg['doc'].setHtml(new_content)
        else:
            msg['raw_content'] = "<i>Retrying now...</i>"
            msg['doc'].setHtml(msg['raw_content'])
        self._update_scroll_bar()
        self.viewport().update()

    def stop_retry_countdown(self):
        """Removes the countdown message (retry succeeded or failed)."""
        if self._retry_countdown_msg_idx < 0:
            return
        if self._retry_countdown_msg_idx < len(self.messages):
            self.messages.pop(self._retry_countdown_msg_idx)
        self._retry_countdown_msg_idx = -1
        self._update_scroll_bar()
        self.viewport().update()

    def update_timer(self, seconds: int):
        if self.is_streaming and self.messages and "..." in self.messages[-1].get('raw_content', ''):
            last_message = self.messages[-1]
            content_html = last_message['raw_content']
            try:
                parts = content_html.split("</b>")
                actor_html = parts[0] + "</b>"
                status_text = parts[1].split("...")[0].strip() + "..."
            except IndexError:
                actor_html = f"<b>{self.current_assistant_name}:</b>"
                status_text = "Thinking..."

            new_content = f"{actor_html} {status_text} {seconds}s"
            last_message['raw_content'] = new_content
            last_message['doc'].setHtml(new_content)
            self._update_scroll_bar()

    def append_to_stream(self, text_chunk: str):
        if not self.is_streaming or not self.messages:
            doc = QTextDocument(self)
            initial_content = f"<b>{self.current_assistant_name}:</b> "
            doc.setHtml(initial_content)
            self.messages.append({'type': 'simple', 'doc': doc, 'raw_content': initial_content})
            self.is_streaming = True

        last_message = self.messages[-1]

        if "..." in last_message.get('raw_content', '') and "s" in last_message.get('raw_content', ''):
            prefix_to_remove = f"{self.current_assistant_name}:"
            if text_chunk.strip().lower().startswith(prefix_to_remove.lower()):
                start_index = text_chunk.lower().find(prefix_to_remove.lower()) + len(prefix_to_remove)
                text_chunk = text_chunk[start_index:].lstrip()

            last_message['raw_content'] = f"<b>{self.current_assistant_name}:</b> "
            last_message['doc'].setHtml(last_message['raw_content'])

        cursor = QTextCursor(last_message['doc'])
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text_chunk)

        last_message['raw_content'] += text_chunk

        self._update_scroll_bar()

    def finalize_stream(self, final_html_content: str):
        """
        Finalizes a streaming message by replacing its content with the final version.
        """
        if self.is_streaming and self.messages:
            last_message = self.messages[-1]
            last_message['raw_content'] = final_html_content
            last_message['doc'].setHtml(final_html_content)
            self._update_scroll_bar()
            self.viewport().update()
        self.is_streaming = False

    def _calculate_content_height(self):
        content_height = 0
        viewport_width = self.viewport().width() - (self.contentsMargins().left() + self.contentsMargins().right())
        if viewport_width <= 0:
            return 0
        for msg in self.messages:
            msg['doc'].setTextWidth(viewport_width)
            content_height += msg['doc'].size().height() + self.document_margin
        return content_height

    def _update_scroll_bar(self):
        content_height = self._calculate_content_height()
        viewport_height = self.viewport().height()

        user_message_count = sum(
            1 for msg in self.messages
            if msg.get('type') == 'simple' and msg.get('raw_content', '').startswith("<b>You:</b>")
        )

        if user_message_count > 1:
            height_of_last_message = 0
            if self.messages:
                last_msg_doc = self.messages[-1]['doc']
                viewport_width = (
                    self.viewport().width()
                    - (self.contentsMargins().left() + self.contentsMargins().right())
                )
                if viewport_width > 0:
                    last_msg_doc.setTextWidth(viewport_width)
                height_of_last_message = last_msg_doc.size().height() + self.document_margin
            pinning_scroll_pos = content_height - height_of_last_message
            standard_max_scroll = max(0, content_height - viewport_height)
            new_range = max(pinning_scroll_pos, standard_max_scroll)
        else:
            new_range = max(0, content_height - viewport_height)

        self.verticalScrollBar().setRange(0, new_range)
        self.verticalScrollBar().setPageStep(viewport_height)
        self.viewport().update()

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        scroll_y = self.verticalScrollBar().value()
        start_sel, end_sel = self._get_selection_range()
        current_y = -scroll_y
        for i, msg in enumerate(self.messages):
            doc = msg['doc']
            doc_height = doc.size().height()
            message_rect = QRect(0, int(current_y), self.viewport().width(), int(doc_height))
            if message_rect.intersects(event.rect()):
                painter.save()
                painter.translate(0, current_y)
                ctx = QAbstractTextDocumentLayout.PaintContext()
                ctx.palette = self.palette()
                selections = []
                if start_sel is not None:
                    start_msg_idx, start_char_idx = start_sel
                    end_msg_idx, end_char_idx = end_sel
                    if start_msg_idx <= i <= end_msg_idx:
                        selection_cursor = QTextCursor(doc)
                        sel_start = start_char_idx if i == start_msg_idx else 0
                        sel_end = end_char_idx if i == end_msg_idx else doc.characterCount() - 1
                        selection_cursor.setPosition(sel_start)
                        selection_cursor.setPosition(sel_end, QTextCursor.KeepAnchor)
                        selection_format = QTextCharFormat()
                        selection_format.setBackground(QApplication.palette().highlight())
                        selection_format.setForeground(QApplication.palette().highlightedText())
                        selection = QAbstractTextDocumentLayout.Selection()
                        selection.cursor = selection_cursor
                        selection.format = selection_format
                        selections.append(selection)
                ctx.selections = selections
                doc.documentLayout().draw(painter, ctx)
                painter.restore()
            current_y += doc_height + self.document_margin

    def resizeEvent(self, event):
        self._update_scroll_bar()
        super().resizeEvent(event)
