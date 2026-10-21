import html

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound

MAX_HIGHLIGHT_LINES = 600


def highlight_code(code: str, filename: str = "", dark: bool = True) -> tuple[str, bool]:
    """Return inline-styled HTML for ``code`` and whether it was truncated."""
    if not code:
        return "", False

    lines = code.splitlines()
    truncated = len(lines) > MAX_HIGHLIGHT_LINES
    if truncated:
        code = "\n".join(lines[:MAX_HIGHLIGHT_LINES])

    try:
        lexer = get_lexer_for_filename(filename, code) if filename else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()

    formatter = HtmlFormatter(
        nowrap=True,
        noclasses=True,
        style="monokai" if dark else "friendly",
    )
    rendered = highlight(code, lexer, formatter)
    if truncated:
        muted = "#8b949e" if dark else "#57606a"
        rendered += (
            f'\n<span style="color: {muted};">'
            f'... {len(lines) - MAX_HIGHLIGHT_LINES} more line(s) hidden ...'
            "</span>"
        )
    return rendered, truncated


def plain_code_notice(filename: str, line_count: int, truncated: bool) -> str:
    """Small, escaped status line describing a code block."""
    label = html.escape(filename or "file")
    suffix = " (preview)" if truncated else ""
    return f"{label} · {line_count} line(s){suffix}"
