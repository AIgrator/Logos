"""Browser tools: ref-based web actions executed via BrowserManager.

Each tool reads the shared manager from the Computer instance
(`computer.browser_manager`, set by the harness / handler — same pattern
as `vision_manager` and `app_launcher`). Refs come from `browser_snapshot`
and expire after every navigation.
"""
