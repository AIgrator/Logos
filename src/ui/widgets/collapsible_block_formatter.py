# src/ui/widgets/collapsible_block_formatter.py
"""
This module provides a formatter for creating collapsible HTML blocks.
"""
import base64
import html

from PySide6.QtCore import QBuffer, QIODevice, QSize
import qtawesome as qta

class CollapsibleBlockFormatter:
    """
    Generates themed HTML for a collapsible (accordion) block.
    """
    def __init__(self, theme_manager):
        self.theme_manager = theme_manager

    def _get_style(self, key):
        """Fetches a style property from the ThemeManager for the current theme."""
        return self.theme_manager.get_style(key)

    def _icon_to_data_uri(self, icon_name: str, color: str, size: int = 12) -> str:
        """Converts a qtawesome icon into a base64-encoded data URI."""
        try:
            icon = qta.icon(icon_name, color=color)
            pixmap = icon.pixmap(QSize(size, size))
            buffer = QBuffer()
            buffer.open(QIODevice.WriteOnly)
            pixmap.save(buffer, "PNG")
            encoded_data = base64.b64encode(buffer.data()).decode('utf-8')
            return f"data:image/png;base64,{encoded_data}"
        except Exception:
            return ""

    def _get_icon_style(self, size: int = 12) -> str:
        """Returns a standardized CSS style string for icons."""
        return f"vertical-align: middle; height: {size}px; width: {size}px;"

    def format_block(self, title: str, content: str, block_id: str, state: str) -> str:
        """
        Generates the HTML for a collapsible block in either a collapsed or expanded state.
        """
        escaped_title = html.escape(title)
        icon_color = self._get_style('TERMINAL_HEADER_COLOR')
        icon_size = 12

        if state == 'collapsed':
            arrow_icon_uri = self._icon_to_data_uri('fa5s.caret-right', icon_color, icon_size)
            href = f"toggle:{block_id}:expanded"
            content_html = ''
        else:  # expanded
            arrow_icon_uri = self._icon_to_data_uri('fa5s.caret-down', icon_color, icon_size)
            href = f"toggle:{block_id}:collapsed"
            escaped_content = html.escape(content)
            content_html = f"""
            <div>
                <pre style="
                    background-color: {self._get_style('TERMINAL_PRE_BG')};
                    padding: 8px;
                    border-radius: 4px;
                    color: {self._get_style('TERMINAL_CODE_COLOR')};
                    white-space: pre-wrap;
                    word-wrap: break-word;
                    margin-top: 5px;
                "><code>{escaped_content}</code></pre>
            </div>
            """

        icon_style = self._get_icon_style(icon_size)
        arrow_html = f'<img src="{arrow_icon_uri}" style="{icon_style}">'

        return f"""
        <div style="
            background-color: {self._get_style('TERMINAL_CONTAINER_BG')};
            border-left: 3px solid {self._get_style('TERMINAL_CONTAINER_BORDER')};
            padding: 10px;
            margin-top: 5px;
            margin-bottom: 5px;
        ">
            <div style="font-weight: bold;">
                <a href="{href}" style="text-decoration: none; color: {icon_color};">
                    {arrow_html}&nbsp;{escaped_title}
                </a>
            </div>
            {content_html}
        </div>
        """
