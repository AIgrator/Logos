"""Capability controls of the composer: what this session may do, at a glance.

The composer used to carry a mode selector (a button with an exclusive menu of
"Chat Mode" / "Terminal Mode" / ...). A session is not mode-locked anymore: it
holds capabilities (``base`` always, plus ``terminal``, ``browser``, ``vision``)
and one of them is the *presentation* the current turn answers with. So the
controls show both facts at once:

- clicking a control opens that panel, attaching its capability if it is not
  attached yet (the controller does the attaching);
- an attached capability is marked with a check and drawn in the icon color,
  an unattached one stays muted;
- the active presentation is the checked (pressed) control;
- right-clicking an attached Terminal/Browser/Vision control offers
  "Detach from session" (P8 of the vision plan: revoking is explicit).

Chat is not a capability to attach: it is the base-only presentation, kept as a
control until Phase 4 turns it into "a session with no capability attached".
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QPushButton, QWidget

from src.computer import capabilities

#: Presentation name -> (label, capability it needs, or None for base only).
CONTROLS: tuple[tuple[str, str, str | None], ...] = (
    ("chat", "Chat", None),
    ("terminal", "Terminal", capabilities.TERMINAL),
    ("browser", "Browser", capabilities.BROWSER),
    ("vision", "Vision", capabilities.VISION),
)

_ATTACHED_PREFIX = "\u2713 "
_PLAIN_PREFIX = "  "

_CAPABILITY_OF = {name: capability for name, _, capability in CONTROLS}


class CapabilityBar(QWidget):
    """Four controls: which panel drives the turn, and what the session holds."""

    presentation_requested = Signal(str)
    detach_requested = Signal(str)

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self._theme = theme_manager
        self._attached: frozenset[str] = frozenset({capabilities.BASE})
        self._active = "chat"
        self._vision_enabled = True
        self._buttons: dict[str, QPushButton] = {}

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        for name, label, capability in CONTROLS:
            button = QPushButton(label, self)
            button.setObjectName("CapabilityChip")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.setFixedHeight(26)
            button.clicked.connect(
                lambda _checked=False, key=name: self.presentation_requested.emit(key))
            if capability is not None:
                button.setContextMenuPolicy(Qt.CustomContextMenu)
                button.customContextMenuRequested.connect(
                    lambda pos, key=name: self._show_context_menu(key, pos))
            self._buttons[name] = button
            row.addWidget(button)
        row.addStretch(0)
        if self._theme is not None:
            try:
                self._theme.theme_changed.connect(self.refresh_theme)
            except Exception:
                pass
        self._apply_theme()
        self._refresh_buttons()

    # -- public API ---------------------------------------------------------

    def set_state(self, active: str, attached, vision_enabled: bool = True) -> None:
        """One call for the whole bar: what is open and what is granted."""
        self._active = str(active or "chat")
        self._attached = frozenset(capabilities.normalize_grants(attached or ()))
        self._vision_enabled = bool(vision_enabled)
        self._refresh_buttons()

    def set_active(self, active: str) -> None:
        self._active = str(active or "chat")
        self._refresh_buttons()

    def set_attached(self, attached) -> None:
        self._attached = frozenset(capabilities.normalize_grants(attached or ()))
        self._refresh_buttons()

    def set_vision_loading(self, loading: bool) -> None:
        """Vision is being prepared: shown but not clickable."""
        self._vision_enabled = not loading
        self._refresh_buttons()

    def refresh_theme(self) -> None:
        self._apply_theme()
        self._refresh_buttons()

    # -- internals ----------------------------------------------------------

    def _is_attached(self, name: str) -> bool:
        """Whether the control carries the check mark.

        Chat is not a capability, so it never does: it is always available and
        attaching it means nothing.
        """
        capability = _CAPABILITY_OF[name]
        return capability is not None and capability in self._attached

    def _refresh_buttons(self) -> None:
        for name, label, capability in CONTROLS:
            button = self._buttons[name]
            attached = self._is_attached(name)
            if name == "vision" and not self._vision_enabled:
                button.setText(label)
                button.setEnabled(False)
                button.setToolTip("Initializing vision module...")
            else:
                button.setText((_ATTACHED_PREFIX if attached else _PLAIN_PREFIX) + label)
                button.setEnabled(True)
                button.setToolTip(self._tooltip(name, label, capability, attached))
                button.setProperty("attached", "true" if attached else "false")
            button.setChecked(name == self._active)
            button.style().unpolish(button)
            button.style().polish(button)

    def _tooltip(self, name: str, label: str, capability: str | None,
                 attached: bool) -> str:
        if name == "chat":
            return ("Chat: plain answers in the session's transcript.\n"
                    "Attach Terminal or Browser to let the agent act.")
        if attached:
            return (f"{label} is attached to this session.\n"
                    f"Click to open it. Right-click to detach.")
        return (f"{label} is not attached yet.\n"
                f"Click to attach it and open its panel.")

    def _show_context_menu(self, name: str, pos) -> None:
        if not self._is_attached(name):
            return
        button = self._buttons[name]
        menu = QMenu(button)
        detach = menu.addAction(f"Detach {name.capitalize()} from session")
        chosen = menu.exec(button.mapToGlobal(pos))
        if chosen is detach:
            self.detach_requested.emit(name)

    def _apply_theme(self) -> None:
        if self._theme is None:
            return
        try:
            meta = self._theme.get_style("COLLAPSIBLE_META")
            icon = self._theme.get_style("ICON_COLOR")
            border = self._theme.get_style("COLLAPSIBLE_BORDER")
            background = self._theme.get_style("COLLAPSIBLE_BG")
        except Exception:
            return
        self.setStyleSheet(f"""
            QPushButton#CapabilityChip {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 0 8px;
                font-size: 12px;
                color: {meta};
            }}
            QPushButton#CapabilityChip:hover {{
                border-color: {border};
                color: {icon};
            }}
            QPushButton#CapabilityChip[attached="true"] {{
                color: {icon};
            }}
            QPushButton#CapabilityChip:checked {{
                background: {background};
                border-color: {border};
                color: {icon};
            }}
            QPushButton#CapabilityChip:disabled {{
                color: {meta};
            }}
        """)
