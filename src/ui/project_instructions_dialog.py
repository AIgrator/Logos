"""The "Project instructions" dialog: one button, one research pass, one file.

The button is the whole feature. It runs the agentic pass in
``context/instructions_research.py`` - the model walks the project read-only,
reads the manifests and the code that wires it together, and writes the file -
then the result is checked against the same facts the deterministic scanner
proved, and every line that cannot be supported is dropped and shown to the
user. Without a configured model the scanner's own scaffold stands in, so the
button still produces something useful offline. Nothing is written until the
user presses the save button.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading

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

from context import instructions_research, project_instructions

WARN_BYTES = project_instructions.MAX_DRAFT_BYTES
TARGET_BYTES = 6 * 1024

#: A pass that outlives its dialog (the user pressed Cancel while a request was
#: in flight) is kept alive here, with its signals disconnected, until it
#: finishes. Destroying a running QThread would abort the process, and the pass
#: only reads files, so letting it end quietly is safe.
_RETIRED_TASKS: set[_ResearchTask] = set()


def _friendly_error(exc: Exception) -> str:
    """One readable line instead of a raw provider traceback in the dialog."""
    text = str(exc or "").strip()
    low = text.lower()
    if "429" in low or "rate-limited" in low or "rate limit" in low or "quota" in low:
        return ("The provider is rate-limited right now (429). Try again in a minute, "
                "or edit the draft by hand.")
    if "401" in low or "403" in low or "api key" in low or "authentication" in low:
        return "The provider rejected the request: check the API key in Settings → Providers."
    if "timed out" in low or "timeout" in low:
        return "The provider did not answer in time. Try again."
    first = text.splitlines()[0] if text else type(exc).__name__
    return first[:300]


class _ResearchTask(QThread):
    """The whole pass, off the UI thread."""

    progress = Signal(str, str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, project_root, settings_manager, settings, parent=None):
        super().__init__(parent)
        self._project_root = project_root
        self._settings_manager = settings_manager
        self._settings = settings
        self.cancel_event = threading.Event()

    def run(self):  # noqa: D102 - QThread entry point
        try:
            result = instructions_research.research(
                self._project_root,
                self._settings_manager,
                self._settings,
                on_progress=self.progress.emit,
                cancel_event=self.cancel_event,
            )
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001 - never crash the dialog
            logging.warning("Project instructions pass failed: %s", exc)
            self.failed.emit(_friendly_error(exc))


class ProjectInstructionsDialog(QDialog):
    """Generates, edits and saves ``<project>/AGENTS.md``."""

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
        self.setMinimumSize(820, 640)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "AGENTS.md is the file an AI agent reads at the start of every session: what the "
            "program does, how to run and build it, what not to touch. One pass reads the "
            "project's manifests, configs and code, then writes the file. Nothing is written "
            "to disk until you press the save button.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Press “Generate AGENTS.md” to let the model read the project and write the file, "
            "or write it yourself.")
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

        self.trail = QTreeWidget()
        self.trail.setColumnCount(2)
        self.trail.setHeaderLabels(["Step", "What it looked at"])
        self.trail.setRootIsDecorated(False)
        self.trail.setAlternatingRowColors(True)
        self.trail.setMaximumHeight(150)
        self.trail.setVisible(False)
        layout.addWidget(self.trail)

        self.dropped = QLabel("")
        self.dropped.setWordWrap(True)
        self.dropped.setTextFormat(Qt.PlainText)
        self.dropped.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.dropped.setVisible(False)
        layout.addWidget(self.dropped)

        buttons = QHBoxLayout()
        self.generate_button = QPushButton("Generate AGENTS.md")
        self.generate_button.setToolTip(
            "Reads the project's manifests, configs and code, then writes the file. "
            "Nothing changes on disk until you save.")
        self.generate_button.setDefault(True)
        self.generate_button.clicked.connect(self._run_generate)
        buttons.addWidget(self.generate_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton("Save changes" if self._existing else "Create AGENTS.md")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        # Connected after the buttons exist: setting the existing file above
        # fires textChanged, and that handler touches the save button.
        self.editor.textChanged.connect(self._update_size)
        self._update_size()
        if self._existing:
            self.status.setText(
                f"AGENTS.md already exists ({self._size_text()}). Generating improves it in "
                "place; other tools read the same file.")
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
        self.generate_button.setText(
            "Regenerate AGENTS.md" if text.strip() else "Generate AGENTS.md")
        self.size_label.setText(f"{self._size_text()}{suffix}")

    def _ensure_scan(self):
        if self.scan_result is None:
            self.scan_result = project_instructions.scan(self.project_root)
        return self.scan_result

    # -- generation -----------------------------------------------------

    def _run_generate(self) -> None:
        if self._task is not None and self._task.isRunning():
            return
        self.trail.clear()
        self.trail.setVisible(True)
        self.dropped.setVisible(False)
        self.check_label.setVisible(False)
        self.dropped_lines = []
        self.generate_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status.setText("Reading the project: manifests, configs, entry points…")
        self._task = _ResearchTask(
            self.project_root, self.settings_manager, self.settings)
        self._task.progress.connect(self._on_progress)
        self._task.finished_ok.connect(self._on_generated)
        self._task.failed.connect(self._on_failed)
        self._task.start()

    def _on_progress(self, tool: str, detail: str) -> None:
        self.trail.addTopLevelItem(QTreeWidgetItem([tool, detail]))
        self.trail.scrollToBottom()
        self.status.setText(f"Step {self.trail.topLevelItemCount()}: {tool} {detail}".strip())

    def _on_generated(self, result) -> None:
        scan = self._ensure_scan()
        if result.evidence_commands:
            # A command printed by a file the pass actually read is evidence in
            # the same sense a manifest line is, so it is not dropped as invented.
            scan = dataclasses.replace(
                scan, readme_commands=list(dict.fromkeys(
                    list(scan.readme_commands) + list(result.evidence_commands))))
            self.scan_result = scan
        cleaned, dropped = project_instructions.clean_draft(result.text, scan)
        self.dropped_lines = dropped
        self.editor.setPlainText(cleaned)
        self._show_dropped()
        self._show_check()
        self.trail.addTopLevelItem(
            QTreeWidgetItem(["File written", f"{self._size_text()} · {result.summary()}"]))
        self.trail.scrollToBottom()
        self._finish_task()
        self.status.setText(
            f"{result.summary()}. Draft ready · {self._size_text()}. Review it before saving: "
            "the model may only use commands found in the project.")

    def _on_failed(self, message: str) -> None:
        self._finish_task()
        if "No model is configured" in message:
            # One button must still work offline: the scanner's own scaffold is
            # deterministic, so it stands in for the model and says so.
            try:
                scan = self._ensure_scan()
            except Exception as exc:  # noqa: BLE001 - a scan failure must not close the dialog
                logging.exception("Project scan failed")
                self.status.setText(f"Scan failed: {exc}")
                return
            if not self.editor.toPlainText().strip():
                self.editor.setPlainText(project_instructions.render_template(scan))
            self._show_check()
            self.status.setText(
                "No model is configured, so this is the scanner's own scaffold: "
                "facts from the manifests and the README, no prose. Connect a provider in "
                "Settings → Providers for a written file.")
            return
        self.status.setText(f"Generation failed: {message}")

    def _finish_task(self) -> None:
        self.generate_button.setEnabled(True)
        self.save_button.setEnabled(bool(self.editor.toPlainText().strip()))
        if self._task is not None:
            self._task.deleteLater()
            self._task = None

    # -- checks ---------------------------------------------------------

    def _show_check(self) -> None:
        """Names the commands in the editor that nothing in the project backs.

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
        task = self._task
        self._task = None
        if task is not None and task.isRunning():
            task.cancel_event.set()
            for signal in (task.progress, task.finished_ok, task.failed):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            _RETIRED_TASKS.add(task)
            task.finished.connect(lambda: _RETIRED_TASKS.discard(task))
        super().reject()
