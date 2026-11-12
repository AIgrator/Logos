"""The slash-command palette above the composer.

Typing ``/`` at the start of a message opens it, exactly like the command
palettes of other coding agents. The keyboard stays in the composer
(``ExpandingTextEdit`` forwards Up/Down/Tab/Enter), so the palette never takes
focus and the caret never leaves the input.

A row is the command name in a monospace face plus its one-line summary; the
delegate paints both with the active theme's colors. While a command's argument
is being completed the row shows the whole line it will insert (``/help
memory``), so the palette never looks like it is replacing the command typed.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import (
    QFrame, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate,
)


class _CommandDelegate(QStyledItemDelegate):
    """One row: ``/name`` on the left, the summary muted on the right."""

    def __init__(self, view: CommandPalette):
        super().__init__(view)
        self._view = view
        self._name_font = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont)
        self._name_font.setBold(True)
        self._summary_font = QFont(view.font())
        self._summary_font.setPointSizeF(
            max(8.0, view.font().pointSizeF() - 0.5))

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), CommandPalette.ROW_HEIGHT)

    def paint(self, painter, option, index):
        view = self._view
        painter.save()
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            background = view.selection_bg
        elif hovered:
            background = view.hover_bg
        else:
            background = view.row_bg
        painter.fillRect(rect, QColor(background))

        content = rect.adjusted(12, 0, -12, 0)
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        summary = str(index.data(Qt.ItemDataRole.UserRole) or "")
        name_color = QColor(view.selection_text if selected else view.text_color)
        summary_color = QColor(
            view.selection_text if selected else view.muted_color)

        painter.setFont(self._name_font)
        painter.setPen(name_color)
        name_width = painter.fontMetrics().horizontalAdvance(name)
        painter.drawText(
            content, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            name)

        painter.setFont(self._summary_font)
        painter.setPen(summary_color)
        left = content.left() + name_width + 16
        summary_rect = QRect(
            left, content.top(), max(0, content.right() - left),
            content.height())
        elided = painter.fontMetrics().elidedText(
            summary, Qt.TextElideMode.ElideRight, summary_rect.width())
        painter.drawText(
            summary_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        painter.restore()


class CommandPalette(QListWidget):
    """A focus-free list of slash commands, shown above the input."""

    ROW_HEIGHT = 34
    MAX_ROWS = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setItemDelegate(_CommandDelegate(self))
        self.row_bg = "#ffffff"
        self.hover_bg = "#f0f0f0"
        self.selection_bg = "#2a82da"
        self.selection_text = "#ffffff"
        self.text_color = "#000000"
        self.muted_color = "#57606a"
        self.border_color = "#d0d7de"

    def set_theme(self, theme_manager):
        """Takes the row colors from the active theme and repaints the list."""
        try:
            self.row_bg = theme_manager.get_style("COLLAPSIBLE_BG")
            self.border_color = theme_manager.get_style("COLLAPSIBLE_BORDER")
            self.muted_color = theme_manager.get_style("COLLAPSIBLE_META")
            self.text_color = theme_manager.get_style("ICON_COLOR")
        except Exception:
            pass
        palette = self.palette()
        self.hover_bg = palette.color(
            QPalette.ColorRole.AlternateBase).name()
        self.selection_bg = palette.color(QPalette.ColorRole.Highlight).name()
        # The dark palette marks highlighted text black, which disappears on
        # its own blue selection; pick the side with the real contrast.
        selection = QColor(self.selection_bg)
        luminance = (0.299 * selection.red() + 0.587 * selection.green()
                     + 0.114 * selection.blue())
        self.selection_text = "#000000" if luminance > 150 else "#ffffff"
        self.setStyleSheet(
            "QListWidget {"
            f" background: {self.row_bg};"
            f" border: 1px solid {self.border_color};"
            " padding: 4px; outline: none; }")
        self.viewport().update()

    def set_commands(self, commands, prefix: str = ""):
        """Fills the list; ``prefix`` shows the argument a row completes.

        ``prefix`` is the command already typed before the argument
        (``/help``), so a row reads ``/help memory`` - exactly the text ``Tab``
        inserts - instead of a bare ``/memory`` that looks like a replacement.
        """
        self.clear()
        for command in commands:
            label = f"{prefix} {command.name}" if prefix else command.slash
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, command.summary)
            item.setData(Qt.ItemDataRole.UserRole + 1, command)
            item.setToolTip(command.summary)
            self.addItem(item)
        self.setCurrentRow(0)

    def current_command(self):
        item = self.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole + 1)

    def step(self, delta: int):
        """Moves the highlight with wraparound, like every command palette."""
        count = self.count()
        if not count:
            return
        self.setCurrentRow((self.currentRow() + delta) % count)

    def preferred_height(self) -> int:
        rows = min(self.count(), self.MAX_ROWS)
        return rows * self.ROW_HEIGHT + 10
