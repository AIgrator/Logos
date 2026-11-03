# src/ui/terminal_html_formatter.py
"""
This module is responsible for generating all HTML content for the terminal UI,
ensuring a clean separation of logic from presentation. It supports theming
for light and dark modes by integrating with the ThemeManager.
"""
import base64
import html
import itertools
import os
import re

from PySide6.QtCore import QBuffer, QIODevice, QSize
import qtawesome as qta

from .code_highlight import (MAX_HIGHLIGHT_LINES, highlight_code,
                             is_html_filename, split_highlighted_lines)
from .theme_manager import DARK_THEME

# One numbered line of a ranged read_files section: "12\tcode" (current) or
# "12: code" (journals written before the tab separator).
_NUMBERED_LINE_RE = re.compile(r'^(?P<num>\d+)(?::|\t) ?(?P<text>.*)$')
# Opening/closing tags of HTML blocks whose content gets its own lexer.
_EMBEDDED_TAG_RE = re.compile(r'<(/?)(style|script)\b[^>]*>', re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)
# A ranged read never scans further than this to find its HTML context.
_CONTEXT_SCAN_LINES = 20000

class TerminalHtmlFormatter:
    """
    Generates themed HTML for terminal mode UI components by using a ThemeManager.
    """
    ICON_SIZE = 10  # Default icon size, matching font height

    def __init__(self, theme_manager, base_dir=None):
        # ``base_dir`` resolves read_files paths for HTML-context detection;
        # it may be a getter because the project root can change in place.
        self.theme_manager = theme_manager
        self.base_dir = base_dir

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
                label = line[len('--- START OF FILE: '):-len(' ---')]
                path = label
                range_note = ''
                match = re.match(
                    r'^(?P<path>.+?) \(lines (?P<first>\d+)-(?P<last>\d+) of (?P<total>\d+)\)$',
                    label)
                if match:
                    path = match.group('path')
                    range_note = (f'lines {match.group("first")}-'
                                  f'{match.group("last")} of {match.group("total")}')
                i += 1
                body = []
                end_marker = f'--- END OF FILE: {path} ---'
                while i < len(lines) and lines[i] != end_marker:
                    body.append(lines[i])
                    i += 1
                i += 1
                body_text = "\n".join(body)
                line_count = len(body_text.splitlines())
                meta_text = range_note or f'{line_count} line(s)'
                header = (f'<div style="color: {self._get_style("COLLAPSIBLE_TITLE")}; '
                          f'font-weight: 600; margin: 10px 0 4px 0;">'
                          f'{html.escape(path)} <span style="color: '
                          f'{self._get_style("COLLAPSIBLE_META")}; font-weight: 400;">'
                          f'· {meta_text}</span></div>')
                if match:
                    first = int(match.group('first'))
                    sections.append(header + self._numbered_code_block(
                        body, path, first, self._embedded_context(path, first)))
                else:
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

    def _embedded_context(self, path: str, first: int) -> str | None:
        """Which embedded HTML block ('style' or 'script') holds ``first``.

        A slice that starts inside <style> or <script> carries no opening tag,
        so the HTML lexer would show it as plain text. Knowing the surrounding
        block lets the numbered block re-wrap the slice for highlighting.
        """
        if first <= 1 or first - 1 > _CONTEXT_SCAN_LINES:
            return None
        if not path or not is_html_filename(path):
            return None
        base_dir = self.base_dir() if callable(self.base_dir) else self.base_dir
        if not os.path.isabs(path):
            if not base_dir:
                return None
            path = os.path.join(base_dir, path)
        try:
            with open(path, encoding='utf-8', errors='replace') as handle:
                prefix = "".join(itertools.islice(handle, first - 1))
        except (OSError, ValueError):
            return None
        prefix = _HTML_COMMENT_RE.sub("", prefix)
        context = None
        for match in _EMBEDDED_TAG_RE.finditer(prefix):
            context = None if match.group(1) else match.group(2).lower()
        return context

    def _numbered_code_block(self, body: list[str], path: str, first: int,
                             context: str | None = None) -> str:
        """Code block with a line-number gutter, highlighted without prefixes.

        The numbers are stripped before highlighting (a numeric prefix breaks
        the lexer) and rendered as a right-aligned gutter. Each line is its
        own table row, so a wrapped long line cannot push the next number
        beside its continuation. A body that is not a clean numbered list,
        such as an older journal, falls back to the plain code block.
        """
        entries = []
        for index, line in enumerate(body):
            match = _NUMBERED_LINE_RE.match(line)
            if not match or int(match.group('num')) != first + index:
                return self.format_code_content("\n".join(body), path)
            entries.append((match.group('num'), match.group('text')))
        width = max((len(num) for num, _ in entries), default=1)
        dark = self.theme_manager.get_current_theme_name() == DARK_THEME
        # A slice taken inside <style>/<script> is wrapped in its opening and
        # closing tag for the lexer; those two extra lines are dropped below.
        wrapped = context in ('style', 'script')
        limit = MAX_HIGHLIGHT_LINES - (2 if wrapped else 0)
        hidden = max(0, len(entries) - limit)
        shown = entries[:limit]
        code = "\n".join(text for _, text in shown)
        if wrapped:
            rendered, _ = highlight_code(f"<{context}>\n{code}\n</{context}>",
                                         path, dark=dark)
            code_lines = split_highlighted_lines(rendered)[1:-1]
        else:
            rendered, _ = highlight_code(code, path, dark=dark)
            code_lines = split_highlighted_lines(rendered)
        if len(code_lines) != len(shown):
            return self.format_code_content("\n".join(body), path)
        meta = self._get_style("COLLAPSIBLE_META")
        pre_color = self._get_style("TERMINAL_PRE_COLOR")
        rows = "".join(
            '<tr>'
            f'<td width="1%" valign="top"><pre style="margin: 0; color: {meta};">'
            f'{num.rjust(width)}&nbsp;&nbsp;</pre></td>'
            f'<td width="99%" valign="top"><pre style="margin: 0; '
            f'color: {pre_color}; white-space: pre-wrap; word-wrap: break-word; '
            f'line-height: 95%;">'
            f'{line_html or "&nbsp;"}</pre></td>'
            '</tr>'
            for (num, _), line_html in zip(shown, code_lines, strict=True)
        )
        if hidden:
            muted = "#8b949e" if dark else "#57606a"
            rows += (
                '<tr><td width="1%"></td>'
                f'<td width="99%" valign="top"><pre style="margin: 0; '
                f'color: {muted};">... {hidden} more line(s) hidden ...'
                '</pre></td></tr>'
            )
        return (
            f'<table width="100%" cellspacing="0" cellpadding="8" '
            f'bgcolor="{self._get_style("CODE_BG")}" '
            f'style="border: 1px solid {self._get_style("CODE_BORDER")}; '
            'margin-top: 2px;"><tr><td>'
            '<table width="100%" cellspacing="0" cellpadding="0">'
            f'{rows}</table>'
            '</td></tr></table>'
        )

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
        # Qt's pre-wrap draws a lone \r as its own break on top of the \n, so
        # CRLF content (shell output, Windows text files) came out double
        # spaced. Normalizing first and tightening the line-height keeps a
        # long block readable instead of twice as tall as it needs to be.
        inner_html = str(inner_html).replace("\r\n", "\n").replace("\r", "\n")
        return (
            f'<table width="100%" cellspacing="0" cellpadding="8" '
            f'bgcolor="{self._get_style("CODE_BG")}" '
            f'style="border: 1px solid {self._get_style("CODE_BORDER")}; '
            'margin-top: 2px;"><tr><td>'
            f'<pre style="margin: 0; color: '
            f'{color or self._get_style("TERMINAL_PRE_COLOR")}; '
            'white-space: pre-wrap; word-wrap: break-word; line-height: 95%;">'
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

