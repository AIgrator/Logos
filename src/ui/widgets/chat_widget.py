# ui/chat_widget.py
import html
import json
import logging
import sys
import time
import uuid
from urllib.parse import unquote

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QSize, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPen,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextTable,
)
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QMenu, QPushButton

from src.ui.text_render import USER_BUBBLE_PADDING, USER_BUBBLE_RADIUS, render_text
from src.ui.utils import IconHoverFilter
from src.ui.widgets.activity_block_formatter import (
    ActivityBlockFormatter,
    block_summary,
    run_default_expanded,
    steps_in_run,
)
from src.ui.widgets.collapsible_block_formatter import CollapsibleBlockFormatter
from src.ui.widgets.status_marks import register_status_images

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
    attachment_preview_requested = Signal(str, str)
    suggestion_selected = Signal(str)

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
        self.activity_formatter = ActivityBlockFormatter(self.theme_manager)
        self.messages = []
        self.document_margin = 8
        self.chat_font = QFont(QApplication.font())
        if self.chat_font.pointSize() > 0:
            self.chat_font.setPointSize(self.chat_font.pointSize() + 1)
        # One-line thought excerpts are clipped by measurement, because Qt's
        # rich text has no text-overflow. The formatter asks the widget for the
        # column width and for font metrics so the "…" lands where the row ends.
        self.activity_formatter.available_width = self._activity_column_width
        self.activity_formatter.measure_text = self._measure_activity_text
        # A narrower window re-clips those excerpts: they are measured when the
        # block renders, so the blocks are rendered again once a resize settles.
        self._excerpt_timer = QTimer(self)
        self._excerpt_timer.setSingleShot(True)
        self._excerpt_timer.setInterval(220)
        self._excerpt_timer.timeout.connect(self._rerender_excerpts)
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

        # Live activity blocks re-render once a second so their running
        # step's elapsed time ticks without handler-driven updates.
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._activity_tick)

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

        # Wheel speed: one notch scrolls the system-configured number of text
        # lines. The default QAbstractScrollArea path moves the bar by
        # singleStep(1) × wheelScrollLines(3) = 3 px per notch, which feels
        # frozen in a pixel-range transcript.
        self._wheel_px_per_line = max(1, QFontMetrics(self.chat_font).lineSpacing())
        self._wheel_remainder = 0.0

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

        # Sticky header: a long turn - a written file, a big listing, a live run
        # still piling up steps - pushes its own header off the top, and
        # folding it back then means scrolling up to find that header. The bar
        # pins it while the body is on screen. Inside an expanded step it names
        # that step (file included) and folds the step; over the block as a
        # whole it summarises the turn and folds the whole block.
        self._step_marker = None
        self._sticky_target = None
        self.sticky_bar_button = QPushButton(self.viewport())
        self.sticky_bar_button.setObjectName("StickyBarButton")
        self.sticky_bar_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sticky_bar_button.setFixedHeight(28)
        self.sticky_bar_button.clicked.connect(self._collapse_sticky_target)
        self.sticky_bar_button.setVisible(False)
        theme_manager.theme_changed.connect(self._apply_sticky_bar_style)
        self._apply_sticky_bar_style()
        self.verticalScrollBar().valueChanged.connect(self._update_sticky_bar)
        logging.info("ChatWidget initialized.")

    def set_assistant_name(self, name: str):
        """Sets the display name for the assistant for subsequent messages."""
        if name:
            self.current_assistant_name = name

    def _new_document(self) -> QTextDocument:
        doc = QTextDocument(self)
        doc.setDocumentMargin(0)
        doc.setDefaultFont(self.chat_font)
        register_status_images(doc, self.theme_manager)
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

                if anchor and anchor.startswith("attachment-preview:"):
                    payload = unquote(anchor.split(":", 1)[1])
                    path, _, name = payload.partition("|")
                    if path:
                        self.attachment_preview_requested.emit(path, name)
                        return

                if anchor and anchor.startswith("retry:"):
                    self.retry_requested.emit()
                    return # Stop further processing

                if anchor and anchor.startswith("continue:"):
                    self.continue_requested.emit()
                    return # Stop further processing

                if anchor and anchor.startswith("suggest:"):
                    parts = anchor.split(':', 2)
                    if len(parts) == 3:
                        self._select_suggestion(parts[1], parts[2])
                        return

                if anchor and anchor.startswith("activity:"):
                    if self._handle_activity_anchor(
                            anchor, content_y=scroll_value + pos_in_viewport.y()):
                        return

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

    def _format_suggestions(self, msg: dict) -> str:
        """One muted row of clickable next-step suggestions."""
        items = msg.get('items') or []
        if not items:
            return ""
        meta = self.theme_manager.get_style("COLLAPSIBLE_META")
        accent = self.theme_manager.get_style("COLLAPSIBLE_TITLE")
        if msg.get('consumed'):
            joined = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(
                html.escape(item) for item in items)
            return (f'<div style="margin: 2px 0 6px 0; font-size: 92%; '
                    f'color: {meta};">Next steps: {joined}</div>')
        links = []
        for index, item in enumerate(items):
            links.append(
                f'<a href="suggest:{msg["id"]}:{index}" '
                f'style="color: {accent}; text-decoration: none; '
                f'font-weight: 600;">{html.escape(item)}</a>')
        return (f'<div style="margin: 2px 0 6px 0; font-size: 92%; '
                f'color: {meta};">Next steps: '
                f'{"&nbsp;&nbsp;·&nbsp;&nbsp;".join(links)}</div>')

    def _select_suggestion(self, msg_id: str, index: str) -> bool:
        """Send one clicked suggestion as the user's next message."""
        for msg in self.messages:
            if msg.get('type') != 'suggestions' or msg.get('id') != msg_id:
                continue
            if msg.get('consumed'):
                return False
            try:
                text = msg['items'][int(index)]
            except (KeyError, IndexError, ValueError):
                return False
            msg['consumed'] = True
            msg['doc'].setHtml(self._format_suggestions(msg))
            self._update_scroll_bar()
            self.viewport().update()
            self.suggestion_selected.emit(text)
            return True
        return False

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

    def wheelEvent(self, event):
        """Scrolls the transcript in pixels at native application speed.

        ``_update_scroll_bar`` keeps the range in pixels, so the base class's
        item-based stepping (3 px per notch via the system setting) crawls.
        High-resolution devices deliver ``pixelDelta``; classic wheels deliver
        ``angleDelta`` in 120ths of a notch, converted to text lines here.
        """
        bar = self.verticalScrollBar()
        pixel_delta = event.pixelDelta().y()
        if pixel_delta:
            bar.setValue(bar.value() - pixel_delta)
            event.accept()
            return

        angle_delta = event.angleDelta().y()
        if not angle_delta:
            super().wheelEvent(event)
            return

        lines = QApplication.wheelScrollLines()
        if lines == 0:
            event.accept()
            return
        if lines < 0:  # system configured "one page per notch"
            step_px = float(bar.pageStep())
        else:
            step_px = float(lines * self._wheel_px_per_line)

        self._wheel_remainder += angle_delta * step_px / 120.0
        step = int(self._wheel_remainder)
        if step:
            self._wheel_remainder -= step
            bar.setValue(bar.value() - step)
        event.accept()

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
        self._step_marker = None
        self._sticky_target = None
        if getattr(self, "sticky_bar_button", None) is not None:
            self.sticky_bar_button.setVisible(False)
        self._activity_timer.stop()
        self.selection_start = None
        self.selection_end = None
        self._update_scroll_bar()
        self.verticalScrollBar().setValue(0)
        self.viewport().update()

    def add_message(self, data, force_scroll=False, keep_stream=False, role=None):
        if role == 'user':
            # A user message splits the turn: the live block stays above it,
            # and the work that follows starts a new block below. Without the
            # seal the block kept growing and later steps appeared above the
            # message that caused them.
            self.seal_live_activity()
        was_at_bottom = self._is_at_bottom()
        streaming_msg = self._streaming_msg
        if not keep_stream:
            if streaming_msg is not None and streaming_msg.get('is_status'):
                # A placeholder "Thinking…" line must not linger as a stale
                # line once real content arrives; drop it instead of leaving
                # an orphaned document in the transcript.
                self.remove_message(streaming_msg)
            elif self.is_streaming:
                self.is_streaming = False
            self._streaming_msg = None

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
        elif isinstance(data, dict) and data.get('type') == 'suggestions':
            msg = {
                'type': 'suggestions',
                'id': f"suggestions-{uuid.uuid4().hex}",
                'items': [str(item) for item in (data.get('items') or [])],
                'consumed': bool(data.get('consumed')),
                'doc': doc,
            }
            doc.setHtml(self._format_suggestions(msg))
        elif isinstance(data, dict) and data.get('type') == 'activity':
            msg = {
                'type': 'activity',
                'id': f"activity-{uuid.uuid4().hex}",
                'title': data.get('title') or 'Working',
                'status': data.get('status') or 'running',
                'expanded': bool(data.get('expanded', True)),
                'finished_at': data.get('finished_at'),
                'rows': data.get('rows') or [],
                'doc': doc,
            }
            doc.setHtml(self.activity_formatter.format_block(msg))
            self._ensure_activity_timer()
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
        return msg

    def update_collapsible(self, msg, title: str, content_html: str) -> bool:
        """Updates a collapsible block in place, keeping its open/closed state."""
        if not isinstance(msg, dict) or msg.get('type') != 'collapsible':
            return False
        if not any(m is msg for m in self.messages):
            return False
        was_at_bottom = self._is_at_bottom()
        msg['title'] = title
        msg['full_content'] = content_html
        msg['full_content_html'] = content_html
        new_doc = self._new_document()
        new_doc.setHtml(self._format_collapsible_message(msg))
        msg['doc'] = new_doc
        self._update_scroll_bar()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)
        return True

    # --- Activity blocks (one turn's plan, thoughts, narration and steps) ---

    def _has_message(self, msg) -> bool:
        return any(m is msg for m in self.messages)

    @staticmethod
    def _activity_row(kind: str, **fields) -> dict:
        row = {'id': f"row-{uuid.uuid4().hex}", 'kind': kind}
        row.update(fields)
        return row

    def add_activity_block(self, title: str = "Working", expanded: bool = True):
        """Appends a live turn block; rows are added through the methods below."""
        was_at_bottom = self._is_at_bottom()
        doc = self._new_document()
        msg = {
            'type': 'activity',
            'id': f"activity-{uuid.uuid4().hex}",
            'title': title or "Working",
            'status': 'running',
            'expanded': bool(expanded),
            'finished_at': None,
            'rows': [],
            'doc': doc,
        }
        doc.setHtml(self.activity_formatter.format_block(msg))
        self.messages.append(msg)
        self._update_scroll_bar()
        self._ensure_activity_timer()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)
        return msg

    def activity_add_thought(self, msg, text: str, expanded: bool = False) -> bool:
        """Model reasoning inside the turn block, collapsed to one line."""
        if not self._has_message(msg):
            return False
        msg['rows'].append(self._activity_row(
            'thought', text=str(text or ''), expanded=bool(expanded)))
        self._render_activity(msg)
        return True

    def activity_add_note(self, msg, text: str) -> bool:
        """One-line narration that stays visible (not collapsible)."""
        if not self._has_message(msg):
            return False
        msg['rows'].append(self._activity_row('note', text=str(text or '')))
        self._render_activity(msg)
        return True

    def activity_add_step(self, msg, title: str, args: str = "",
                          detail_html: str | None = None, name: str = ""):
        """Starts a tool step row; returns the row for later updates."""
        if not self._has_message(msg):
            return None
        row = self._activity_row(
            'step', title=str(title or 'Step'), status='running',
            name=str(name or ''),
            args=str(args or ''), output='', detail_html=detail_html,
            expanded=False, args_expanded=False, output_expanded=False,
            job_id=None, log_path=None,
            started_at=time.monotonic(), finished_at=None)
        msg['rows'].append(row)
        self._render_activity(msg)
        self._ensure_activity_timer()
        return row

    def activity_update_step(self, msg, row, status: str | None = None,
                             title: str | None = None, args: str | None = None,
                             output: str | None = None,
                             detail_html: str | None = None,
                             job_id=None, log_path=None,
                             expanded: bool | None = None) -> bool:
        """Updates an existing step row, keeping the rest of the block intact."""
        if not self._has_message(msg) or not isinstance(row, dict):
            return False
        if status is not None:
            status = str(status)
            row['status'] = status
            if status != 'running' and row.get('finished_at') is None:
                row['finished_at'] = time.monotonic()
        if title is not None:
            row['title'] = str(title)
        if args is not None:
            row['args'] = str(args)
        if output is not None:
            row['output'] = str(output)
        if detail_html is not None:
            row['detail_html'] = detail_html
        if job_id is not None:
            row['job_id'] = job_id
        if log_path is not None:
            row['log_path'] = log_path
        if expanded is not None:
            row['expanded'] = bool(expanded)
        self._render_activity(msg)
        self._stop_activity_timer_if_idle()
        return True

    def finish_activity(self, msg, title: str | None = None,
                        status: str = "ok", collapse: str | None = None) -> bool:
        """Freezes the block header: final title and status.

        ``collapse`` picks the transcript treatment. ``"auto"`` folds the
        finished turn away unless the user opened the block by hand, a step
        failed without a later success, or a background job still runs;
        ``"always"`` folds regardless (session restore); None leaves the
        current open/closed state alone.
        """
        if not self._has_message(msg):
            return False
        msg['finished_at'] = time.monotonic()
        msg['status'] = status
        if title:
            msg['title'] = title
        # A stopped/finished task leaves no step visually running: the 1s
        # tick must not spin forever on a row whose result never arrives.
        # Background-job rows stay "running" (the job itself is alive) but
        # the finalized block no longer ticks.
        for row in msg.get('rows') or []:
            if (row.get('kind') == 'step' and row.get('status') == 'running'
                    and row.get('job_id') is None):
                row['status'] = 'stopped'
                row['finished_at'] = msg['finished_at']
        if collapse == 'always' or (
                collapse == 'auto' and self._can_auto_collapse(msg)):
            msg['expanded'] = False
        self._render_activity(msg)
        self._stop_activity_timer_if_idle()
        return True

    def _can_auto_collapse(self, msg: dict) -> bool:
        """Whether a just-finished turn may fold itself away.

        The user's manual open/close wins, a real failure with no later
        success stays visible, and work still in flight (a background job)
        keeps waiting for its result on screen.
        """
        if msg.get('user_toggled'):
            return False
        if self._has_running_step(msg):
            return False
        return not self._has_unresolved_failure(msg)

    @staticmethod
    def _has_unresolved_failure(msg: dict) -> bool:
        """True when the last failed step has no successful step after it."""
        failed = False
        for row in msg.get('rows') or []:
            if row.get('kind') != 'step':
                continue
            if row.get('status') in ('error', 'denied'):
                failed = True
            elif row.get('status') == 'ok':
                failed = False
        return failed

    def seal_live_activity(self) -> bool:
        """Closes live turn blocks at a transcript boundary (a user message).

        Steps that already happened stay in the sealed card above the message;
        whatever comes next opens a new card below it. A step still in flight
        is marked stopped here and gets its real result when it returns. The
        header must stop saying "Working": the card no longer grows, even
        though the tool it was waiting for may still finish.
        """
        sealed = False
        for msg in self.messages:
            if msg.get('type') != 'activity' or not self._activity_is_live(msg):
                continue
            running = any(
                row.get('kind') == 'step' and row.get('status') == 'running'
                and row.get('job_id') is None for row in msg.get('rows') or [])
            self.finish_activity(
                msg, title='Stopped' if running else 'Done', status='ok')
            sealed = True
        return sealed

    def _activity_is_live(self, msg: dict) -> bool:
        return msg.get('finished_at') is None

    @staticmethod
    def _has_running_step(msg: dict) -> bool:
        return any(row.get('kind') == 'step' and row.get('status') == 'running'
                   for row in msg.get('rows') or [])

    def _ensure_activity_timer(self):
        if not self._activity_timer.isActive():
            self._activity_timer.start()

    def _stop_activity_timer_if_idle(self):
        if not any(m.get('type') == 'activity' and self._activity_is_live(m)
                   and self._has_running_step(m) for m in self.messages):
            self._activity_timer.stop()

    def _render_activity(self, msg):
        """Replaces the block's document with a fresh render of its state."""
        if not self._has_message(msg):
            return
        was_at_bottom = self._is_at_bottom()
        try:
            rendered = self.activity_formatter.format_block(msg)
        except Exception as e:
            logging.warning(f"Activity block render failed for {msg.get('id')}: {e}")
            return
        new_doc = self._new_document()
        new_doc.setHtml(rendered)
        msg['doc'] = new_doc
        self._update_scroll_bar()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)

    def _activity_tick(self):
        live = [m for m in self.messages
                if m.get('type') == 'activity' and self._activity_is_live(m)
                and self._has_running_step(m)]
        if not live:
            self._activity_timer.stop()
            return
        was_at_bottom = self._is_at_bottom()
        for msg in live:
            try:
                new_doc = self._new_document()
                new_doc.setHtml(self.activity_formatter.format_block(msg))
                msg['doc'] = new_doc
            except Exception as e:
                logging.warning(
                    f"Activity tick render failed for {msg.get('id')}: {e}")
        self._update_scroll_bar()
        self.viewport().update()
        self._follow_if_at_bottom(was_at_bottom)

    def _handle_activity_anchor(self, anchor: str, content_y: float | None = None) -> bool:
        parts = anchor.split(':', 3)
        if len(parts) < 3:
            return False
        msg_id, kind = parts[1], parts[2]
        rest = parts[3] if len(parts) > 3 else ""
        msg = next((m for m in self.messages if m.get('type') == 'activity'
                    and m.get('id') == msg_id), None)
        if msg is None:
            return False
        if kind == 'toggle':
            msg['expanded'] = not bool(msg.get('expanded', True))
            # The user's own open/close choice outranks auto-collapse: a block
            # they opened by hand must not fold under their cursor later.
            msg['user_toggled'] = True
            self._render_activity(msg)
            return True
        if kind == 'row':
            for row in msg.get('rows') or []:
                if row.get('id') == rest:
                    before = self._document_height(msg)
                    row['expanded'] = not bool(row.get('expanded'))
                    self._track_step_toggle(msg, row, content_y)
                    self._render_activity(msg)
                    self._remeasure_step_growth(msg, row, before)
                    return True
            return False
        if kind in ('args', 'output'):
            for row in msg.get('rows') or []:
                if row.get('id') == rest:
                    key = f'{kind}_expanded'
                    before = self._document_height(msg)
                    row[key] = not bool(row.get(key))
                    self._render_activity(msg)
                    self._remeasure_step_growth(msg, row, before)
                    return True
            return False
        if kind == 'run':
            rows = msg.get('rows') or []
            start = next((i for i, row in enumerate(rows)
                          if row.get('id') == rest), None)
            if start is None:
                return False
            steps = steps_in_run(rows, start)
            if not steps:
                return False
            state = msg.setdefault('run_state', {})
            current = state.get(rest)
            if current is None:
                current = run_default_expanded(steps)
            state[rest] = not current
            self._render_activity(msg)
            return True
        if kind == 'job':
            try:
                self.job_stop_requested.emit(int(rest))
            except (TypeError, ValueError):
                return False
            return True
        if kind == 'log':
            path = unquote(rest)
            if path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
                return True
        return False

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
                if msg.get('type') == 'activity':
                    self._stop_activity_timer_if_idle()
                self._update_scroll_bar()
                self.viewport().update()
                return

    def scroll_to_bottom(self):
        """Public method to scroll the chat to the very bottom."""
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def _apply_scroll_to_bottom_style(self):
        self.scroll_to_bottom_button.setStyleSheet(
            self.theme_manager.get_style("SCROLL_BOTTOM_BUTTON_STYLE"))

    def _apply_sticky_bar_style(self):
        self.sticky_bar_button.setStyleSheet(
            self.theme_manager.get_style("STICKY_BAR_STYLE"))

    def _block_sticky_label(self, msg: dict) -> str:
        """Same text as the block header, so the bar is the header, pinned."""
        return block_summary(msg) or " ".join(str(msg.get("title") or "Working").split())

    @staticmethod
    def _step_file_name(row: dict) -> str:
        """File name a step works on, for the step bar's label."""
        try:
            data = json.loads(str(row.get("args") or ""))
        except (TypeError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        candidate = (data.get("path") or data.get("file_path")
                     or data.get("file"))
        if not candidate:
            paths = data.get("paths")
            if isinstance(paths, list) and paths:
                candidate = paths[0]
        if not isinstance(candidate, str) or not candidate.strip():
            return ""
        return candidate.replace("\\", "/").rstrip("/").split("/")[-1]

    def _step_sticky_label(self, row: dict) -> str:
        title = " ".join(str(row.get("title") or "step").split())
        name = self._step_file_name(row)
        label = f"{title} · {name}" if name and name not in title else title
        return label if len(label) <= 60 else label[:59] + "…"

    def _block_at_scroll(self):
        """The expanded activity block whose body the viewport top is inside."""
        value = self.verticalScrollBar().value()
        top = 0.0
        found = None
        for msg in self.messages:
            doc = msg.get("doc")
            height = doc.size().height() if doc is not None else 0.0
            if (msg.get("type") == "activity" and msg.get("expanded", True)
                    and top + 30 < value < top + height):
                found = (msg, top)
            top += height + self.document_margin
        return found

    @staticmethod
    def _document_height(msg: dict) -> float:
        doc = msg.get("doc")
        return doc.size().height() if doc is not None else 0.0

    def _mark_step_position(self, msg: dict, row: dict, content_y: float):
        """Remembers where an expanded step's header sits in the transcript."""
        y = max(0, int(content_y))
        self._step_marker = {
            "msg": msg,
            "row": row,
            "y": y,
            "label": self._step_sticky_label(row),
            "end": y + int(row.get("growth") or 0) + 28,
        }
        self._update_sticky_bar()

    def _track_step_toggle(self, msg: dict, row: dict, content_y: float | None):
        """Arms or disarms the step bar for a step the reader opened."""
        marker = self._step_marker
        if not row.get("expanded"):
            if marker and marker["row"] is row:
                self._step_marker = None
            return
        if content_y is not None:
            self._mark_step_position(msg, row, content_y)

    def _remeasure_step_growth(self, msg: dict, row: dict, before: float):
        """Keeps the step bar's window in step with that step's height.

        The bar must vanish exactly when the step's own content scrolls off
        the top, so the height a toggle added is measured from the rendered
        document instead of guessed from the block model.
        """
        if not row.get("expanded"):
            row["growth"] = 0
            self._update_sticky_bar()
            return
        delta = self._document_height(msg) - before
        row["growth"] = max(0, int((row.get("growth") or 0) + delta))
        marker = self._step_marker
        if marker and marker["row"] is row:
            marker["end"] = marker["y"] + row["growth"] + 28
        self._update_sticky_bar()

    def _step_row_on_screen(self, msg: dict, row: dict) -> bool:
        """Whether the step's row is actually rendered (its run is open)."""
        rows = msg.get("rows") or []
        start = next((i for i, r in enumerate(rows) if r is row), None)
        if start is None:
            return False
        steps = steps_in_run(rows, start)
        if len(steps) <= 1:
            # A lone step is drawn as a plain row, never inside a run.
            return True
        state = (msg.get("run_state") or {}).get(steps[0].get("id"))
        return run_default_expanded(steps) if state is None else bool(state)

    def _sticky_target_at_scroll(self):
        """What the bar folds right now: the open step, else the whole block."""
        value = self.verticalScrollBar().value()
        marker = self._step_marker
        if (marker and self._has_message(marker["msg"])
                and marker["msg"].get("expanded", True)
                and marker["row"].get("expanded")
                and self._step_row_on_screen(marker["msg"], marker["row"])
                and marker["y"] + 24 < value < marker["end"]):
            return ("step", marker["label"],
                    (marker["msg"], marker["row"], marker["y"]))
        found = self._block_at_scroll()
        if found:
            msg, top = found
            return ("block", self._block_sticky_label(msg), (msg, top))
        return None

    def _collapse_sticky_target(self):
        """Folds what the bar names, without scrolling back to its header."""
        target = self._sticky_target
        if not target:
            return
        kind, _label, payload = target
        self._sticky_target = None
        self.sticky_bar_button.setVisible(False)
        if kind == "step":
            msg, row, y = payload
            row["expanded"] = False
            row["growth"] = 0
            if self._step_marker and self._step_marker["row"] is row:
                self._step_marker = None
            if self._has_message(msg):
                self._render_activity(msg)
                self.verticalScrollBar().setValue(max(0, int(y) - 8))
            return
        msg, top = payload
        msg["expanded"] = False
        # A fold the reader asked for outranks the automatic ones.
        msg["user_toggled"] = True
        if self._has_message(msg):
            self._render_activity(msg)
            self.verticalScrollBar().setValue(max(0, int(top) - 8))

    def _update_sticky_bar(self, *args):
        """Pins the header the reader has scrolled past, step or block alike."""
        button = getattr(self, "sticky_bar_button", None)
        if button is None:
            return
        target = self._sticky_target_at_scroll()
        if target is None:
            self._sticky_target = None
            button.setVisible(False)
            return
        self._sticky_target = target
        kind, label, _payload = target
        text = f"▾  {label}"
        if button.text() != text:
            button.setText(text)
        tooltip = "Collapse this step" if kind == "step" else "Collapse this block"
        if button.toolTip() != tooltip:
            button.setToolTip(tooltip)
        x_offset, text_width = self._content_geometry()
        if text_width <= 0:
            return
        button.setGeometry(x_offset, 0, text_width, 28)
        button.setVisible(True)
        button.raise_()

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
        label = (status_text or "Thinking").strip().rstrip(".…").strip()
        label = html.escape(label or "Thinking")
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
        self._update_sticky_bar()

    @staticmethod
    def _block_has_image(block) -> bool:
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid() and fragment.charFormat().isImageFormat():
                return True
            it += 1
        return False

    def _user_bubble_rect(self, doc):
        """Rect of the right-aligned bubble cell inside a user message doc.

        The message HTML puts the prompt into a right-aligned table cell;
        Qt rich text has no border-radius, so the background is painted by
        hand under the cell's text blocks. Attachment thumbnails sit above
        the bubble, so image-only blocks are excluded from the background.
        """
        table = next((frame for frame in doc.rootFrame().childFrames()
                      if isinstance(frame, QTextTable)), None)
        if table is None or table.columns() < 2:
            return None
        cell = table.cellAt(0, table.columns() - 1)
        layout = doc.documentLayout()
        end_pos = cell.lastCursorPosition().position()
        block = doc.findBlock(cell.firstCursorPosition().position())
        rect = None
        while block.isValid() and block.position() <= end_pos:
            if not self._block_has_image(block) and block.text().strip():
                block_rect = layout.blockBoundingRect(block)
                rect = block_rect if rect is None else rect.united(block_rect)
            block = block.next()
        if rect is None:
            return None
        pad = USER_BUBBLE_PADDING
        return rect.adjusted(-pad, -pad, pad, pad)

    def _paint_activity_card(self, painter, width: float, height: float):
        """Card fill and border for an activity block.

        Painted here instead of in the block's HTML: Qt's rich text does not
        carry a div background into nested block rows, which left the rows
        sitting on the chat background.
        """
        bg = self.theme_manager.get_style("COLLAPSIBLE_BG")
        border = self.theme_manager.get_style("COLLAPSIBLE_BORDER")
        rect = QRectF(0, 0, width, height)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        if bg:
            painter.setBrush(QColor(bg))
            painter.drawRoundedRect(rect, 6, 6)
        if border:
            pen = QPen(QColor(border))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 6, 6)
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        bg_color = self.theme_manager.get_style("CHAT_BACKGROUND")
        if bg_color:
            painter.fillRect(event.rect(), QColor(bg_color))
        bubble_color = self.theme_manager.get_style("USER_MESSAGE_BG")
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
                if msg.get('type') == 'activity':
                    self._paint_activity_card(painter, text_width, doc_height)
                if msg.get('role') == 'user' and bubble_color:
                    bubble_rect = self._user_bubble_rect(doc)
                    if bubble_rect is not None:
                        painter.setRenderHint(
                            QPainter.RenderHint.Antialiasing, True)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QColor(bubble_color))
                        painter.drawRoundedRect(
                            bubble_rect, USER_BUBBLE_RADIUS, USER_BUBBLE_RADIUS)
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
        if self.messages:
            self._excerpt_timer.start()

    def _activity_column_width(self) -> int:
        return self._content_geometry()[1]

    def _measure_activity_text(self, text: str, bold: bool = False) -> int:
        font = QFont(self.chat_font)
        font.setBold(bool(bold))
        return QFontMetrics(font).horizontalAdvance(str(text))

    def _rerender_excerpts(self):
        """Re-clips one-line thought rows after the text column changed width."""
        width = self._activity_column_width()
        last = int(getattr(self.activity_formatter, "last_width", 0) or 0)
        if width <= 0 or last <= 0 or abs(width - last) < 24:
            return
        touched = False
        for msg in list(self.messages):
            if msg.get('type') != 'activity':
                continue
            if not any(row.get('kind') == 'thought'
                       for row in msg.get('rows') or []):
                continue
            try:
                msg['doc'].setHtml(self.activity_formatter.format_block(msg))
            except Exception as e:
                logging.warning(f"Thought excerpt re-render failed: {e}")
                continue
            touched = True
        if touched:
            self._update_scroll_bar()
            self.viewport().update()
