# ui/chat_widget.py
import html
import logging
import sys
import uuid

from PySide6.QtCore import Qt, QPoint, QRect, QSize, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPainter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QMenu, QPushButton

from src.ui.text_render import render_text
from src.ui.utils import IconHoverFilter
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
    continue_requested = Signal()
    job_stop_requested = Signal(int)

    # Readable, centered text column. On windows wider than this the chat text
    # stops stretching edge-to-edge and is centered with symmetric margins
    # (mirrors opencode's ~800px content column); on narrower windows the text
    # keeps the full available width.
    MAX_CONTENT_WIDTH = 800
    # On a maximised/very wide window the column may grow a little wider, but
    # only once the window is near full-screen width. The trigger is a fraction
    # of the screen width so it adapts to DPI scaling and monitor size (a fixed
    # pixel value silently fails at 125%/150% scaling).
    MAX_CONTENT_WIDTH_WIDE = 960
    WIDE_CONTENT_SCREEN_RATIO = 0.9

    @classmethod
    def content_max_width(cls, window_width: int, screen_width=None) -> int:
        """Pick the column cap for a window of ``window_width`` logical px."""
        if screen_width is None:
            app = QApplication.instance()
            screen = app.primaryScreen() if app is not None else None
            screen_width = screen.availableGeometry().width() if screen is not None else 0
        if screen_width > 0 and window_width >= int(screen_width * cls.WIDE_CONTENT_SCREEN_RATIO):
            return cls.MAX_CONTENT_WIDTH_WIDE
        return cls.MAX_CONTENT_WIDTH

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.collapsible_formatter = CollapsibleBlockFormatter(self.theme_manager)
        self.messages = []
        self.document_margin = 8
        self.chat_font = QFont(QApplication.font())
        if self.chat_font.pointSize() > 0:
            self.chat_font.setPointSize(self.chat_font.pointSize() + 1)
        self.is_streaming = False
        # The message dict the live stream writes into. Tracked by reference
        # instead of ``messages[-1]`` so queued user messages appended below the
        # in-flight answer never become the stream target.
        self._streaming_msg = None
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

        # Floating "jump to the latest message" button, shown only when the
        # user has scrolled away from the bottom of the conversation.
        self.scroll_to_bottom_button = QPushButton(self.viewport())
        self.scroll_to_bottom_button.setObjectName("ScrollToBottomButton")
        self.scroll_to_bottom_button.setIconSize(QSize(18, 18))
        self.scroll_to_bottom_button.setFixedSize(36, 36)
        self.scroll_to_bottom_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scroll_to_bottom_button.setToolTip("Scroll to bottom")
        self.scroll_to_bottom_button.clicked.connect(self.scroll_to_bottom)
        self.scroll_to_bottom_button.installEventFilter(
            IconHoverFilter(theme_manager, 'fa5s.arrow-down',
                            parent=self.scroll_to_bottom_button))
        self.scroll_to_bottom_button.setVisible(False)
        theme_manager.theme_changed.connect(self._apply_scroll_to_bottom_style)
        theme_manager.theme_changed.connect(self.viewport().update)
        self._apply_scroll_to_bottom_style()
        self.verticalScrollBar().valueChanged.connect(
            self._update_scroll_to_bottom_button)
        logging.info("ChatWidget initialized.")

    def set_assistant_name(self, name: str):
        """Sets the display name for the assistant for subsequent messages."""
        if name:
            self.current_assistant_name = name

    def _new_document(self) -> QTextDocument:
        doc = QTextDocument(self)
        doc.setDocumentMargin(0)
        doc.setDefaultFont(self.chat_font)
        return doc

    def _content_geometry(self):
        """Return ``(x_offset, text_width)`` of the centered text column.

        Narrow windows keep the full available width (x_offset 0, unchanged
        behaviour); once the available width exceeds ``MAX_CONTENT_WIDTH`` the
        column is capped and centered, leaving symmetric side margins.
        """
        viewport_width = self.viewport().width()
        if viewport_width <= 0:
            return 0, 0
        margins = self.contentsMargins()
        available = viewport_width - margins.left() - margins.right()
        if available <= 0:
            available = viewport_width
        fn = getattr(self, "content_max_width_fn", None)
        cap = fn() if fn is not None else self.content_max_width(self.window().width())
        if available <= cap:
            return 0, available
        width = cap
        # Center on the full column (viewport + scrollbar) so the text stays
        # aligned with the centered input/toolbar block and does not jump a few
        # pixels when the vertical scrollbar appears or disappears.
        bar = self.verticalScrollBar()
        extra = bar.width() if bar.isVisible() else 0
        return (viewport_width + extra - width) // 2, width

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
                x_offset, _ = self._content_geometry()
                relative_pos = QPoint(pos.x() - x_offset, pos.y() - int(current_y - scroll_y))
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
                x_offset, _ = self._content_geometry()
                relative_pos = QPoint(
                    pos_in_viewport.x() - x_offset,
                    pos_in_viewport.y() - int(-scroll_value + sum_heights),
                )
                anchor = self.messages[msg_idx]['doc'].documentLayout().anchorAt(relative_pos)

                if anchor and anchor.startswith("toggle:"):
                    parts = anchor.split(':')
                    if len(parts) == 3:
                        block_id, new_state = parts[1], parts[2]
                        self._toggle_collapsible_block(block_id, new_state)
                        return # Stop further processing

                if anchor and anchor.startswith("copy:"):
                    parts = anchor.split(':', 2)
                    if len(parts) == 3:
                        self._copy_collapsible_payload(parts[1], parts[2])
                        return

                if anchor and anchor.startswith("action:"):
                    parts = anchor.split(':', 2)
                    if len(parts) == 3:
                        self._run_collapsible_action(parts[1], parts[2])
                        return

                if anchor and anchor.startswith("retry:"):
                    self.retry_requested.emit()
                    return # Stop further processing

                if anchor and anchor.startswith("continue:"):
                    self.continue_requested.emit()
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

    # Max chars rendered inside an expanded block. Beyond this Qt's text
    # layout on huge docs freezes the UI; full text stays in full_content
    # (copy-all still gives everything).
    MAX_RENDERED_CONTENT = 30000

    def _format_collapsible_message(self, msg: dict) -> str:
        full = msg.get('full_content') or ''
        if not isinstance(full, str):
            full = str(full)
        full_html = msg.get('full_content_html')
        if full_html:
            content = full_html
        else:
            content = full
            if len(content) > self.MAX_RENDERED_CONTENT:
                content = (content[:self.MAX_RENDERED_CONTENT]
                           + f"\n… [truncated for display, "
                             f"{len(full) - self.MAX_RENDERED_CONTENT} chars hidden]")
        return self.collapsible_formatter.format_block(
            title=msg.get('title', 'Details'),
            content=content,
            block_id=msg['id'],
            state=msg.get('state', 'collapsed'),
            title_color=msg.get('title_color'),
            content_is_html=bool(full_html),
            copy_payloads=msg.get('copy_payloads'),
            actions=msg.get('actions'),
        )

    def _run_collapsible_action(self, block_id: str, action: str) -> bool:
        for msg in self.messages:
            if msg.get('id') != block_id:
                continue
            if action == 'stop_job':
                try:
                    self.job_stop_requested.emit(int(msg.get('job_id')))
                except (TypeError, ValueError):
                    return False
                return True
            if action == 'open_log':
                path = msg.get('open_log_path')
                if path:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                    return True
            return False
        return False

    def update_job_block(self, job_id: int, data: dict) -> bool:
        """Replace a job's block with its final result and drop stale polls."""
        target = None
        for msg in self.messages:
            if msg.get('type') != 'collapsible' or msg.get('job_id') != job_id:
                continue
            if msg.get('job_role') == 'launch':
                target = msg
                break
            if target is None:
                target = msg
        if target is None:
            return False
        was_at_bottom = self._is_at_bottom()
        old_scroll = self.verticalScrollBar().value()
        target['title'] = data.get('title', target.get('title', 'Details'))
        target['state'] = data.get('state', target.get('state', 'collapsed'))
        target['full_content'] = data.get('plain_content', data.get('content', ''))
        target['full_content_html'] = (
            data.get('content') if data.get('content_is_html') else None)
        target['title_color'] = data.get('title_color')
        target['job_state'] = data.get('job_state', target.get('job_state'))
        target['copy_payloads'] = data.get('copy_payloads') or {}
        target['actions'] = data.get('actions') or {}
        target['open_log_path'] = data.get('open_log_path')
        try:
            new_doc = self._new_document()
            new_doc.setHtml(self._format_collapsible_message(target))
            target['doc'] = new_doc
        except Exception as e:
            logging.warning(f"Job block update failed for #{job_id}: {e}")
            return False
        removed = False
        for msg in list(self.messages):
            if msg is target:
                continue
            if (msg.get('type') == 'collapsible'
                    and msg.get('job_id') == job_id):
                self.messages.remove(msg)
                removed = True
        if removed:
            self.selection_start = None
            self.selection_end = None
            self.is_selecting = False
        self._update_scroll_bar()
        if was_at_bottom:
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().maximum())
        elif removed:
            self.verticalScrollBar().setValue(
                min(old_scroll, self.verticalScrollBar().maximum()))
        self.viewport().update()
        return True

    def _copy_collapsible_payload(self, block_id: str, label: str) -> bool:
        """Copy a labelled payload from an expanded collapsible block."""
        for msg in self.messages:
            if msg.get('id') == block_id:
                payload = (msg.get('copy_payloads') or {}).get(label)
                if payload:
                    QApplication.clipboard().setText(str(payload))
                    return True
                return False
        return False

    def _toggle_collapsible_block(self, block_id: str, new_state: str):
        """Finds a collapsible block by its ID, toggles its state, and re-renders it.

        Builds a FRESH QTextDocument instead of re-setHtml'ing the old one:
        reusing a document whose layout/resources are live (paint, hit-test,
        cursors) is a use-after-free risk that once killed the app here.
        """
        for msg in self.messages:
            if msg.get('id') == block_id:
                msg['state'] = new_state
                try:
                    new_html = self._format_collapsible_message(msg)
                except Exception as e:
                    logging.warning(f"Collapsible render failed for {block_id}: {e}")
                    return
                logging.info(f"ChatWidget: toggling {block_id} -> {new_state} "
                             f"(content {len(msg.get('full_content') or '')} chars).")
                new_doc = self._new_document()
                new_doc.setHtml(new_html)
                msg['doc'] = new_doc
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

    def _is_at_bottom(self, tolerance: int = 20) -> bool:
        """Whether the viewport currently shows the end of the chat."""
        bar = self.verticalScrollBar()
        return bar.maximum() > tolerance and bar.value() >= bar.maximum() - tolerance

    def _follow_if_at_bottom(self, was_at_bottom: bool):
        """Follows new content when the user was already at the bottom.

        Synchronous on purpose: the scroll range was just recomputed in the
        same call, so setValue(maximum()) is exact here — unlike the async
        scroll_to_bottom(), whose late-evaluated maximum() can race with
        later content changes. Never yanks a reading user. The pre-existing
        async scroll paths (explicit jumps, pinning) are untouched.
        """
        if was_at_bottom:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())

    def clear_messages(self):
        """Drops all rendered messages (used when switching projects)."""
        self.messages = []
        self.is_streaming = False
        self._streaming_msg = None
        self._retry_countdown_msg_idx = -1
        self.selection_start = None
        self.selection_end = None
        self._update_scroll_bar()
        self.verticalScrollBar().setValue(0)
        self.viewport().update()

    def add_message(self, data, force_scroll=False, keep_stream=False, role=None):
        streaming_msg = self._streaming_msg
        if not keep_stream:
            if self.is_streaming:
                self.is_streaming = False
            self._streaming_msg = None

        was_at_bottom = self._is_at_bottom()
        doc = self._new_document()

        if isinstance(data, dict) and data.get('type') == 'collapsible':
            block_id = f"block-{uuid.uuid4()}"
            initial_state = data.get('state', 'collapsed')
            if initial_state not in ('collapsed', 'expanded'):
                initial_state = 'collapsed'
            content_is_html = bool(data.get('content_is_html'))
            msg = {
                'type': 'collapsible',
                'id': block_id,
                'state': initial_state,
                'title': data.get('title', 'Details'),
                'full_content': data.get('plain_content', data.get('content', '')),
                'full_content_html': data.get('content') if content_is_html else None,
                'title_color': data.get('title_color'),
                'copy_payloads': data.get('copy_payloads') or {},
                'actions': data.get('actions') or {},
                'open_log_path': data.get('open_log_path'),
                'job_id': data.get('job_id'),
                'job_role': data.get('job_role'),
                'job_state': data.get('job_state'),
                'doc': doc,
            }
            html_content = self._format_collapsible_message(msg)
            doc.setHtml(html_content)
        elif isinstance(data, str):
            html_content = data
            doc.setHtml(html_content)
            msg = {
                'type': 'simple',
                'raw_content': html_content,
                'role': role,
                'doc': doc
            }
        else:
            logging.warning(f"ChatWidget received add_message with unknown data type: {type(data)}")
            return

        if keep_stream and streaming_msg is not None and streaming_msg in self.messages:
            # Insert before the in-flight reply so it keeps streaming into its
            # own message and stays the last (growing) bubble. Used for notices
            # shown while a reply is still arriving (e.g. a model switch).
            self.messages.insert(self.messages.index(streaming_msg), msg)
        else:
            self.messages.append(msg)

        self._update_scroll_bar()

        try:
            compact = bool(self.is_compact_fn()) if getattr(
                self, "is_compact_fn", None) else False
        except Exception:
            compact = False
        if force_scroll or compact:
            # Explicit jumps and the compact strip always show the tail.
            self.scroll_to_bottom()
        elif was_at_bottom:
            # Stay pinned to the bottom only when the user is already there;
            # never yank someone who scrolled up to read. This also covers user
            # messages — they are no longer pinned to the top of the viewport,
            # which used to shove the whole history off-screen.
            self._follow_if_at_bottom(was_at_bottom)

        self.viewport().update()

    def add_queued_message(self, raw_html: str, display_html: str):
        """Appends a user message shown as "queued" (dimmed).

        Unlike ``add_message`` this does NOT touch the active stream state, so
        the in-flight answer keeps streaming into its own message while the
        queued message sits below it. Returns the message dict (a handle used
        later to un-dim it, ``activate_queued_message``).
        """
        was_at_bottom = self._is_at_bottom()
        doc = self._new_document()
        doc.setHtml(display_html)
        msg = {
            'type': 'simple',
            'raw_content': raw_html,  # normal (undimmed) HTML, restored on send
            'role': 'user',
            'doc': doc,
            'queued': True,
        }
        self.messages.append(msg)
        self._update_scroll_bar()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)
        return msg

    def activate_queued_message(self, msg):
        """Un-dims a queued message when its turn to be answered arrives."""
        if not isinstance(msg, dict) or not msg.get('queued'):
            return
        was_at_bottom = self._is_at_bottom()
        msg['queued'] = False
        msg['doc'].setHtml(msg.get('raw_content', ''))
        self._update_scroll_bar()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)

    def remove_message(self, msg):
        """Removes a specific message by identity (drops an abandoned queue item)."""
        for i, m in enumerate(self.messages):
            if m is msg:
                self.messages.pop(i)
                if self._retry_countdown_msg_idx > i:
                    self._retry_countdown_msg_idx -= 1
                elif self._retry_countdown_msg_idx == i:
                    self._retry_countdown_msg_idx = -1
                if self._streaming_msg is msg:
                    self._streaming_msg = None
                    self.is_streaming = False
                self._update_scroll_bar()
                self.viewport().update()
                return

    def scroll_to_bottom(self):
        """Public method to scroll the chat to the very bottom."""
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def _apply_scroll_to_bottom_style(self):
        self.scroll_to_bottom_button.setStyleSheet(
            self.theme_manager.get_style("SCROLL_BOTTOM_BUTTON_STYLE"))

    def _update_scroll_to_bottom_button(self, *args):
        """Show the jump-to-bottom button when scrolled away from the tail."""
        if getattr(self, "scroll_to_bottom_button", None) is None:
            return
        bar = self.verticalScrollBar()
        should_show = bar.maximum() > 0 and not self._is_at_bottom()
        button = self.scroll_to_bottom_button
        if button.isVisible() != should_show:
            button.setVisible(should_show)
        if not should_show:
            return
        x_offset, text_width = self._content_geometry()
        if text_width <= 0:
            return
        x = x_offset + (text_width - button.width()) // 2
        y = max(0, self.viewport().height() - button.height() - 16)
        button.move(x, y)
        button.raise_()

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
            if msg.get('type') == 'simple' and msg.get('role') == 'user':
                last_user_message_index = i
                break

        if last_user_message_index == -1:
            self.scroll_to_bottom()
            return

        scroll_position = 0
        _, text_width = self._content_geometry()
        if text_width <= 0:
            return

        for i in range(last_user_message_index):
            doc = self.messages[i]['doc']
            doc.setTextWidth(text_width)
            scroll_position += doc.size().height() + self.document_margin

        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(scroll_position))

    def scroll_to_last_message(self):
        """Calculates the position of the last message and scrolls to it, pinning it to the top."""
        if not self.messages:
            return

        scroll_position = 0
        _, text_width = self._content_geometry()
        if text_width <= 0:
            return

        for i in range(len(self.messages) - 1):
            doc = self.messages[i]['doc']
            doc.setTextWidth(text_width)
            scroll_position += doc.size().height() + self.document_margin

        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(scroll_position))

    def _status_html(self, status_text: str, seconds: int = 0) -> str:
        """Muted one-line status. The model is never named here."""
        color = self.theme_manager.get_style("COLLAPSIBLE_META")
        label = html.escape((status_text or "Thinking").strip().rstrip("."))
        return f'<p style="margin: 0; color: {color};"><i>{label}… {seconds}s</i></p>'

    def show_thinking_ui(self, show: bool, status_text: str = "Thinking"):
        """Shows or removes the live status line.

        The line is transient: hiding it removes it from the transcript, so
        agent steps no longer leave a trail of stale "Thinking… Ns" lines.
        Chat reuses the same message for the streamed answer (is_status flips
        on the first chunk).
        """
        was_at_bottom = self._is_at_bottom()
        if show:
            last_message = self._streaming_msg
            if (self.is_streaming and last_message is not None
                    and last_message.get('is_status')):
                last_message['status_text'] = status_text
                last_message['raw_content'] = self._status_html(status_text)
                last_message['doc'].setHtml(last_message['raw_content'])
            else:
                if self.is_streaming:
                    self.is_streaming = False
                doc = self._new_document()
                initial_content = self._status_html(status_text)
                doc.setHtml(initial_content)
                msg = {'type': 'simple', 'doc': doc,
                       'raw_content': initial_content,
                       'is_status': True, 'status_text': status_text,
                       'plain': '', 'actor': self.current_assistant_name}
                self.messages.append(msg)
                self._streaming_msg = msg
                self.is_streaming = True
            self._update_scroll_bar()
            self._follow_if_at_bottom(was_at_bottom)
        else:
            status_msg = self._streaming_msg
            self.is_streaming = False
            self._streaming_msg = None
            if status_msg is not None and status_msg.get('is_status'):
                self.remove_message(status_msg)
                self._follow_if_at_bottom(was_at_bottom)

    def _notice_html(self, text: str) -> str:
        """Muted single-line system voice."""
        color = self.theme_manager.get_style("COLLAPSIBLE_META")
        return f'<p style="margin: 0; color: {color};"><i>{html.escape(text)}</i></p>'

    def show_retry_countdown(self, attempt_num: int, max_attempts: int, wait_seconds: float):
        """Shows a visible countdown to the next automatic retry in the chat."""
        was_at_bottom = self._is_at_bottom()
        self.stop_retry_countdown()
        self._retry_countdown_attempt = attempt_num
        self._retry_countdown_max = max_attempts
        initial = max(1, int(wait_seconds))
        content = self._notice_html(
            f"Provider busy — automatic retry in {initial}s "
            f"(attempt {attempt_num}/{max_attempts})…")
        doc = self._new_document()
        doc.setHtml(content)
        self.messages.append({'type': 'simple', 'doc': doc, 'raw_content': content})
        self._retry_countdown_msg_idx = len(self.messages) - 1
        self._update_scroll_bar()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)

    def update_retry_countdown(self, remaining: float):
        """Updates the countdown message with the remaining seconds."""
        if self._retry_countdown_msg_idx < 0:
            return
        if self._retry_countdown_msg_idx >= len(self.messages):
            self._retry_countdown_msg_idx = -1
            self._retry_countdown_attempt = 0
            self._retry_countdown_max = 0
            return
        was_at_bottom = self._is_at_bottom()
        seconds_left = max(0, int(remaining))
        msg = self.messages[self._retry_countdown_msg_idx]
        if seconds_left > 0:
            content = msg['raw_content']
            import re
            new_content = re.sub(r"in \d+s", f"in {seconds_left}s", content)
            if new_content == content:
                new_content = self._notice_html(
                    f"Provider busy — automatic retry in {seconds_left}s "
                    f"(attempt {self._retry_countdown_attempt}/{self._retry_countdown_max})…")
            msg['raw_content'] = new_content
            msg['doc'].setHtml(new_content)
        else:
            msg['raw_content'] = self._notice_html("Retrying now…")
            msg['doc'].setHtml(msg['raw_content'])
        self._update_scroll_bar()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)

    def stop_retry_countdown(self):
        """Removes the countdown message (retry succeeded or failed)."""
        if self._retry_countdown_msg_idx < 0:
            return
        was_at_bottom = self._is_at_bottom()
        if self._retry_countdown_msg_idx < len(self.messages):
            self.messages.pop(self._retry_countdown_msg_idx)
        self._retry_countdown_msg_idx = -1
        self._update_scroll_bar()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)

    def show_tool_progress(self, ticket: int, html_text: str):
        """Creates or updates the live progress line of a running tool."""
        for msg in self.messages:
            if msg.get('live_ticket') == ticket:
                msg['doc'].setHtml(html_text)
                self._update_scroll_bar()
                self.viewport().update()
                return
        was_at_bottom = self._is_at_bottom()
        doc = self._new_document()
        doc.setHtml(html_text)
        self.messages.append({'type': 'live', 'live_ticket': ticket, 'doc': doc})
        self._update_scroll_bar()
        self._follow_if_at_bottom(was_at_bottom)
        self.viewport().update()

    def hide_tool_progress(self, ticket: int):
        """Removes the live progress line of a finished tool."""
        for i, msg in enumerate(self.messages):
            if msg.get('live_ticket') == ticket:
                self.messages.pop(i)
                if self._retry_countdown_msg_idx > i:
                    self._retry_countdown_msg_idx -= 1
                self._update_scroll_bar()
                self.viewport().update()
                return

    def update_timer(self, seconds: int):
        was_at_bottom = self._is_at_bottom()
        last_message = self._streaming_msg
        if (self.is_streaming and last_message is not None
                and last_message.get('is_status')):
            new_content = self._status_html(
                last_message.get('status_text', 'Thinking'), seconds)
            last_message['raw_content'] = new_content
            last_message['doc'].setHtml(new_content)
            self._update_scroll_bar()
            self._follow_if_at_bottom(was_at_bottom)

    def append_to_stream(self, text_chunk: str):
        was_at_bottom = self._is_at_bottom()
        if self.is_streaming and self._streaming_msg is not None:
            actor_name = self._streaming_msg.get('actor') or self.current_assistant_name
        else:
            actor_name = self.current_assistant_name
        if not self.is_streaming or self._streaming_msg is None:
            doc = self._new_document()
            doc.setHtml("")
            msg = {'type': 'simple', 'doc': doc, 'raw_content': '',
                   'plain': '', 'actor': actor_name}
            self.messages.append(msg)
            self._streaming_msg = msg
            self.is_streaming = True

        last_message = self._streaming_msg

        # Replace the "Thinking… Ns" placeholder with the real response.
        if last_message.get('is_status'):
            last_message['is_status'] = False
            last_message['plain'] = ''

        plain = last_message.get('plain', '')
        # The model sometimes echoes its own "Name:" header; drop it.
        stripped = text_chunk.lstrip()
        if not plain and stripped.lower().startswith(actor_name.lower() + ":"):
            text_chunk = stripped[len(actor_name) + 1:].lstrip()

        plain += text_chunk
        last_message['plain'] = plain
        # Render Markdown live so headings/lists/bold show while streaming
        # instead of raw ### / ** syntax.
        rendered = render_text(plain, markdown_ok=True)
        last_message['raw_content'] = rendered
        last_message['doc'].setHtml(rendered)

        self._update_scroll_bar()
        self._follow_if_at_bottom(was_at_bottom)

    def finalize_stream(self, final_html_content: str):
        """
        Finalizes a streaming message by replacing its content with the final version.
        """
        was_at_bottom = self._is_at_bottom()
        last_message = self._streaming_msg
        if last_message is not None:
            last_message['raw_content'] = final_html_content
            last_message['doc'].setHtml(final_html_content)
            self._update_scroll_bar()
            self.viewport().update()
        self.is_streaming = False
        self._streaming_msg = None
        self._follow_if_at_bottom(was_at_bottom)

    def _calculate_content_height(self):
        content_height = 0
        _, text_width = self._content_geometry()
        if text_width <= 0:
            return 0
        for msg in self.messages:
            msg['doc'].setTextWidth(text_width)
            content_height += msg['doc'].size().height() + self.document_margin
        return content_height

    def _update_scroll_bar(self):
        # Standard scroll range: from the very top to showing the tail. No
        # over-scroll / "pin the last message to the top" range — that was what
        # made the history fly off-screen on send and showed only the last line
        # in the compact strip.
        content_height = self._calculate_content_height()
        viewport_height = self.viewport().height()
        new_range = max(0, content_height - viewport_height)

        self.verticalScrollBar().setRange(0, new_range)
        self.verticalScrollBar().setPageStep(viewport_height)
        self.viewport().update()
        self._update_scroll_to_bottom_button()

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        bg_color = self.theme_manager.get_style("CHAT_BACKGROUND")
        if bg_color:
            painter.fillRect(event.rect(), QColor(bg_color))
        scroll_y = self.verticalScrollBar().value()
        start_sel, end_sel = self._get_selection_range()
        current_y = -scroll_y
        x_offset, text_width = self._content_geometry()
        for i, msg in enumerate(self.messages):
            doc = msg['doc']
            doc_height = doc.size().height()
            message_rect = QRect(x_offset, int(current_y), text_width, int(doc_height))
            if message_rect.intersects(event.rect()):
                painter.save()
                painter.translate(x_offset, current_y)
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
