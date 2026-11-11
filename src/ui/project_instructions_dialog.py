"""The "Project instructions" dialog: scan a project, draft AGENTS.md, review it.

Two drafts exist on purpose. The scan-only template is built from facts the
scanner proved and needs no model; the model draft is nicer prose but is then
checked against the same facts, and every line it cannot support is dropped and
shown to the user. Nothing is written until the user presses the save button.
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from context import project_instructions

WARN_BYTES = project_instructions.MAX_DRAFT_BYTES
TARGET_BYTES = 6 * 1024


def _friendly_error(exc: Exception) -> str:
    """One readable line instead of a raw provider traceback in the dialog."""
    text = str(exc or "").strip()
    low = text.lower()
    if "429" in low or "rate-limited" in low or "rate limit" in low or "quota" in low:
        return ("The provider is rate-limited right now (429). Try again in a minute, "
                "or use the scan-only draft.")
    if "401" in low or "403" in low or "api key" in low or "authentication" in low:
        return "The provider rejected the request: check the API key in Settings → Providers."
    if "timed out" in low or "timeout" in low:
        return "The provider did not answer in time. Try again, or use the scan-only draft."
    first = text.splitlines()[0] if text else type(exc).__name__
    return first[:300]


class _DraftTask(QThread):
    """One model call for the draft, off the UI thread."""

    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, settings_manager, settings, system_prompt, user_prompt, parent=None):
        super().__init__(parent)
        self._settings_manager = settings_manager
        self._settings = settings
        self._system_prompt = system_prompt
        self._user_prompt = user_prompt

    def run(self):  # noqa: D102 - QThread entry point
        try:
            from llm.llm_worker import create_llm_provider

            provider = create_llm_provider(self._settings_manager, self._settings)
            if provider is None:
                self.failed.emit(
                    "No model is configured. Connect a provider in Settings → Providers, "
                    "or use the scan-only draft.")
                return
            response = provider.get_response(
                prompt=self._user_prompt,
                attachments=[],
                system_prompt=self._system_prompt,
                history=[],
                require_structured=False,
            )
            text = ""
            if isinstance(response, dict):
                text = response.get("text") or ""
            text = str(text or "").strip()
            if not text:
                self.failed.emit("The model returned an empty draft.")
                return
            self.finished_ok.emit(text)
        except Exception as exc:  # noqa: BLE001 - never crash the dialog
            logging.warning("Project instructions draft failed: %s", exc)
            self.failed.emit(_friendly_error(exc))


class ProjectInstructionsDialog(QDialog):
    """Shows, drafts, edits and saves ``<project>/AGENTS.md``."""

    def __init__(self, project_root: str, settings_manager, settings, parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self.settings_manager = settings_manager
        self.settings = settings
        self.scan_result = None
        self.dropped_lines: list[str] = []
        self._task = None
        self._existing = project_instructions.read_raw(project_root)

        name = os.path.basename(os.path.abspath(project_root).rstrip("\\/")) or project_root
        self.setWindowTitle(f"Project instructions — {name}")
        self.setMinimumSize(820, 620)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "AGENTS.md is the file an AI agent reads at the start of every session: how to run the "
            "project, how to test it, what not to touch. Logos reads it from the project root and "
            "never rewrites it on its own — this dialog only prepares a draft.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Press “Scan project” to build a draft from the project's own manifests, or write the file yourself.")
        fixed = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        fixed.setPointSize(max(9, fixed.pointSize()))
        self.editor.setFont(fixed)
        self.editor.setTabStopDistance(self.editor.fontMetrics().horizontalAdvance(" ") * 4)
        if self._existing:
            self.editor.setPlainText(self._existing)
        layout.addWidget(self.editor, 1)

        self.size_label = QLabel("")
        layout.addWidget(self.size_label)

        self.check_label = QLabel("")
        self.check_label.setWordWrap(True)
        self.check_label.setTextFormat(Qt.PlainText)
        self.check_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.check_label.setVisible(False)
        layout.addWidget(self.check_label)

        self.evidence = QTreeWidget()
        self.evidence.setColumnCount(2)
        self.evidence.setHeaderLabels(["Found in the project", "Source"])
        self.evidence.setRootIsDecorated(False)
        self.evidence.setAlternatingRowColors(True)
        self.evidence.setMaximumHeight(150)
        self.evidence.setVisible(False)
        layout.addWidget(self.evidence)

        self.dropped = QLabel("")
        self.dropped.setWordWrap(True)
        self.dropped.setTextFormat(Qt.PlainText)
        self.dropped.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.dropped.setVisible(False)
        layout.addWidget(self.dropped)

        buttons = QHBoxLayout()
        self.scan_button = QPushButton("Scan project")
        self.scan_button.setToolTip("Reads manifests, scripts and CI; changes nothing on disk.")
        self.scan_button.clicked.connect(self._run_scan)
        buttons.addWidget(self.scan_button)
        self.draft_button = QPushButton("Draft with model")
        self.draft_button.setToolTip("Asks the active model to write the file from the scanned facts.")
        self.draft_button.clicked.connect(self._run_draft)
        buttons.addWidget(self.draft_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton("Save changes" if self._existing else "Create AGENTS.md")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        # Connected after the buttons exist: setting the existing file above
        # fires textChanged, and that handler touches the save button.
        self.editor.textChanged.connect(self._update_size)
        self._update_size()
        if self._existing:
            self.status.setText(
                f"AGENTS.md already exists ({self._size_text()}). Editing it replaces the file; "
                "other tools read the same file.")
        else:
            self.status.setText("No AGENTS.md in this project yet.")

    # -- state ----------------------------------------------------------

    def _size_text(self) -> str:
        text = self.editor.toPlainText()
        size = len(text.encode("utf-8"))
        lines = len(text.splitlines())
        return f"{size / 1024:.1f} KB · {lines} lines"

    def _update_size(self) -> None:
        text = self.editor.toPlainText()
        size = len(text.encode("utf-8"))
        suffix = ""
        if size > WARN_BYTES:
            suffix = "  — too long: every line is sent with every request"
        elif size > TARGET_BYTES:
            suffix = "  — long; aim for a few kilobytes"
        self.save_button.setEnabled(bool(text.strip()))
        self.save_button.setText("Save changes" if self._existing else "Create AGENTS.md")
        self.size_label.setText(f"{self._size_text()}{suffix}")

    # -- scan -----------------------------------------------------------

    def _ensure_scan(self):
        if self.scan_result is None:
            self.scan_result = project_instructions.scan(self.project_root)
        return self.scan_result

    def _run_scan(self) -> None:
        self.scan_button.setEnabled(False)
        try:
            result = self._ensure_scan()
        except Exception as exc:  # noqa: BLE001 - a scan failure must not close the dialog
            logging.exception("Project scan failed")
            self.status.setText(f"Scan failed: {exc}")
            self.scan_button.setEnabled(True)
            return
        self.evidence.clear()
        for fact in result.facts:
            item = QTreeWidgetItem([fact.text, fact.source])
            self.evidence.addTopLevelItem(item)
        self.evidence.resizeColumnToContents(0)
        self.evidence.setVisible(bool(result.facts))
        if not self.editor.toPlainText().strip():
            self.editor.setPlainText(project_instructions.render_template(result))
        commands = len(result.commands)
        self._show_check()
        self.status.setText(
            f"Scanned: {len(result.facts)} facts, {commands} commands from manifests. "
            "Nothing was written to disk.")
        self.scan_button.setEnabled(True)

    def _show_check(self) -> None:
        """Names the commands in the editor that no manifest backs.

        Wording matters: the file is the user's, and a command can be real
        without appearing in a manifest (a plain ``git status``). So this is a
        "check these" list, never a "these are wrong" verdict.
        """
        text = self.editor.toPlainText()
        unknown = project_instructions.audit(text, self._ensure_scan()) if text.strip() else []
        if not unknown:
            self.check_label.setVisible(False)
            self.check_label.setText("")
            return
        preview = ", ".join(f"`{item}`" for item in unknown[:5])
        more = "" if len(unknown) <= 5 else f" (+{len(unknown) - 5} more)"
        if len(unknown) == 1:
            head = "1 command in the file is not in any project manifest — keep it only if it really works"
        else:
            head = (f"{len(unknown)} commands in the file are not in any project manifest — "
                    "keep them only if they really work")
        self.check_label.setText(f"{head}: {preview}{more}")
        self.check_label.setVisible(True)

    # -- model draft ----------------------------------------------------

    def _run_draft(self) -> None:
        if self._task is not None and self._task.isRunning():
            return
        result = self._ensure_scan()
        if not result.stack and not result.commands and not result.readme_commands:
            self.status.setText(
                "Nothing to base a draft on: this project has no manifests (package.json, pyproject.toml, "
                "Makefile…) and no commands in its README. Add one and scan again, or write the file yourself.")
            return
        system_prompt, user_prompt = project_instructions.draft_prompts(result)
        self.draft_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status.setText("Drafting with the model…")
        self._task = _DraftTask(self.settings_manager, self.settings, system_prompt, user_prompt, self)
        self._task.finished_ok.connect(self._on_draft_ok)
        self._task.failed.connect(self._on_draft_failed)
        self._task.start()

    def _on_draft_ok(self, text: str) -> None:
        result = self._ensure_scan()
        cleaned, dropped = project_instructions.clean_draft(text, result)
        self.dropped_lines = dropped
        self.editor.setPlainText(cleaned)
        self._show_dropped()
        self._show_check()
        self._finish_task()
        self.status.setText(
            f"Draft ready · {self._size_text()}. Review it before saving: "
            "the model may only use commands found in the project.")

    def _on_draft_failed(self, message: str) -> None:
        self._finish_task()
        self.status.setText(f"Draft failed: {message}")

    def _finish_task(self) -> None:
        self.draft_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.save_button.setEnabled(bool(self.editor.toPlainText().strip()))

    def _show_dropped(self) -> None:
        if not self.dropped_lines:
            self.dropped.setVisible(False)
            return
        preview = "\n".join(f"• {line}" for line in self.dropped_lines[:6])
        more = "" if len(self.dropped_lines) <= 6 else f"\n… and {len(self.dropped_lines) - 6} more"
        self.dropped.setText(
            "Not included (not supported by the project's manifests, or too generic to be useful):\n"
            + preview + more)
        self.dropped.setVisible(True)

    # -- save -----------------------------------------------------------

    def _save(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            return
        try:
            project_instructions.write(self.project_root, text)
        except OSError as exc:
            self.status.setText(f"Could not write AGENTS.md: {exc}")
            return
        self._existing = True
        self.accept()

    # -- lifecycle ------------------------------------------------------

    def reject(self) -> None:  # noqa: D102 - QDialog override
        if self._task is not None and self._task.isRunning():
            self._task.wait(5000)
        super().reject()
