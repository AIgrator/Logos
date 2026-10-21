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


def render_user_message(content: str, background: str, border: str, text_color: str,
                        label_color: str, label: str = "You", queued: bool = False,
                        queued_color: str = "#8b949e") -> str:
    """Render a user prompt as a safe, line-preserving card."""
    import html

    content = content if isinstance(content, str) else str(content)
    escaped = html.escape(content)
    queued_html = ""
    if queued:
        queued_html = f' <span style="color: {queued_color};">(queued)</span>'
    return (
        f'<div style="background-color: {background}; border-left: 4px solid {border}; '
        'padding: 10px 12px; margin: 6px 0;">'
        f'<div style="color: {label_color}; font-weight: 600; margin-bottom: 4px;">'
        f'{html.escape(label)}{queued_html}</div>'
        f'<div style="color: {text_color}; white-space: pre-wrap; line-height: 1.35;">'
        f'{escaped}</div></div>'
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
