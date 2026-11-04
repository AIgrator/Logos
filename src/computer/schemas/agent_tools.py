"""Tool names both agent envelopes accept.

A session can hold several capabilities at once, so the response format of one
handler may have to carry a tool that belongs to another capability (a terminal
turn that also holds the browser capability). The envelope therefore lists the
union; whether a call is *allowed* is decided at execution time from the
session's grants, never by the response format.
"""
from src.computer import capabilities

#: Every tool of the three agent capabilities, de-duplicated, in grant order.
AGENT_TOOL_NAMES: tuple[str, ...] = capabilities.tools_for_grants(
    (capabilities.BASE, capabilities.TERMINAL, capabilities.BROWSER))
