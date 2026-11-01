"""First-use download of the Chromium build that Browser mode drives.

Browser mode controls a real browser, and that binary is not shipped inside
Logos (it would add ~150 MB to every download). The first time the user picks
Browser mode, this dialog says what is about to be downloaded, where it goes,
and then shows the download with progress instead of letting Playwright fail
later with "Executable doesn't exist at ..._MEI...".
"""
import logging
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from src.browser import runtime


class _InstallTask(QThread):
    """Runs the Playwright download off the UI thread."""

    progressed = Signal(str)
    done = Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self):  # noqa: D102 - QThread entry point
        try:
            ok, message = runtime.install_chromium(
                on_progress=self.progressed.emit,
                cancel_flag=self._cancelled.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - never crash the dialog
            logging.exception("Chromium download failed")
            ok, message = False, f"Unexpected error: {exc}"
        self.done.emit(ok, message)


class BrowserEngineDialog(QDialog):
    """Explains the one-time Chromium download and performs it.

    Accepts when Chromium is ready to use; rejects when the user declines,
    cancels or the download fails.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task = None
        self._downloading = False

        self.setWindowTitle("Browser engine")
        self.setMinimumWidth(560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Browser mode needs Chromium")
        title.setStyleSheet("font-weight: 600; font-size: 14pt;")
        layout.addWidget(title)

        self._body = QLabel()
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.PlainText)
        self._body.setText(
            "Browser mode drives a real Chromium browser, the same engine family "
            "as Google Chrome. It is not bundled with Logos — that would add about "
            "150 MB to every download — so it is fetched once, on first use.\n\n"
            f"Download size: {runtime.DOWNLOAD_SIZE}\n"
            f"Saved to: {runtime.browsers_dir()}\n\n"
            "The download needs an internet connection, usually takes one to five "
            "minutes and is reused by every later Browser-mode session. Nothing "
            "else on your computer is touched."
        )
        layout.addWidget(self._body)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate: the driver reports no stable percent
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel_button = QPushButton("Not now")
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        buttons.addWidget(self._cancel_button)
        self._download_button = QPushButton("Download Chromium")
        self._download_button.setDefault(True)
        self._download_button.clicked.connect(self._start_download)
        buttons.addWidget(self._download_button)
        layout.addLayout(buttons)

    # -- download -------------------------------------------------------

    def _start_download(self) -> None:
        if self._downloading:
            return
        self._downloading = True
        self._download_button.setEnabled(False)
        self._cancel_button.setText("Cancel download")
        self._status.setText("Downloading Chromium… this can take a few minutes.")
        self._progress.setVisible(True)
        self._task = _InstallTask(self)
        self._task.progressed.connect(self._on_progress)
        self._task.done.connect(self._on_done)
        self._task.start()

    def _on_progress(self, line: str) -> None:
        if self._downloading:
            self._status.setText(line)

    def _on_done(self, ok: bool, message: str) -> None:
        self._downloading = False
        self._progress.setVisible(False)
        if ok:
            self._status.setText(message)
            self.accept()
            return
        if message == "cancelled":
            self._status.setText("Download cancelled. Browser mode stays unavailable until Chromium is installed.")
        else:
            self._status.setText(message)
        self._download_button.setEnabled(True)
        self._download_button.setText("Try again")
        self._cancel_button.setText("Not now")

    def _on_cancel_clicked(self) -> None:
        if self._downloading:
            self._status.setText("Stopping the download…")
            self._cancel_button.setEnabled(False)
            if self._task is not None:
                self._task.cancel()
            return
        self.reject()

    # -- lifecycle ------------------------------------------------------

    def reject(self) -> None:  # noqa: D102 - QDialog override
        if self._downloading:
            if self._task is not None:
                self._task.cancel()
                self._task.wait(15000)
            self._downloading = False
        super().reject()

    def closeEvent(self, event):  # noqa: D102 - QDialog override
        if self._task is not None and self._task.isRunning():
            self._task.cancel()
            self._task.wait(15000)
        super().closeEvent(event)
