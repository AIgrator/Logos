# src/ui/terminal_html_formatter.py
"""
This module is responsible for generating all HTML content for the terminal UI,
ensuring a clean separation of logic from presentation. It supports theming
for light and dark modes by integrating with the ThemeManager.
"""
import base64
import html

from PySide6.QtCore import QBuffer, QIODevice, QSize
import qtawesome as qta

class TerminalHtmlFormatter:
    """
    Generates themed HTML for terminal mode UI components by using a ThemeManager.
    """
    ICON_SIZE = 10  # Default icon size, matching font height

    def __init__(self, theme_manager):
        self.theme_manager = theme_manager

    def _get_style(self, key):
        """Fetches a style property from the ThemeManager for the current theme."""
        return self.theme_manager.get_style(key)

    def _icon_to_data_uri(self, icon_name: str, color: str, size: int = ICON_SIZE) -> str:
        """Converts a qtawesome icon into a base64-encoded data URI for embedding in HTML."""
        try:
            icon = qta.icon(icon_name, color=color)
            pixmap = icon.pixmap(QSize(size, size))
            buffer = QBuffer()
            buffer.open(QIODevice.WriteOnly)
            pixmap.save(buffer, "PNG")
            encoded_data = base64.b64encode(buffer.data()).decode('utf-8')
            return f"data:image/png;base64,{encoded_data}"
        except Exception:
            return "" # Return empty string if icon generation fails

    def _get_icon_style(self, size: int = ICON_SIZE) -> str:
        """Returns a standardized CSS style string for icons."""
        return f"vertical-align: middle; height: {size}px; width: {size}px;"

    def format_container_start(self, path: str) -> str:
        """Returns the starting HTML for a unified file operation block."""
        icon_color = self._get_style('TERMINAL_HEADER_COLOR')
        icon_data_uri = self._icon_to_data_uri('fa5s.pencil-alt', icon_color, self.ICON_SIZE)
        icon_style = self._get_icon_style(self.ICON_SIZE)

        return f"""
        <div style="background-color: {self._get_style('TERMINAL_CONTAINER_BG')}; border-left: 3px solid {self._get_style('TERMINAL_CONTAINER_BORDER')}; padding: 10px; margin-top: 5px; margin-bottom: 5px;">
            <p style="margin: 0; font-weight: bold; color: {icon_color};">
                <img src="{icon_data_uri}" style="{icon_style}">&nbsp;Modifying File
            </p>
            <p style="margin: 0; color: {self._get_style('TERMINAL_SUBHEADER_COLOR')};"><b>File:</b> <code>{path}</code></p>
        """

    def format_container_end(self) -> str:
        """Returns the closing tag for the container."""
        return "</div>"

    def format_write_chunk(self, content: str) -> str:
        """Formats the HTML for a file write operation."""
        icon_color = self._get_style('TERMINAL_WRITE_HEADER_COLOR')
        icon_data_uri = self._icon_to_data_uri('fa5s.file-alt', icon_color, self.ICON_SIZE)
        icon_style = self._get_icon_style(self.ICON_SIZE)

        lines = content.split('\n')
        numbered_lines = []
        for i, line in enumerate(lines, 1):
            escaped_line = html.escape(line)
            numbered_lines.append(f'<span style="color: {self._get_style("TERMINAL_LINE_NUM_COLOR")}; -webkit-user-select: none; user-select: none;">{i: >3}</span>&nbsp;&nbsp;{escaped_line}')
        numbered_content = "\n".join(numbered_lines)

        return f"""
        <hr style="border: 1px solid {self._get_style('TERMINAL_DIVIDER_COLOR')}; margin-top: 5px; margin-bottom: 5px;">
        <p style="margin: 0; font-weight: bold; color: {icon_color};">
            <img src="{icon_data_uri}" style="{icon_style}">&nbsp;Writing new file
        </p>
        <pre style="background-color: {self._get_style('TERMINAL_PRE_BG')}; padding: 8px; border-radius: 4px; color: {self._get_style('TERMINAL_PRE_COLOR')}; white-space: pre-wrap; word-wrap: break-word;"><code>{numbered_content}</code></pre>
        """

    def format_replace_chunk(self, old_lines: list[str], new_lines: list[str], start_line: int) -> str:
        """Formats the HTML for a file replace (diff) operation."""
        old_lines_html = []
        if start_line != -1:
            for i, line in enumerate(old_lines):
                old_lines_html.append(f'<span style="color: {self._get_style("TERMINAL_LINE_NUM_COLOR")}; -webkit-user-select: none; user-select: none;">{start_line + i: >3}&nbsp;-&nbsp;</span><span style="background-color: {self._get_style("TERMINAL_DIFF_REMOVE_BG")};">{html.escape(line)}</span>')
        else:
            for line in old_lines:
                old_lines_html.append(f'<span style="color: {self._get_style("TERMINAL_LINE_NUM_COLOR")}; -webkit-user-select: none; user-select: none;">-&nbsp;</span><span style="background-color: {self._get_style("TERMINAL_DIFF_REMOVE_BG")};">{html.escape(line)}</span>')

        new_lines_html = []
        if start_line != -1:
            for i, line in enumerate(new_lines):
                new_lines_html.append(f'<span style="color: {self._get_style("TERMINAL_LINE_NUM_COLOR")}; -webkit-user-select: none; user-select: none;">{start_line + i: >3}&nbsp;+&nbsp;</span><span style="background-color: {self._get_style("TERMINAL_DIFF_ADD_BG")};">{html.escape(line)}</span>')
        else:
            for line in new_lines:
                new_lines_html.append(f'<span style="color: {self._get_style("TERMINAL_LINE_NUM_COLOR")}; -webkit-user-select: none; user-select: none;">+&nbsp;</span><span style="background-color: {self._get_style("TERMINAL_DIFF_ADD_BG")};">{html.escape(line)}</span>')

        return f"""
        <hr style="border: 1px solid {self._get_style('TERMINAL_DIVIDER_COLOR')}; margin-top: 5px; margin-bottom: 5px;">
        <pre style="background-color: {self._get_style('TERMINAL_PRE_BG')}; padding: 8px; border-radius: 4px; color: {self._get_style('TERMINAL_PRE_COLOR')}; white-space: pre-wrap; word-wrap: break-word;"><code>{"<br>".join(old_lines_html)}<br>{"<br>".join(new_lines_html)}</code></pre>
        """

    def format_read_file(self, path: str) -> str:
        """Formats the HTML for a read_file action."""
        return f"""
        <div style="background-color: {self._get_style('TERMINAL_CONTAINER_BG')}; border: 1px solid {self._get_style('TERMINAL_READ_FILE_BORDER')}; border-radius: 4px; padding: 10px; margin-top: 5px; margin-bottom: 5px;">
            <p style="margin: 0; color: {self._get_style('TERMINAL_SUBHEADER_COLOR')};">
                <span style="font-weight: bold; color: {self._get_style('TERMINAL_READ_FILE_ICON')};">✓</span>
                ReadFile ...<code>{path}</code>
            </p>
        </div>
        """

