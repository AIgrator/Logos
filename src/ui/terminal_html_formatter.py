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

from .code_highlight import highlight_code
from .theme_manager import DARK_THEME

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
        icon_color = self._get_style('COLLAPSIBLE_TITLE')
        icon_data_uri = self._icon_to_data_uri('fa5s.pencil-alt', icon_color, self.ICON_SIZE)
        icon_style = self._get_icon_style(self.ICON_SIZE)

        return f"""
        <div style="background-color: {self._get_style('COLLAPSIBLE_BG')}; border: 1px solid {self._get_style('COLLAPSIBLE_BORDER')}; border-left: 3px solid {icon_color}; padding: 10px; margin-top: 6px; margin-bottom: 6px;">
            <p style="margin: 0; font-weight: bold; color: {icon_color};">
                <img src="{icon_data_uri}" style="{icon_style}">&nbsp;Editing File
            </p>
            <p style="margin: 0; color: {self._get_style('COLLAPSIBLE_META')};">File: <code>{html.escape(str(path))}</code></p>
        """

    def format_container_end(self) -> str:
        """Returns the closing tag for the container."""
        return "</div>"

    def format_write_chunk(self, content: str) -> str:
        """Formats the HTML for a file write operation."""
        icon_color = self._get_style('TERMINAL_WRITE_HEADER_COLOR')
        icon_data_uri = self._icon_to_data_uri('fa5s.file-alt', icon_color, self.ICON_SIZE)
        icon_style = self._get_icon_style(self.ICON_SIZE)

        return f"""
        <hr style="border: 1px solid {self._get_style('TERMINAL_DIVIDER_COLOR')}; margin-top: 5px; margin-bottom: 5px;">
        <p style="margin: 0; font-weight: bold; color: {icon_color};">
            <img src="{icon_data_uri}" style="{icon_style}">&nbsp;Writing new file
        </p>
        {self.format_code_content(content)}
        """

    def format_read_files_content(self, result: str) -> str:
        """Render read_files output as one highlighted section per file.

        The tool returns a plain-text concatenation with marker lines. Those
        markers are useful for the model/log, but in the UI they become file
        headers, and each file body is syntax-highlighted separately.
        """
        lines = str(result).splitlines()
        sections = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith('--- START OF FILE: ') and line.endswith(' ---'):
                path = line[len('--- START OF FILE: '):-len(' ---')]
                i += 1
                body = []
                end_marker = f'--- END OF FILE: {path} ---'
                while i < len(lines) and lines[i] != end_marker:
                    body.append(lines[i])
                    i += 1
                i += 1
                body_text = "\n".join(body)
                line_count = len(body_text.splitlines())
                header = (f'<div style="color: {self._get_style("COLLAPSIBLE_TITLE")}; '
                          f'font-weight: 600; margin: 10px 0 4px 0;">'
                          f'{html.escape(path)} <span style="color: '
                          f'{self._get_style("COLLAPSIBLE_META")}; font-weight: 400;">'
                          f'· {line_count} line(s)</span></div>')
                sections.append(header + self.format_code_content(body_text, path))
            elif line.startswith('--- ERROR READING FILE: ') and line.endswith(' ---'):
                path = line[len('--- ERROR READING FILE: '):-len(' ---')]
                i += 1
                body = []
                while i < len(lines) and lines[i] != '--- END OF ERROR ---':
                    body.append(lines[i])
                    i += 1
                i += 1
                sections.append(
                    f'<div style="color: {self._get_style("ERROR_COLOR")}; '
                    'padding: 6px 0;">'
                    f'{html.escape(path)}: {html.escape(chr(10).join(body))}</div>'
                )
            else:
                i += 1

        if not sections:
            return self.format_code_content(str(result), "")
        return "\n".join(sections)

    def format_list_content(self, items, ordered: bool = False,
                            empty_text: str = "(no items)") -> str:
        """Render tool result rows as a real list instead of one long block."""
        rows = [str(item) for item in (items or []) if str(item).strip()]
        if not rows:
            return (f'<div style="color: {self._get_style("COLLAPSIBLE_META")};">'
                    f'{html.escape(empty_text)}</div>')
        tag = "ol" if ordered else "ul"
        list_items = "".join(
            f'<li style="margin: 2px 0;">{html.escape(row)}</li>' for row in rows
        )
        return (
            f'<{tag} style="margin: 0; padding: 8px 12px 8px 28px; '
            f'background-color: {self._get_style("CODE_BG")}; '
            f'border: 1px solid {self._get_style("CODE_BORDER")}; '
            f'color: {self._get_style("TERMINAL_PRE_COLOR")}; '
            f'line-height: 1.35;">{list_items}</{tag}>'
        )

    def _code_box(self, inner_html: str, color: str | None = None) -> str:
        """Framed code block.

        A one-cell table, not a bare <pre>: Qt's rich text ignores a pre's
        background, border and padding, so the output used to sit naked on
        the card. Table cell background and cellpadding render solidly.
        """
        return (
            f'<table width="100%" cellspacing="0" cellpadding="8" '
            f'bgcolor="{self._get_style("CODE_BG")}" '
            f'style="border: 1px solid {self._get_style("CODE_BORDER")}; '
            'margin-top: 2px;"><tr><td>'
            f'<pre style="margin: 0; color: '
            f'{color or self._get_style("TERMINAL_PRE_COLOR")}; '
            'white-space: pre-wrap; word-wrap: break-word;">'
            f"<code>{inner_html}</code></pre></td></tr></table>"
        )

    def _callout(self, text_html: str, accent: str, bg: str) -> str:
        """Left-accent note strip.

        Qt draws no border-left on rich-text blocks and ignores padding, so
        the strip is a borderless table: accent cell plus tinted text cell.
        """
        return (
            '<table width="100%" cellspacing="0" cellpadding="0" '
            'style="margin: 0 0 8px 0;"><tr>'
            f'<td width="3" bgcolor="{accent}">&nbsp;</td>'
            f'<td bgcolor="{bg}"><div style="color: {accent}; '
            f'margin: 6px 8px;">{text_html}</div></td></tr></table>'
        )

    def format_code_content(self, content: str, path: str = "",
                            color: str | None = None) -> str:
        """Return syntax-highlighted HTML for file content or a command preview."""
        dark = self.theme_manager.get_current_theme_name() == DARK_THEME
        rendered, _truncated = highlight_code(content, path, dark=dark)
        return self._code_box(rendered, color=color)

    def format_plain_content(self, content: str, color: str | None = None) -> str:
        """Plain monospace block for logs and shell output (no lexer guessing)."""
        return self._code_box(html.escape(str(content)), color=color)

    def format_shell_block(self, command: str, stdout: str = "", stderr: str = "",
                           exit_note: str = "", output_file: str = "",
                           cwd: str = "", show_output: bool = True) -> str:
        """Render one shell call as command + labelled output, not a raw dump."""
        meta = self._get_style("COLLAPSIBLE_META")
        label = (f'color: {meta}; font-size: 90%; font-weight: 600; '
                 'margin: 10px 0 4px 0;')
        parts = []
        if cwd:
            parts.append(
                f'<div style="color: {meta}; font-size: 90%; margin-bottom: 6px; '
                'word-wrap: break-word;">'
                f'Working directory: <code>{html.escape(str(cwd))}</code></div>'
            )
        if exit_note:
            parts.append(self._callout(
                html.escape(str(exit_note)),
                self._get_style("WARNING_COLOR"),
                self._get_style("ACTION_SUMMARY_BG")))
        parts.append(f'<div style="{label}">Command</div>')
        parts.append(self.format_code_content(f"$ {command}", "command.sh"))
        if show_output:
            clean_stdout = str(stdout or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
            parts.append(f'<div style="{label}">Output</div>')
            parts.append(self.format_plain_content(clean_stdout or "(no output)"))
        clean_stderr = str(stderr or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
        if clean_stderr:
            parts.append(
                f'<div style="color: {self._get_style("ERROR_COLOR")}; '
                'font-size: 90%; font-weight: 600; margin: 10px 0 4px 0;">'
                'System note</div>'
            )
            parts.append(self.format_plain_content(
                clean_stderr, color=self._get_style("ERROR_COLOR")))
        if output_file:
            parts.append(
                f'<div style="color: {meta}; font-size: 90%; margin-top: 8px; '
                'word-wrap: break-word;">'
                f'Full output: <code>{html.escape(str(output_file))}</code></div>'
            )
        return "\n".join(parts)

    def format_diff_content(self, old_lines: list[str], new_lines: list[str], start_line: int) -> str:
        """Return just the coloured diff body, suitable for a collapsible block."""
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

        return self._code_box(
            f'{"<br>".join(old_lines_html)}<br>{"<br>".join(new_lines_html)}')

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
                ReadFile ...<code>{html.escape(str(path))}</code>
            </p>
        </div>
        """

