"""First-use download of the Chromium build that Browser mode drives.

Browser mode controls a real browser, and that binary is not shipped inside
Logos (it would make every download hundreds of megabytes bigger). The first
time the user picks Browser mode, this dialog says what is about to be
downloaded, how much space it takes and where it goes, then shows a real
progress bar instead of letting Playwright fail later with
"Executable doesn't exist at ..._MEI...".
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
        self._tracker = runtime.DownloadProgress()

        self.setWindowTitle("Browser engine")
        self.setMinimumWidth(620)
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
            "Browser mode drives a real Chromium browser — the engine inside Google "
            "Chrome — which is not part of Windows. It is not shipped inside Logos, "
            "because that would make every Logos download hundreds of megabytes "
            "bigger, so it is fetched once, the first time you use Browser mode.\n\n"
            f"Download: {runtime.DOWNLOAD_SIZE}\n"
            f"On disk after unpacking: {runtime.DISK_SIZE}\n"
            f"Saved to: {runtime.browsers_dir()}\n\n"
            "It comes as four parts one after another — Chromium, its headless "
            "shell, FFmpeg and winldd — so the name and size under the bar change "
            "a few times while the bar keeps filling.\n\n"
            "It needs an internet connection and usually takes one to five minutes. "
            "Later Browser-mode sessions reuse the same copy, and updating or "
            "reinstalling Logos keeps it, so this happens once per computer. "
            "Nothing else on your computer is changed."
        )
        layout.addWidget(self._body)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._status)

        # One bar for the whole download: the driver reports 0-100 % for each
        # archive separately, so a raw percentage would drop back to zero
        # between them. DownloadProgress turns that into a single forward
        # moving value, and the status line names the file being fetched.
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("%p%")
        self._progress.setTextVisible(True)
        self._progress.setMinimumHeight(24)
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
        self._tracker = runtime.DownloadProgress()
        self._download_button.setEnabled(False)
        self._cancel_button.setEnabled(True)
        self._cancel_button.setText("Cancel download")
        self._status.setText("Preparing the download…")
        self._progress.setFormat("%p%")
        self._progress.setRange(0, 0)  # busy until a percentage shows up
        self._progress.setVisible(True)
        self._task = _InstallTask(self)
        self._task.progressed.connect(self._on_progress)
        self._task.done.connect(self._on_done)
        self._task.start()

    def _on_progress(self, line: str) -> None:
        if not self._downloading:
            return
        percent, status = self._tracker.feed(line)
        if status:
            self._status.setText(status)
        if percent is None:
            return
        if self._progress.maximum() != 100:
            self._progress.setRange(0, 100)
        self._progress.setValue(percent)
        done = self._tracker.downloaded_mb
        self._progress.setFormat(f"%p% · {done:.0f} of {runtime.TOTAL_LABEL_MB:.0f} MB")

    def _on_done(self, ok: bool, message: str) -> None:
        self._downloading = False
        self._progress.setVisible(False)
        self._cancel_button.setEnabled(True)
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
