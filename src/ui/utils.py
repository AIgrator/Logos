from PySide6.QtCore import Qt, QEvent, QObject, QPoint
from PySide6.QtWidgets import QLabel, QPushButton, QToolButton, QWidget
import qtawesome as qta

from . import styles
class IconHoverFilter(QObject):
    def __init__(self, theme_manager, icon_name, hover_icon_name=None, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.icon_name = icon_name
        self.hover_icon_name = hover_icon_name if hover_icon_name else icon_name
        self.normal_icon = None
        self.hover_icon = None

        self.theme_manager.theme_changed.connect(self._update_icons)
        self._update_icons()

    def _update_icons(self):
        """Updates icon colors based on the current theme."""
        normal_color = self.theme_manager.get_style("ICON_COLOR")
        hover_color = self.theme_manager.get_style("ICON_COLOR_HOVER")

        self.normal_icon = qta.icon(self.icon_name, color=normal_color)
        self.hover_icon = qta.icon(self.hover_icon_name, color=hover_color)

        # After updating icons, re-apply the correct one to the parent widget
        # based on its current state (e.g., if the mouse is already over it).
        parent_widget = self.parent()
        if isinstance(parent_widget, QPushButton):
            if parent_widget.underMouse():
                parent_widget.setIcon(self.hover_icon)
            else:
                parent_widget.setIcon(self.normal_icon)

    def set_icon_names(self, icon_name, hover_icon_name=None):
        """Dynamically changes the icons managed by the filter."""
        self.icon_name = icon_name
        self.hover_icon_name = hover_icon_name if hover_icon_name else icon_name
        self._update_icons()

    def eventFilter(self, watched, event):
        if not isinstance(watched, QPushButton):
            return super().eventFilter(watched, event)

        if event.type() == QEvent.Enter:
            watched.setIcon(self.hover_icon)
        elif event.type() == QEvent.Leave:
            watched.setIcon(self.normal_icon)

        return super().eventFilter(watched, event)

class NavButtonHoverFilter(QObject):
    """
    A dedicated event filter for navigation QToolButtons that handles hover
    and checked states, updating icon colors accordingly.
    """
    def __init__(self, theme_manager, icon_name, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.icon_name = icon_name
        self._icons = {}  # To store normal, hover, and checked icons

        self.theme_manager.theme_changed.connect(self._update_icons)
        self._update_icons()

    def _update_icons(self):
        """Updates icon colors based on the current theme."""
        self._icons = {
            "normal": qta.icon(self.icon_name, color=self.theme_manager.get_style("ICON_COLOR")),
            "hover": qta.icon(self.icon_name, color=self.theme_manager.get_style("ICON_COLOR_HOVER")),
            "checked": qta.icon(self.icon_name, color=self.theme_manager.get_style("NAV_ICON_COLOR_CHECKED"))
        }
        self._apply_icon_to_parent()

    def force_update(self):
        """Public method to force an icon update based on the current state."""
        self._apply_icon_to_parent()

    def _apply_icon_to_parent(self):
        """
        Applies the correct icon to the parent widget based on its state
        and whether its window is active.
        """
        parent = self.parent()
        if not isinstance(parent, QToolButton):
            return

        is_window_active = parent.window().isActiveWindow()

        if parent.isChecked() and is_window_active:
            parent.setIcon(self._icons["checked"])
        elif parent.underMouse() and is_window_active:
            parent.setIcon(self._icons["hover"])
        else:
            parent.setIcon(self._icons["normal"])

    def eventFilter(self, watched, event):
        if not isinstance(watched, QToolButton):
            return super().eventFilter(watched, event)

        # Events that should trigger a re-evaluation of the icon
        trigger_events = [
            QEvent.Enter, QEvent.Leave,
            QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
            QEvent.WindowActivate, QEvent.WindowDeactivate
        ]

        if event.type() in trigger_events:
            # Use a single shot timer to ensure the state is fully updated
            # before we apply the new icon.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._apply_icon_to_parent)

        return super().eventFilter(watched, event)

class CustomToolTipManager(QWidget):
    _instance = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tooltip_label = QLabel(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.tooltip_label.setStyleSheet(styles.TOOLTIP_STYLE)
        self.tooltip_label.setMaximumWidth(250)
        self.tooltip_label.setWordWrap(True)
        self.tooltip_label.hide()

    def install(self, widget, tooltip_text):
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        widget.setProperty("tooltip_text", tooltip_text)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Enter:
            tooltip_text = watched.property("tooltip_text")
            if tooltip_text:
                self.tooltip_label.setText(tooltip_text)

                # Calculate position
                btn_pos = watched.mapToGlobal(QPoint(0, 0))
                btn_width = watched.width()

                tooltip_size = self.tooltip_label.sizeHint()

                x = btn_pos.x() + (btn_width - tooltip_size.width()) / 2
                y = btn_pos.y() + watched.height() + 5

                self.tooltip_label.move(int(x), int(y))
                self.tooltip_label.show()

        elif event.type() == QEvent.Leave:
            self.tooltip_label.hide()

        return super().eventFilter(watched, event)
