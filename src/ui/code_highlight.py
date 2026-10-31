import copy
import html
import re

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexer import using
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.lexers.css import CssLexer
from pygments.lexers.javascript import JavascriptLexer
from pygments.lexers.web import HtmlLexer
from pygments.util import ClassNotFound

MAX_HIGHLIGHT_LINES = 600


class _FragmentHtmlLexer(HtmlLexer):
    """HtmlLexer that keeps embedded CSS/JS highlighted in file fragments.

    The stock lexer leaves the style/script state at the first newline when
    the closing tag is not on that line (its fallback rule pops), so a
    read_files slice cut inside a <style> or <script> block lost its
    highlighting after one line. Stay in the embedded state until the closing
    tag or the end of the fragment instead.
    """
    tokens = copy.deepcopy(HtmlLexer.tokens)
    tokens['style-content'] = [
        HtmlLexer.tokens['style-content'][0],
        (r'(?s).+?(?=<\s*/\s*style\s*>)', using(CssLexer)),
        (r'.+?\n', using(CssLexer)),
        (r'.+', using(CssLexer)),
    ]
    tokens['script-content'] = [
        HtmlLexer.tokens['script-content'][0],
        (r'(?s).+?(?=<\s*/\s*script\s*>)', using(JavascriptLexer)),
        (r'.+?\n', using(JavascriptLexer)),
        (r'.+', using(JavascriptLexer)),
    ]


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
    if isinstance(lexer, HtmlLexer):
        lexer = _FragmentHtmlLexer()

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


def is_html_filename(filename: str) -> bool:
    """Whether ``filename`` selects the HTML lexer (embedded CSS/JS blocks)."""
    try:
        return isinstance(get_lexer_for_filename(filename or "", ""), HtmlLexer)
    except ClassNotFound:
        return False


_SPAN_TOKEN_RE = re.compile(r'(<span\b[^>]*>|</span>|\n)')


def split_highlighted_lines(markup: str) -> list[str]:
    """Split nowrap Pygments HTML into one valid HTML string per code line.

    A token can span newlines (a multi-line comment or string), so the spans
    still open at a break are closed before it and reopened after it; that
    keeps every line standalone for a one-row-per-line gutter.
    """
    lines: list[str] = []
    open_tags: list[str] = []
    current: list[str] = []
    # HtmlFormatter closes the last line with a newline of its own.
    markup = markup[:-1] if markup.endswith('\n') else markup
    for piece in _SPAN_TOKEN_RE.split(markup):
        if piece == '\n':
            current.append('</span>' * len(open_tags))
            lines.append(''.join(current))
            current = list(open_tags)
        elif piece == '</span>':
            if open_tags:
                open_tags.pop()
            current.append(piece)
        elif piece:
            current.append(piece)
            if piece.startswith('<span'):
                open_tags.append(piece)
    lines.append(''.join(current))
    return lines
