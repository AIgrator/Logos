"""Settings page for connecting and activating model providers.

The page lists connected providers as rows (tile, name, current model, Use /
Edit / Delete) and the rest as a connect list. Built-in providers open a
schema-driven dialog for the API key and model; custom OpenAI- or
Anthropic-compatible endpoints keep their own definition dialog. Edits are
staged in memory (``save=False, emit=False``) and announced through the
``changed`` signal, so the settings page autosaves them shortly after they
are made; ``commit()`` then prunes keychain entries of deleted providers.
"""
import copy
import logging

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from llm import PROVIDER_METADATA, PROVIDERS, providers_ready, start_provider_warmup
from llm.custom_providers import is_custom_provider, provider_is_configured
from llm.mode_capabilities import CUA_PROVIDERS, ModeCapabilities
from llm.model_config_manager import ModelConfigManager

from src.ui.provider_icons import provider_glyph
from src.ui.widgets.busy_spinner import BusySpinner
from src.ui.widgets.custom_provider_dialog import CustomProviderDialog
from src.ui.widgets.provider_form import ProviderForm

logger = logging.getLogger(__name__)

_PROVIDER_DESCRIPTIONS = {
    "anthropic": "Claude models · native computer use",
    "openai": "GPT models · native computer use",
    "google": "Gemini models · native computer use",
    "openrouter": "Many models behind a single key",
    "huggingface": "Open models through the Hugging Face router",
    "x-ai": "Grok models",
}

_PROVIDER_ACCENTS = {
    "anthropic": "#D97757",
    "openai": "#10A37F",
    "google": "#4285F4",
    "openrouter": "#7C5CFF",
    "huggingface": "#FFB800",
    "x-ai": "#555555",
}

_CUSTOM_ACCENT = "#7E57C2"
_FALLBACK_ACCENTS = ("#5B8DEF", "#2FA98C", "#D96C6C", "#C77D3E", "#6C8EBF")

_PAGE_STYLE = """
QLabel#providerPageTitle { font-size: 20px; font-weight: 600; }
QLabel#providerPageSubtitle { color: #9E9E9E; }
QLabel#providerSectionHeader { color: #9E9E9E; font-size: 11px; font-weight: 700; }
QFrame#providerCard {
    background-color: palette(base);
    border: 1px solid palette(mid);
    border-radius: 10px;
}
QFrame#providerBanner {
    background-color: palette(alternate-base);
    border: 1px solid palette(mid);
    border-left: 3px solid palette(highlight);
    border-radius: 8px;
}
QLabel#providerBannerText { background: transparent; }
QLabel#providerRowName { font-size: 14px; font-weight: 600; background: transparent; }
QLabel#providerRowDetail { color: #9E9E9E; font-size: 12px; background: transparent; }
QLabel#providerRowEmpty { color: #9E9E9E; font-style: italic; background: transparent; padding: 14px; }
QLabel#providerPillActive {
    background-color: palette(highlight);
    color: palette(highlighted-text);
    border-radius: 8px;
    padding: 1px 8px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#providerPillCustom {
    background-color: transparent;
    color: #9E9E9E;
    border: 1px solid palette(mid);
    border-radius: 8px;
    padding: 0px 8px;
    font-size: 11px;
}
QPushButton#providerPrimary {
    background-color: palette(highlight);
    color: palette(highlighted-text);
    border: none;
    border-radius: 6px;
    padding: 6px 16px;
    font-weight: 600;
}
QPushButton#providerPrimary:hover { background-color: #4695e6; }
QPushButton#providerPrimary:pressed { background-color: #1F6FBE; }
QPushButton#providerRowAction {
    border: none;
    background: transparent;
    padding: 5px 8px;
    border-radius: 6px;
}
QPushButton#providerRowAction:hover { background-color: palette(alternate-base); }
QPushButton#providerLink {
    border: none;
    background: transparent;
    color: palette(link);
    padding: 6px 8px;
    text-align: left;
}
QPushButton#providerLink:hover { background-color: palette(alternate-base); border-radius: 6px; }
"""


def _accent_color(provider_id: str) -> str:
    accent = _PROVIDER_ACCENTS.get(provider_id)
    if accent:
        return accent
    if is_custom_provider(provider_id):
        return _CUSTOM_ACCENT
    checksum = sum(ord(char) for char in provider_id)
    return _FALLBACK_ACCENTS[checksum % len(_FALLBACK_ACCENTS)]


def _text_on(accent: str) -> str:
    color = QColor(accent)
    luminance = (0.299 * color.red() + 0.587 * color.green()
                 + 0.114 * color.blue()) / 255
    return "#1b1b1b" if luminance > 0.6 else "#ffffff"


def _initials(display_name: str) -> str:
    words = [word for word in display_name.replace("=", " ").split() if word]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:1].upper()
    return (words[0][:1] + words[1][:1]).upper()


def _separator():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Plain)
    line.setFixedHeight(1)
    line.setStyleSheet(
        "QFrame { border: none; background-color: palette(mid); }")
    return line


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


class _ProviderLoadingDialog(QDialog):
    """Shown while provider SDKs import, before a provider dialog can open."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Providers")
        self.setModal(True)
        self.setFixedSize(300, 170)

        layout = QVBoxLayout(self)
        layout.addSpacing(10)
        self.spinner = BusySpinner()
        self.spinner.setFixedSize(42, 42)
        self.spinner.start()
        layout.addWidget(self.spinner, 0, Qt.AlignHCenter)
        label = QLabel("Loading provider settings…")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #9E9E9E;")
        layout.addWidget(label)
        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ProviderDialog(QDialog):
    """API key and model for one built-in provider."""

    def __init__(self, provider_id, settings_manager, parent=None):
        super().__init__(parent)
        self.provider_id = provider_id
        self.values = {}

        provider_class = PROVIDERS[provider_id]
        name = provider_class.get_display_name()
        self.setWindowTitle(f"{name} Settings")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setStyleSheet(_PAGE_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(10)

        title = QLabel(name)
        title.setObjectName("providerPageTitle")
        layout.addWidget(title)

        subtitle = QLabel(_PROVIDER_DESCRIPTIONS.get(provider_id, ""))
        subtitle.setObjectName("providerPageSubtitle")
        subtitle.setWordWrap(True)
        if not subtitle.text():
            subtitle.setVisible(False)
        layout.addWidget(subtitle)

        self.form = ProviderForm(provider_id)
        stored = (
            (settings_manager.get("llm_providers", {}) or {})
            .get(provider_id) or {})
        # Provider defaults prefill a fresh connect, so e.g. OpenRouter starts
        # on its recommended model instead of the first cached entry.
        self.form.load({**self.form.defaults, **stored})
        layout.addWidget(self.form)

        note = QLabel(
            "The API key is stored in the system keychain. The selected "
            "model is used by Chat, Terminal and Browser.")
        note.setObjectName("providerRowDetail")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #E06C75;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.key_edit = self.form.api_key_widget()
        if self.key_edit is not None:
            self.save_button.setEnabled(bool(self.key_edit.text().strip()))
            self.key_edit.textChanged.connect(self._update_save_state)

    def _update_save_state(self, text):
        self.save_button.setEnabled(bool(text.strip()))

    def _validate_and_accept(self):
        values = self.form.values()
        if self.key_edit is not None:
            api_key = (values.get("api_key") or "").strip()
            if not api_key:
                self.error_label.setText(
                    "Enter an API key to connect this provider.")
                self.error_label.setVisible(True)
                return
            values["api_key"] = api_key
        self.values = values
        self.accept()


class ProvidersPage(QWidget):
    """Provider list with connect, edit, activate and delete actions."""

    changed = Signal()

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self._deleted_custom_ids = set()
        self.setStyleSheet(_PAGE_STYLE)
        self._build_ui()
        self.refresh()

    # --- construction -----------------------------------------------------

    def repolish(self):
        """Re-resolves palette() roles in the sheet after a theme switch."""
        self.setStyleSheet(self.styleSheet())

    def changeEvent(self, event):
        # palette(...) roles in the sheet are resolved at polish time, so a
        # live theme switch would keep stale colors until reopened.
        if event.type() == QEvent.PaletteChange:
            self.repolish()
        super().changeEvent(event)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("Providers")
        title.setObjectName("providerPageTitle")
        layout.addWidget(title)

        subtitle = QLabel(
            "Choose the model provider Logos uses for Chat, Terminal and "
            "Browser. Keys are stored in the system keychain; everything "
            "else lives in settings.")
        subtitle.setObjectName("providerPageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.banner = QFrame()
        self.banner.setObjectName("providerBanner")
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(12, 10, 12, 10)
        self.banner_label = QLabel()
        self.banner_label.setObjectName("providerBannerText")
        self.banner_label.setWordWrap(True)
        banner_layout.addWidget(self.banner_label)
        layout.addWidget(self.banner)

        self.connected_header = QLabel("CONNECTED")
        self.connected_header.setObjectName("providerSectionHeader")
        layout.addWidget(self.connected_header)
        self.connected_card = QFrame()
        self.connected_card.setObjectName("providerCard")
        self.connected_layout = QVBoxLayout(self.connected_card)
        self.connected_layout.setContentsMargins(0, 4, 0, 4)
        self.connected_layout.setSpacing(0)
        layout.addWidget(self.connected_card)

        self.available_header = QLabel("AVAILABLE")
        self.available_header.setObjectName("providerSectionHeader")
        layout.addWidget(self.available_header)
        self.available_card = QFrame()
        self.available_card.setObjectName("providerCard")
        self.available_layout = QVBoxLayout(self.available_card)
        self.available_layout.setContentsMargins(0, 4, 0, 4)
        self.available_layout.setSpacing(0)
        layout.addWidget(self.available_card)

        self.vision_header = QLabel("VISION (COMPUTER USE)")
        self.vision_header.setObjectName("providerSectionHeader")
        layout.addWidget(self.vision_header)
        self.vision_card = QFrame()
        self.vision_card.setObjectName("providerCard")
        vision_layout = QVBoxLayout(self.vision_card)
        vision_layout.setContentsMargins(14, 10, 10, 12)
        vision_layout.setSpacing(6)

        vision_title = QLabel("Model for Vision")
        vision_title.setObjectName("providerRowName")
        vision_layout.addWidget(vision_title)
        vision_detail = QLabel(
            "Used when Logos drives the screen. Native computer use is "
            "available through OpenAI, Anthropic and Google.")
        vision_detail.setObjectName("providerRowDetail")
        vision_detail.setWordWrap(True)
        vision_layout.addWidget(vision_detail)

        vision_row = QHBoxLayout()
        vision_row.setSpacing(8)
        self.vision_provider_combo = QComboBox()
        self.vision_provider_combo.setMinimumWidth(200)
        self.vision_provider_combo.setToolTip(
            "Provider that drives the Vision capability.")
        self.vision_provider_combo.currentIndexChanged.connect(
            self._on_vision_provider_changed)
        vision_row.addWidget(self.vision_provider_combo)

        self.vision_model_combo = QComboBox()
        self.vision_model_combo.setEditable(True)
        self.vision_model_combo.setPlaceholderText("Select a model")
        self.vision_model_combo.setMinimumWidth(260)
        self.vision_model_combo.setToolTip(
            "Default model for Vision. It is used when the active model "
            "cannot drive the screen.")
        self.vision_model_combo.currentTextChanged.connect(
            self._on_vision_model_changed)
        vision_row.addWidget(self.vision_model_combo, 1)
        vision_layout.addLayout(vision_row)

        self.vision_hint = QLabel(
            "Vision needs native computer use. Connect OpenAI, Anthropic or "
            "Google — or update the model lists — to choose a model here. "
            "Other providers and custom endpoints cannot drive Vision.")
        self.vision_hint.setObjectName("providerRowDetail")
        self.vision_hint.setWordWrap(True)
        vision_layout.addWidget(self.vision_hint)
        layout.addWidget(self.vision_card)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        key_note = QLabel("API keys never leave the system keychain.")
        key_note.setObjectName("providerRowDetail")
        footer.addWidget(key_note)
        footer.addStretch(1)
        self.update_models_button = QPushButton("Update model lists")
        self.update_models_button.setObjectName("providerLink")
        self.update_models_button.setCursor(Qt.PointingHandCursor)
        self.update_models_button.setToolTip(
            "Download the latest model list from the Logos models repository.")
        self.update_models_button.clicked.connect(self._update_models)
        footer.addWidget(self.update_models_button)
        layout.addLayout(footer)
        layout.addStretch(1)

    # --- data helpers -----------------------------------------------------

    def _display_name(self, provider_id):
        info = PROVIDER_METADATA.get(provider_id)
        if info is not None and info.display_name:
            return info.display_name
        try:
            return PROVIDERS[provider_id].get_display_name()
        except Exception:
            return provider_id

    def _sorted_provider_ids(self):
        return sorted(PROVIDERS, key=lambda pid: (self._display_name(pid).lower(), pid))

    def _connected_ids(self, providers=None):
        settings = {"llm_providers": (
            providers if providers is not None
            else self.settings_manager.get("llm_providers", {}) or {})}
        return [pid for pid in self._sorted_provider_ids()
                if provider_is_configured(settings, pid)]

    def _custom_definition(self, provider_id):
        for definition in self.settings_manager.get("custom_providers", []) or []:
            if isinstance(definition, dict) and definition.get("id") == provider_id:
                return definition
        return None

    def _custom_definitions(self):
        return [copy.deepcopy(d) for d in
                (self.settings_manager.get("custom_providers", []) or [])
                if isinstance(d, dict)]

    def _model_label(self, provider_id, model):
        if not model:
            return ""
        config = ModelConfigManager.PROVIDER_CONFIG or {}
        labels = (config.get(provider_id) or {}).get("model_labels") or {}
        return labels.get(model) or model

    def _detail_text(self, provider_id, providers):
        entry = providers.get(provider_id) or {}
        model_label = self._model_label(provider_id, entry.get("model") or "")
        if is_custom_provider(provider_id):
            definition = self._custom_definition(provider_id) or {}
            base_url = definition.get("base_url") or ""
            parts = [part for part in (model_label, base_url) if part]
            if (definition.get("protocol") or "openai") == "anthropic":
                parts.append("Anthropic Messages")
            return "  ·  ".join(parts) or "No model selected"
        return model_label or "No model selected"

    def _description(self, provider_id):
        if is_custom_provider(provider_id):
            return "Any OpenAI- or Anthropic-compatible endpoint"
        return _PROVIDER_DESCRIPTIONS.get(
            provider_id, f"Connect {self._display_name(provider_id)}")

    # --- vision model -----------------------------------------------------

    def _vision_models(self, provider_id):
        info = (ModelConfigManager.PROVIDER_CONFIG or {}).get(provider_id) or {}
        return [name for name in sorted(info.get("models") or [])
                if ModeCapabilities.supports(provider_id, name, "vision")]

    def _vision_providers(self):
        providers = self.settings_manager.get("llm_providers", {}) or {}
        settings = {"llm_providers": providers}
        candidates = [pid for pid in PROVIDERS
                      if pid in CUA_PROVIDERS
                      and provider_is_configured(settings, pid)
                      and self._vision_models(pid)]
        return sorted(candidates,
                      key=lambda pid: self._display_name(pid).lower())

    def _populate_vision(self):
        mode_models = self.settings_manager.get("mode_models", {}) or {}
        entry = mode_models.get("vision")
        entry = entry if isinstance(entry, dict) else {}
        stored_pid = entry.get("provider") or ""
        stored_mid = entry.get("model") or ""

        options = self._vision_providers()
        combo = self.vision_provider_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Not set", "")
        for provider_id in options:
            combo.addItem(self._display_name(provider_id), provider_id)
        if stored_pid and stored_pid not in options:
            # Keep a choice whose provider was disconnected so it is visible
            # instead of silently dropped from the settings.
            combo.addItem(
                f"{self._display_name(stored_pid)} (unavailable)", stored_pid)
        index = combo.findData(stored_pid) if stored_pid else 0
        combo.setCurrentIndex(index if index != -1 else 0)
        combo.blockSignals(False)

        self._populate_vision_models(combo.currentData() or "", stored_mid)
        self.vision_hint.setVisible(not options)
        if (stored_pid and not stored_mid
                and self.vision_model_combo.currentText().strip()):
            # A stored provider without a model (legacy entry): show and keep
            # its first capable model so Vision has a complete default.
            self._stage_vision_model()

    def _populate_vision_models(self, provider_id, model_id=""):
        combo = self.vision_model_combo
        combo.blockSignals(True)
        combo.clear()
        if provider_id:
            combo.addItems(self._vision_models(provider_id))
        combo.setEnabled(bool(provider_id))
        if provider_id and combo.count():
            if model_id:
                combo.setCurrentText(model_id)
            else:
                combo.setCurrentIndex(0)
        else:
            combo.setCurrentText("")
        combo.blockSignals(False)

    def _on_vision_provider_changed(self, _index=0):
        provider_id = self.vision_provider_combo.currentData() or ""
        self._populate_vision_models(provider_id, "")
        self._stage_vision_model()

    def _on_vision_model_changed(self, _text=""):
        self._stage_vision_model()

    def _stage_vision_model(self):
        provider_id = self.vision_provider_combo.currentData() or ""
        model_id = (
            self.vision_model_combo.currentText().strip() if provider_id else "")
        mode_models = copy.deepcopy(
            self.settings_manager.get("mode_models", {}) or {})
        if provider_id and model_id:
            mode_models["vision"] = {
                "provider": provider_id, "model": model_id}
        else:
            mode_models.pop("vision", None)
        if mode_models == (self.settings_manager.get("mode_models", {}) or {}):
            return
        self.settings_manager.set_many(
            {"mode_models": mode_models}, save=False, emit=False)
        self.changed.emit()

    # --- refresh ----------------------------------------------------------

    def load_settings(self):
        """Reloads the page from the current settings."""
        self._deleted_custom_ids.clear()
        self.refresh()

    def refresh(self):
        """Rebuilds the rows from the current settings."""
        providers = self.settings_manager.get("llm_providers", {}) or {}
        active = self.settings_manager.get("active_provider", "") or ""
        connected = self._connected_ids(providers)
        active_configured = provider_is_configured(
            {"llm_providers": providers}, active)

        self._update_banner(connected, active_configured)
        self._fill_connected(providers, connected, active)
        self._fill_available([pid for pid in self._sorted_provider_ids()
                              if pid not in connected])
        self._populate_vision()

    def _update_banner(self, connected, active_configured):
        if not connected:
            self.banner_label.setText(
                "No provider is connected yet. Connect one below with an API "
                "key, or add your own OpenAI- or Anthropic-compatible "
                "endpoint such as LM Studio, Ollama or a company gateway.")
            self.banner.setVisible(True)
        elif not active_configured:
            self.banner_label.setText(
                "Choose the provider Logos should use. Press Use on one of "
                "the connected providers below.")
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

    def _fill_connected(self, providers, connected, active):
        _clear_layout(self.connected_layout)
        if not connected:
            empty = QLabel("No providers connected yet.")
            empty.setObjectName("providerRowEmpty")
            self.connected_layout.addWidget(empty)
            return
        ordered = sorted(
            connected, key=lambda pid: (pid != active, self._display_name(pid).lower()))
        for index, provider_id in enumerate(ordered):
            if index:
                self.connected_layout.addWidget(_separator())
            self.connected_layout.addWidget(
                self._connected_row(provider_id, providers, provider_id == active))

    def _fill_available(self, available):
        _clear_layout(self.available_layout)
        for index, provider_id in enumerate(available):
            if index:
                self.available_layout.addWidget(_separator())
            self.available_layout.addWidget(self._available_row(provider_id))
        if available:
            self.available_layout.addWidget(_separator())
        self.available_layout.addWidget(self._add_custom_row())

    def _tile(self, provider_id):
        accent = _accent_color(provider_id)
        icon = provider_glyph(provider_id, _text_on(accent))
        label = QLabel()
        if icon is None:
            label.setText(_initials(self._display_name(provider_id)))
            label.setStyleSheet(
                f"QLabel {{ background-color: {accent}; "
                f"color: {_text_on(accent)}; border-radius: 9px; "
                f"font-size: 14px; font-weight: 700; }}")
        else:
            label.setPixmap(icon)
            label.setStyleSheet(
                f"QLabel {{ background-color: {accent}; border-radius: 9px; }}")
        label.setFixedSize(38, 38)
        label.setAlignment(Qt.AlignCenter)
        return label

    def _connected_row(self, provider_id, providers, is_active):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(12)
        layout.addWidget(self._tile(provider_id))

        info = QVBoxLayout()
        info.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        name = QLabel(self._display_name(provider_id))
        name.setObjectName("providerRowName")
        name.setToolTip(f"Provider ID: {provider_id}")
        title_row.addWidget(name)
        if is_active:
            active_pill = QLabel("Active")
            active_pill.setObjectName("providerPillActive")
            title_row.addWidget(active_pill)
        if is_custom_provider(provider_id):
            custom_pill = QLabel("Custom")
            custom_pill.setObjectName("providerPillCustom")
            title_row.addWidget(custom_pill)
        title_row.addStretch(1)
        info.addLayout(title_row)

        detail = QLabel(self._detail_text(provider_id, providers))
        detail.setObjectName("providerRowDetail")
        definition = self._custom_definition(provider_id) if is_custom_provider(provider_id) else None
        if definition:
            models = ", ".join(model.get("id", "") for model in definition.get("models") or [])
            protocol = ("Anthropic Messages"
                        if (definition.get("protocol") or "openai") == "anthropic"
                        else "OpenAI Chat Completions")
            detail.setToolTip(
                f"{protocol}\n{definition.get('base_url', '')}\n{models}")
        info.addWidget(detail)
        layout.addLayout(info, 1)

        if not is_active:
            use_button = QPushButton("Use")
            use_button.setObjectName("providerPrimary")
            use_button.setCursor(Qt.PointingHandCursor)
            use_button.setToolTip("Use this provider for new requests.")
            use_button.clicked.connect(
                lambda _checked=False, pid=provider_id: self._use_provider(pid))
            layout.addWidget(use_button)

        edit_button = QPushButton("Edit")
        edit_button.setObjectName("providerRowAction")
        edit_button.setCursor(Qt.PointingHandCursor)
        edit_button.clicked.connect(
            lambda _checked=False, pid=provider_id: self._edit_provider(pid))
        layout.addWidget(edit_button)

        if is_custom_provider(provider_id):
            delete_button = QPushButton("Delete")
            delete_button.setObjectName("providerRowAction")
            delete_button.setStyleSheet("color: #E06C75;")
            delete_button.setCursor(Qt.PointingHandCursor)
            delete_button.clicked.connect(
                lambda _checked=False, pid=provider_id: self._delete_custom_provider(pid))
            layout.addWidget(delete_button)
        return row

    def _available_row(self, provider_id):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(12)
        layout.addWidget(self._tile(provider_id))

        info = QVBoxLayout()
        info.setSpacing(2)
        name = QLabel(self._display_name(provider_id))
        name.setObjectName("providerRowName")
        info.addWidget(name)
        description = QLabel(self._description(provider_id))
        description.setObjectName("providerRowDetail")
        description.setWordWrap(True)
        info.addWidget(description)
        layout.addLayout(info, 1)

        connect_button = QPushButton("Connect")
        connect_button.setObjectName("providerPrimary")
        connect_button.setCursor(Qt.PointingHandCursor)
        connect_button.clicked.connect(
            lambda _checked=False, pid=provider_id: self._connect_provider(pid))
        layout.addWidget(connect_button)
        return row

    def _add_custom_row(self):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 2, 8, 2)
        button = QPushButton("+  Add custom provider…")
        button.setObjectName("providerLink")
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(
            "Connect an OpenAI- or Anthropic-compatible endpoint such as "
            "LM Studio, Ollama, llama.cpp, DeepSeek or a company gateway.")
        button.clicked.connect(self._add_custom_provider)
        layout.addWidget(button)
        layout.addStretch(1)
        return row

    # --- staged writes ----------------------------------------------------

    def _apply(self, changes):
        self.settings_manager.set_many(changes, save=False, emit=False)
        self.refresh()
        self.changed.emit()

    def _sync_custom_providers(self):
        try:
            self.settings_manager.sync_custom_providers(
                save=False, delete_keys=False)
        except Exception as exc:
            logger.error("Failed to sync custom providers: %s", exc,
                         exc_info=True)

    def commit(self):
        """Drops keychain entries of deleted custom providers."""
        for provider_id in sorted(self._deleted_custom_ids):
            self.settings_manager.delete_provider_key(provider_id)
        self._deleted_custom_ids.clear()

    # --- actions ----------------------------------------------------------

    def _ensure_providers_ready(self) -> bool:
        """Imports provider SDKs lazily before a provider form is built."""
        if providers_ready():
            return True
        start_provider_warmup()
        dialog = _ProviderLoadingDialog(self)
        timer = QTimer(dialog)
        timer.setInterval(150)

        def check():
            if providers_ready():
                timer.stop()
                dialog.accept()

        timer.timeout.connect(check)
        timer.start()
        dialog.exec()
        timer.stop()
        return providers_ready()

    def _connect_provider(self, provider_id):
        if not self._ensure_providers_ready():
            return
        dialog = ProviderDialog(provider_id, self.settings_manager, self)
        if dialog.exec() != QDialog.Accepted:
            return
        providers = copy.deepcopy(
            self.settings_manager.get("llm_providers", {}) or {})
        entry = providers.get(provider_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(dialog.values)
        providers[provider_id] = entry
        changes = {"llm_providers": providers}
        active = self.settings_manager.get("active_provider", "") or ""
        if not provider_is_configured({"llm_providers": providers}, active):
            changes["active_provider"] = provider_id
        self._apply(changes)

    def _edit_provider(self, provider_id):
        if is_custom_provider(provider_id):
            self._edit_custom_provider(provider_id)
            return
        if not self._ensure_providers_ready():
            return
        dialog = ProviderDialog(provider_id, self.settings_manager, self)
        if dialog.exec() != QDialog.Accepted:
            return
        providers = copy.deepcopy(
            self.settings_manager.get("llm_providers", {}) or {})
        entry = providers.get(provider_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(dialog.values)
        providers[provider_id] = entry
        self._apply({"llm_providers": providers})

    def _use_provider(self, provider_id):
        self._apply({"active_provider": provider_id})

    def _add_custom_provider(self):
        dialog = CustomProviderDialog(self, reserved_ids=set(PROVIDERS))
        if dialog.exec() != QDialog.Accepted or not dialog.definition:
            return
        definition = dialog.definition
        provider_id = definition["id"]
        self._deleted_custom_ids.discard(provider_id)
        definitions = self._custom_definitions()
        definitions.append(definition)
        providers = copy.deepcopy(
            self.settings_manager.get("llm_providers", {}) or {})
        entry = providers.get(provider_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry["api_key"] = dialog.api_key
        providers[provider_id] = entry
        changes = {
            "custom_providers": definitions,
            "llm_providers": providers,
        }
        active = self.settings_manager.get("active_provider", "") or ""
        if not provider_is_configured({"llm_providers": providers}, active):
            changes["active_provider"] = provider_id
        self.settings_manager.set_many(changes, save=False, emit=False)
        self._sync_custom_providers()
        self.refresh()
        self.changed.emit()

    def _edit_custom_provider(self, provider_id):
        definition = self._custom_definition(provider_id)
        if definition is None:
            return
        providers = self.settings_manager.get("llm_providers", {}) or {}
        api_key = (providers.get(provider_id) or {}).get("api_key", "")
        dialog = CustomProviderDialog(
            self, definition=definition, api_key=api_key,
            reserved_ids=set(PROVIDERS))
        if dialog.exec() != QDialog.Accepted or not dialog.definition:
            return
        definitions = [
            dialog.definition if item.get("id") == provider_id else item
            for item in self._custom_definitions()
        ]
        providers = copy.deepcopy(providers)
        entry = providers.get(provider_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry["api_key"] = dialog.api_key
        providers[provider_id] = entry
        self.settings_manager.set_many({
            "custom_providers": definitions,
            "llm_providers": providers,
        }, save=False, emit=False)
        self._sync_custom_providers()
        self.refresh()
    def _delete_custom_provider(self, provider_id):
        definition = self._custom_definition(provider_id)
        if definition is None:
            return
        name = definition.get("name") or provider_id
        answer = QMessageBox.question(
            self, "Delete provider",
            f"Delete the custom provider \"{name}\" ({provider_id})?\n\n"
            "Its settings and API key will be removed.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._deleted_custom_ids.add(provider_id)
        definitions = [
            item for item in self._custom_definitions()
            if item.get("id") != provider_id
        ]
        self.settings_manager.set_many(
            {"custom_providers": definitions}, save=False, emit=False)
        self._sync_custom_providers()
        self.refresh()
        self.changed.emit()

    def _update_models(self):
        self.update_models_button.setEnabled(False)
        self.update_models_button.setText("Updating…")
        try:
            success, message = ModelConfigManager.update_models_from_source()
        finally:
            self.update_models_button.setEnabled(True)
            self.update_models_button.setText("Update model lists")
        if success:
            self.refresh()
        else:
            QMessageBox.information(
                self, "Update model lists",
                f"Failed to update the model list:\n{message}")
