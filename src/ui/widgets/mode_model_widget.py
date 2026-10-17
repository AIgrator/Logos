# src/ui/widgets/mode_model_widget.py
"""Settings widget for the dedicated Vision (computer-use) model.

The general model is chosen in the provider section above (the "Model"
dropdown). Here the user may pick a separate model for Vision mode; it is
used only when the active model cannot do computer use. Only providers with
an API key and computer-use-capable models are listed.
"""
import logging

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
)

from llm.model_config_manager import ModelConfigManager
from llm.mode_capabilities import ModeCapabilities, SUBSECTIONS, SUBSECTION_LABELS

logger = logging.getLogger(__name__)


class ModeModelWidget(QGroupBox):
    """Vision (computer-use) model selector (provider + model)."""

    def __init__(self, settings_manager, parent=None):
        super().__init__("Vision (Computer Use)", parent)
        self.settings_manager = settings_manager
        self.rows = {}

        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        for sub_key in SUBSECTIONS:
            provider_combo = QComboBox()
            model_combo = QComboBox()
            model_combo.setEditable(True)
            # Two separate full-width rows, matching the provider section above
            # (provider on one row, model on the next) so long model ids fit.
            form.addRow("AI Provider:", provider_combo)
            form.addRow(f"{SUBSECTION_LABELS[sub_key]}:", model_combo)
            self.rows[sub_key] = {"provider": provider_combo, "model": model_combo}
            provider_combo.currentIndexChanged.connect(
                lambda _idx, k=sub_key: self._on_provider_changed(k)
            )

        self.load_settings(settings_manager.get("mode_models", {}) or {})

    # --- data helpers -----------------------------------------------------

    def _provider_has_key(self, provider_id: str) -> bool:
        providers = self.settings_manager.get("llm_providers", {}) or {}
        return bool((providers.get(provider_id) or {}).get("api_key"))

    def _models_for_subsection(self, subsection: str) -> list:
        """Returns [(provider_id, stored_model_id, label), ...] for a subsection."""
        result = []
        config = ModelConfigManager.PROVIDER_CONFIG or {}
        for pid, info in config.items():
            if not self._provider_has_key(pid):
                continue
            if pid == "openrouter":
                groups = info.get("models_by_provider") or {}
                for group, models in groups.items():
                    for name in sorted(models or []):
                        stored = f"{group.lower()}/{name.lower().replace(' ', '-')}"
                        if ModeCapabilities.supports_subsection(pid, stored, subsection):
                            result.append((pid, stored, name))
            else:
                for name in sorted(info.get("models") or []):
                    if ModeCapabilities.supports_subsection(pid, name, subsection):
                        result.append((pid, name, name))
        return result

    # --- population -------------------------------------------------------

    def _populate_providers(self, subsection: str, provider_id: str = "", model_id: str = ""):
        combo = self.rows[subsection]["provider"]
        available = self._models_for_subsection(subsection)
        provider_ids = []
        for pid, _mid, _label in available:
            if pid not in provider_ids:
                provider_ids.append(pid)

        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Not set", "")
        for pid in provider_ids:
            display = (ModelConfigManager.PROVIDER_CONFIG.get(pid) or {}).get("display_name") or pid
            combo.addItem(display, pid)
        index = combo.findData(provider_id) if provider_id else 0
        combo.setCurrentIndex(index if index != -1 else 0)
        combo.blockSignals(False)

        self._populate_models(subsection, combo.currentData(), model_id)

    def _populate_models(self, subsection: str, provider_id: str, model_id: str = ""):
        combo = self.rows[subsection]["model"]
        combo.blockSignals(True)
        combo.clear()
        if provider_id:
            for pid, mid, _label in self._models_for_subsection(subsection):
                if pid == provider_id:
                    combo.addItem(mid)
        combo.setCurrentText(model_id or "")
        combo.blockSignals(False)

    def _on_provider_changed(self, subsection: str):
        combo = self.rows[subsection]["provider"]
        self._populate_models(subsection, combo.currentData(), "")

    # --- public API -------------------------------------------------------

    def load_settings(self, mode_models: dict):
        for sub_key in self.rows:
            entry = (mode_models or {}).get(sub_key) or {}
            self._populate_providers(sub_key, entry.get("provider", ""), entry.get("model", ""))

    def get_settings(self) -> dict:
        result = {}
        for sub_key, widgets in self.rows.items():
            pid = widgets["provider"].currentData()
            mid = widgets["model"].currentText().strip()
            if pid and mid:
                result[sub_key] = {"provider": pid, "model": mid}
        return result
