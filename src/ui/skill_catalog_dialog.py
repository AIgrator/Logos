"""Browse ClawHub and install community Agent Skills."""
import logging
import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from context import skill_catalog


class _Task(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self._work = work

    def run(self):
        try:
            result = self._work()
        except skill_catalog.CatalogError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - report, do not crash the UI
            logging.exception("Skill catalogue task failed")
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.completed.emit(result)


class SkillCatalogDialog(QDialog):
    """Search the public catalogue and install skills into ~/.logos/skills."""

    installed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task = None
        self._items = []

        self.setWindowTitle("Skill Catalogue")
        self.setMinimumSize(640, 520)
        self.resize(760, 560)
        self.setModal(True)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Browse community skills from ClawHub (clawhub.ai). Installed "
            "skills are saved to ~/.logos/skills and become available to the "
            "Terminal and Browser agents on the next request.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search skills by name or task")
        self.search_edit.setToolTip(
            "Searches the public ClawHub registry. Empty search shows the "
            "most downloaded skills.")
        self.search_edit.returnPressed.connect(self._search)
        search_row.addWidget(self.search_edit, 1)

        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self._search)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        self.results_list = QListWidget()
        self.results_list.setMinimumHeight(220)
        self.results_list.setTextElideMode(Qt.ElideRight)
        self.results_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.results_list.itemSelectionChanged.connect(self._update_details)
        layout.addWidget(self.results_list, 1)

        self.details_label = QLabel(
            "Select a skill to see its source and license.")
        self.details_label.setWordWrap(True)
        self.details_label.setStyleSheet("color: #9E9E9E;")
        layout.addWidget(self.details_label)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #9E9E9E;")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.install_button = QPushButton("Install")
        self.install_button.setToolTip(
            "Download the skill and install it for the current user.")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self._install_selected)
        buttons.addWidget(self.install_button)
        buttons.addStretch()

        self.button_box = QDialogButtonBox(QDialogButtonBox.Close)
        self.button_box.rejected.connect(self.reject)
        buttons.addWidget(self.button_box)
        layout.addLayout(buttons)

        self._set_status("Loading popular skills...")
        self._run(lambda: skill_catalog.popular(30), self._show_popular,
                  "Loading popular skills...")

    # -- actions --------------------------------------------------------

    def _search(self):
        query = " ".join(self.search_edit.text().split())
        if not query:
            self._run(lambda: skill_catalog.popular(30), self._show_popular,
                      "Loading popular skills...")
            return
        self._run(
            lambda: skill_catalog.search(query),
            lambda items: self._show_results(
                items, f"Results for \"{query}\"."),
            f"Searching for \"{query}\"...")

    def _show_popular(self, items):
        self._show_results(items, "Most downloaded skills on ClawHub.")

    def _show_results(self, items, status=""):
        self._items = list(items or [])
        self.results_list.clear()
        if not self._items:
            placeholder = QListWidgetItem("No skills found.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.results_list.addItem(placeholder)
            self._set_status("No skills found. Try different words.")
            self._update_details()
            return
        for item in self._items:
            entry = QListWidgetItem()
            entry.setData(Qt.UserRole, item)
            self._fill_item(entry, item)
            self.results_list.addItem(entry)
        self._set_status(status or f"{len(self._items)} skills.")
        self._update_details()

    @staticmethod
    def _fill_item(entry, item):
        label = item["name"]
        if item.get("summary"):
            label += " — " + item["summary"]
        target = skill_catalog.target_path(item["slug"])
        if os.path.isdir(target):
            label += "  (installed)"
        entry.setText(label)
        license_text = item.get("license") or "not declared"
        entry.setToolTip(
            f"{item['summary']}\n\n"
            f"Reference: {item['reference']}\n"
            f"Downloads: {item['downloads']:,}\n"
            f"License: {license_text}\n"
            f"{item['url']}")

    def _selected_item(self):
        entry = self.results_list.currentItem()
        if entry is None:
            return None
        return entry.data(Qt.UserRole)

    def _update_details(self):
        item = self._selected_item()
        if not item:
            self.details_label.setText(
                "Select a skill to see its source and license.")
            self.install_button.setEnabled(False)
            return
        license_text = item.get("license") or "not declared"
        self.details_label.setText(
            f"{item['summary']}\n\n"
            f"Source: {item['reference']} · Downloads: "
            f"{item['downloads']:,} · License: {license_text}")
        self.install_button.setEnabled(self._task is None)

    def _install_selected(self):
        item = self._selected_item()
        if not item:
            return
        target = skill_catalog.target_path(item["slug"])
        overwrite = False
        if os.path.isdir(target):
            answer = QMessageBox.question(
                self, "Replace skill",
                f"\"{item['name']}\" is already installed in:\n{target}\n\n"
                "Replace it with the catalogue version?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            overwrite = True
        self._run(
            lambda: skill_catalog.details(item["owner"], item["slug"]),
            lambda meta: self._confirm_install(item, meta, overwrite),
            "Loading skill details...",
            on_error=self._show_error)

    def _confirm_install(self, item, meta, overwrite):
        license_text = meta.get("license") or "not declared"
        warning = ""
        if not meta.get("license"):
            warning = ("\n\nThe author did not declare a license. Check the "
                       "source before using it.")
        text = (
            f"Install \"{meta['name']}\" from {item['reference']}?\n\n"
            f"License: {license_text}\n"
            f"Downloads: {meta['downloads']:,}\n"
            f"Source: {meta['url']}\n\n"
            "Skills are instructions the agent follows. Install only skills "
            f"you trust.{warning}\n\n"
            f"Install location:\n{skill_catalog.target_path(item['slug'])}")
        answer = QMessageBox.question(
            self, "Install skill", text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._run(
            lambda: skill_catalog.install(
                item["owner"], item["slug"], overwrite=overwrite),
            lambda path: self._installed(meta, path),
            f"Installing \"{meta['name']}\"...",
            on_error=self._show_error)

    def _installed(self, meta, path):
        self._set_status(f"Installed {meta['name']}.")
        self.installed.emit()
        self._show_results(self._items, f"Installed {meta['name']}.")
        QMessageBox.information(
            self, "Skill installed",
            f"\"{meta['name']}\" is installed in:\n{path}\n\n"
            "The Terminal and Browser agents can use it on the next request.")

    def _show_error(self, message):
        self._set_status(message, error=True)
        QMessageBox.warning(self, "Skill catalogue", message)

    # -- task plumbing --------------------------------------------------

    def _run(self, work, on_success, busy_text, on_error=None):
        if self._task is not None:
            return
        self._set_busy(True, busy_text)
        task = _Task(work, self)
        self._task = task
        task.completed.connect(
            lambda result: self._finish(task, on_success, result))
        task.failed.connect(
            lambda message: self._fail(task, message, on_error))
        task.finished.connect(task.deleteLater)
        task.start()

    def _finish(self, task, callback, result):
        if self._task is task:
            self._task = None
        self._set_busy(False)
        callback(result)

    def _fail(self, task, message, on_error):
        if self._task is task:
            self._task = None
        self._set_busy(False)
        if on_error is not None:
            on_error(message)
        else:
            self._set_status(message, error=True)

    def _set_busy(self, busy, text=""):
        self.search_edit.setEnabled(not busy)
        self.search_button.setEnabled(not busy)
        self.results_list.setEnabled(not busy)
        self.install_button.setEnabled(
            not busy and self._selected_item() is not None)
        close = self.button_box.button(QDialogButtonBox.Close)
        if close is not None:
            close.setEnabled(not busy)
        if busy:
            self._set_status(text)

    def _set_status(self, text, error=False):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            "color: #D32F2F;" if error else "color: #9E9E9E;")

    def reject(self):
        if self._task is not None:
            self._set_status("Please wait for the current request to finish.")
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            self._set_status("Please wait for the current request to finish.")
            event.ignore()
            return
        super().closeEvent(event)
