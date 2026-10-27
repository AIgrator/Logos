"""Single text-to-HTML pipeline for all chat output.

Before this, chat/model text went through add_formatted_message (markdown
detect + render, else escape) while every agent mode hand-built its own
<b>/<i> fragments — the same text rendered differently per caller, and raw
structures (dict reprs with literal \\n) leaked into the chat unreadably.
Now: human text ALWAYS goes through render_text; only prebuilt widgets
(collapsibles) bypass it, with their parts escaped at build time.
"""

import re

MARKDOWN_HINTS = ("###", "**", "```", "\n-", "\n*", "\n1.")
LIST_ITEM_RE = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+')
LIST_START_RE = re.compile(r'(?m)^\s*(?:[-*+]|\d+[.)])\s+')
INLINE_NUMBERED_RE = re.compile(r'(?<=\S) (?=\d{1,2}[.)]\s+)')

# Qt rich text ignores border-radius, so ChatWidget paints the user bubble
# background itself; these values keep the HTML cell and that paint in sync.
USER_BUBBLE_PADDING = 8
USER_BUBBLE_RADIUS = 12


def looks_like_markdown(content: str) -> bool:
    if any(h in content for h in MARKDOWN_HINTS):
        return True
    return bool(LIST_START_RE.search(content))


def _normalize_lists(content: str) -> str:
    """Make model-written lists render as lists even without blank lines.

    Python-Markdown requires a blank line before a list when it follows a
    paragraph, and models often write ``text: 1. ... 2. ...`` on one line.
    Split those inline items and insert the blank line, while leaving fenced
    code untouched.
    """
    lines = []
    in_fence = False
    for raw_line in content.splitlines():
        if raw_line.strip().startswith("```"):
            in_fence = not in_fence
            lines.append(raw_line)
            continue
        if in_fence:
            lines.append(raw_line)
            continue
        lines.extend(INLINE_NUMBERED_RE.sub("\n", raw_line).split("\n"))

    normalized = []
    for line in lines:
        is_item = bool(LIST_ITEM_RE.match(line))
        previous = normalized[-1] if normalized else ""
        if (is_item and previous.strip() and not LIST_ITEM_RE.match(previous)
                and not previous.lstrip().startswith(("#", "```"))):
            normalized.append("")
        normalized.append(line)
    return "\n".join(normalized)


def _format_attachment_size(size) -> str:
    try:
        size = max(0, int(size or 0))
    except (TypeError, ValueError):
        size = 0
    if not size:
        return ""
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


def render_attachment_chips(attachments, background: str, border: str,
                            text_color: str, warning_color: str = "#9a6700",
                            error_color: str = "#cf222e") -> str:
    """Compact transcript chips for attachments recorded on a message."""
    import html

    chips = []
    for record in attachments or []:
        if not isinstance(record, dict):
            continue
        name = html.escape(str(record.get("name") or "attachment"))
        kind = html.escape(str(record.get("kind") or "file"))
        size = _format_attachment_size(record.get("size"))
        meta = kind + (f" · {size}" if size else "")
        status = str(record.get("status") or "")
        badge = ""
        if status == "missing":
            badge = (f' <span style="color: {error_color}; font-weight: 600;">'
                     'missing</span>')
        elif status == "changed":
            badge = (f' <span style="color: {warning_color}; font-weight: 600;">'
                     'changed</span>')
        chips.append(
            f'<span style="display: inline; background-color: {background}; '
            f'border: 1px solid {border}; border-radius: 8px; '
            f'padding: 1px 7px; margin-right: 5px; color: {text_color}; '
            f'font-size: 11px;">{name} '
            f'<span style="color: {text_color};">({meta})</span>{badge}</span>')
    if not chips:
        return ""
    return ('<div align="left" style="margin-top: 6px;">'
            + "".join(chips)
            + "</div>")


def render_user_message(content: str, background: str, text_color: str,
                        meta_color: str = "#8b949e", queued: bool = False,
                        attachments=None, attachment_bg: str = "",
                        attachment_border: str = "", attachment_text: str = "",
                        warning_color: str = "#9a6700",
                        error_color: str = "#cf222e") -> str:
    """Render a user prompt as a right-aligned bubble.

    The bubble hugs its content: an empty left spacer column caps it at 75%
    of the text column and keeps short messages narrow instead of stretching
    them across the chat. The background is not painted here — ChatWidget
    draws a rounded rect under the cell, because Qt rich text ignores
    border-radius.
    """
    import html

    content = content if isinstance(content, str) else str(content)
    escaped = html.escape(content)
    queued_html = ""
    if queued:
        queued_html = (f'<p align="left" style="margin: 4px 0 0 0; '
                       f'color: {meta_color};"><i>queued</i></p>')
    chips = render_attachment_chips(
        attachments,
        attachment_bg or background,
        attachment_border or meta_color,
        attachment_text or text_color,
        warning_color,
        error_color,
    )
    return (
        '<table align="right" cellspacing="0" '
        f'cellpadding="{USER_BUBBLE_PADDING}"><tr>'
        '<td width="25%"></td>'
        '<td>'
        f'<p align="left" style="margin: 0; color: {text_color}; '
        f'white-space: pre-wrap; line-height: 1.35;">{escaped}</p>'
        f'{chips}{queued_html}</td>'
        '</tr></table>'
    )


def render_text(content: str, prefix_html: str = "", markdown_ok: bool = True) -> str:
    """Renders human text to chat HTML.

    markdown_ok=True (model prose): render markdown when detected, else
    escape. markdown_ok=False (machine text: errors, statuses, reports):
    ALWAYS escape — deterministic, no mangling of **/``` inside errors.
    """
    import html

    content = content if isinstance(content, str) else str(content)
    normalized = _normalize_lists(content) if markdown_ok else content
    if markdown_ok and looks_like_markdown(normalized):
        import markdown
        processed = markdown.markdown(normalized, extensions=['fenced_code', 'nl2br', 'sane_lists'])
        if prefix_html:
            # Prefix as its own paragraph: block-level markdown (<h3>/<ol>/<p>)
            # must not be nested inside a <p> (invalid HTML).
            return (f'<p style="white-space: pre-wrap; margin: 0 0 4px 0;">'
                    f'{prefix_html}</p>{processed}')
        return processed
    processed = html.escape(content)
    if prefix_html:
        return f'<p style="white-space: pre-wrap;">{prefix_html}{processed}</p>'
    return f'<p style="white-space: pre-wrap;">{processed}</p>'


def render_reasoning(content: str, color: str) -> str:
    """Model reasoning: normal Markdown, muted so it never reads as an answer."""
    return f'<div style="color: {color};">{render_text(content, markdown_ok=True)}</div>'
