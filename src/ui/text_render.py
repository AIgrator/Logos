"""Single text-to-HTML pipeline for all chat output.

Before this, chat/model text went through add_formatted_message (markdown
detect + render, else escape) while every agent mode hand-built its own
<b>/<i> fragments — the same text rendered differently per caller, and raw
structures (dict reprs with literal \\n) leaked into the chat unreadably.
Now: human text ALWAYS goes through render_text; only prebuilt widgets
(collapsibles) bypass it, with their parts escaped at build time.
"""

MARKDOWN_HINTS = ("###", "**", "```", "\n-", "\n*", "\n1.")


def looks_like_markdown(content: str) -> bool:
    return any(h in content for h in MARKDOWN_HINTS)


def render_text(content: str, prefix_html: str = "", markdown_ok: bool = True) -> str:
    """Renders human text to chat HTML.

    markdown_ok=True (model prose): render markdown when detected, else
    escape. markdown_ok=False (machine text: errors, statuses, reports):
    ALWAYS escape — deterministic, no mangling of **/``` inside errors.
    """
    import html

    content = content if isinstance(content, str) else str(content)
    if markdown_ok and looks_like_markdown(content):
        import markdown
        processed = markdown.markdown(content, extensions=['fenced_code'])
        if prefix_html:
            return f'<p style="white-space: pre-wrap;">{prefix_html}{processed}</p>'
        return processed
    processed = html.escape(content)
    if prefix_html:
        return f'<p style="white-space: pre-wrap;">{prefix_html}{processed}</p>'
    return f'<p style="white-space: pre-wrap;">{processed}</p>'
