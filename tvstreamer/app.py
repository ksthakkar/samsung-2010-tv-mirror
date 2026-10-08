"""Desktop window for sharing this PC with a Samsung TV."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from tvstreamer.engine import Engine
from tvstreamer.ffmpeg_util import encoder_label, suggest_convert
from tvstreamer.net import ipv4_addresses


class Bridge(QObject):
    log = Signal(str)
    ready = Signal()
    failed = Signal(str)
    sharing = Signal(bool)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Laptop to Samsung TV")
        self.resize(760, 700)
        self.bridge = Bridge()
        self.engine = Engine(self.bridge.log.emit)
        self._sharing = False
        self._build()
        self.bridge.log.connect(self._append)
        self.bridge.ready.connect(self._on_ready)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.sharing.connect(self._on_sharing)
        self._append("Starting…")
        threading.Thread(target=self._bootstrap, daemon=True).start()

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title = QLabel("Laptop to Samsung TV")
        title.setObjectName("title")
        layout.addWidget(title)
        intro = QLabel(
            "DLNA server, same kind of source as Universal Media Server. "
            "On the TV it shows up in the Source list as Laptop Screen."
        )
        intro.setWordWrap(True)
        intro.setObjectName("intro")
        layout.addWidget(intro)

        self.status = QLabel("Starting…")
        self.status.setObjectName("status")
        layout.addWidget(self.status)

        net_row = QHBoxLayout()
        net_row.addWidget(QLabel("Network"))
        self.ip_box = QComboBox()
        self.ip_box.setEnabled(False)
        self.ip_box.currentIndexChanged.connect(self._ip_changed)
        net_row.addWidget(self.ip_box, 1)
        layout.addLayout(net_row)

        mode_row = QHBoxLayout()
        self.mode_desktop = QRadioButton("Mirror the desktop")
        self.mode_file = QRadioButton("Play a video file")
        self.mode_web = QRadioButton("Web video link")
        self.mode_desktop.setChecked(True)
        self.modes = QButtonGroup(self)
        for button in (self.mode_desktop, self.mode_file, self.mode_web):
            self.modes.addButton(button)
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        self.mode_desktop.toggled.connect(self._sync_mode)
        self.mode_file.toggled.connect(self._sync_mode)
        self.mode_web.toggled.connect(self._sync_mode)
        layout.addLayout(mode_row)

        opt_row = QHBoxLayout()
        self.size_label = QLabel("Size")
        opt_row.addWidget(self.size_label)
        self.size_box = QComboBox()
        self.size_box.addItem("1920×1080", (1920, 1080))
        self.size_box.addItem("1280×720, lighter", (1280, 720))
        opt_row.addWidget(self.size_box)
        opt_row.addWidget(QLabel("Encoder"))
        self.encoder_box = QComboBox()
        self.encoder_box.addItem("Auto", "auto")
        opt_row.addWidget(self.encoder_box)
        self.audio_label = QLabel("Audio")
        opt_row.addWidget(self.audio_label)
        self.audio_box = QComboBox()
        self.audio_box.addItem("No audio", "off")
        opt_row.addWidget(self.audio_box, 1)
        layout.addLayout(opt_row)

        self.file_row = QWidget()
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("Choose a video on this PC")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        file_layout.addWidget(self.file_edit, 1)
        file_layout.addWidget(browse)
        layout.addWidget(self.file_row)

        self.convert_box = QCheckBox("Convert so a 2010 TV can play it")
        self.convert_box.setChecked(True)
        layout.addWidget(self.convert_box)

        self.url_row = QWidget()
        url_layout = QHBoxLayout(self.url_row)
        url_layout.setContentsMargins(0, 0, 0, 0)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Video page, or a direct .mp4 / .m3u8 link")
        url_layout.addWidget(self.url_edit)
        layout.addWidget(self.url_row)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("hint")
        layout.addWidget(self.hint)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start sharing")
        self.start_btn.setObjectName("start")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        self.push_btn = QPushButton("Try to start it on the TV")
        self.push_btn.setEnabled(False)
        self.push_btn.clicked.connect(self._push)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addWidget(self.push_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(400)
        layout.addWidget(self.log_view, 1)
        self._sync_mode()
        self.setStyleSheet(_STYLE)

    def _bootstrap(self) -> None:
        try:
            self.engine.bootstrap()
        except Exception as exc:
            self.bridge.failed.emit(str(exc))
            return
        self.bridge.ready.emit()

    def _on_ready(self) -> None:
        self.ip_box.blockSignals(True)
        self.ip_box.clear()
        for ip in ipv4_addresses():
            self.ip_box.addItem(ip, ip)
        index = self.ip_box.findData(self.engine.ip)
        if index >= 0:
            self.ip_box.setCurrentIndex(index)
        self.ip_box.blockSignals(False)
        self.ip_box.setEnabled(True)

        self.encoder_box.clear()
        self.encoder_box.addItem("Auto", "auto")
        for name in self.engine.encoders:
            self.encoder_box.addItem(encoder_label(name), name)

        self.audio_box.clear()
        if self.engine.audio_devices:
            self.audio_box.addItem("System audio", None)
            for device in self.engine.audio_devices:
                label = device["name"] + (" (default)" if device["default"] else "")
                self.audio_box.addItem(label, device["index"])
        self.audio_box.addItem("No audio", "off")

        best = encoder_label(self.engine.encoders[0])
        self.status.setText(f"Ready · {self.engine.ip} · {best}")
        self.start_btn.setEnabled(True)
        self._append("Ready. On the TV, open Source and choose Laptop Screen.")

    def _on_failed(self, message: str) -> None:
        self.status.setText("Not ready")
        self._append(message)

    def _append(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _ip_changed(self) -> None:
        ip = self.ip_box.currentData()
        if ip:
            self.engine.set_ip(ip)
            self.status.setText(f"Ready · {ip}")

    def _sync_mode(self) -> None:
        desktop = self.mode_desktop.isChecked()
        file_mode = self.mode_file.isChecked()
        web = self.mode_web.isChecked()
        self.size_label.setVisible(desktop)
        self.size_box.setVisible(desktop)
        self.audio_label.setVisible(desktop)
        self.audio_box.setVisible(desktop)
        self.file_row.setVisible(file_mode)
        self.convert_box.setVisible(file_mode)
        self.url_row.setVisible(web)
        if desktop:
            extra = ""
            if sys.platform == "darwin":
                extra = " On a Mac, allow Screen Recording if asked. System audio needs a loopback device such as BlackHole."
            elif sys.platform != "win32":
                extra = " On Linux the picture is taken from the X11 screen, and the sound from PulseAudio or PipeWire."
            self.hint.setText(
                "1080p at a steady 30 frames per second. Motion stays even with the laptop." + extra
            )
        elif file_mode:
            self.hint.setText(
                "Best picture for a file you already have. Leave Convert on for MKV and unusual audio. "
                "Turn it off for an MP4 the TV already plays from a USB stick."
            )
        else:
            self.hint.setText(
                "Paste a video page or a direct .mp4 / .m3u8 link. "
                "The laptop converts it at 1080p ahead of the TV, so playback stays smooth. Nothing is saved. "
                "If the page hides the file, mirror the desktop instead."
            )

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose a video",
            "",
            "Videos (*.mp4 *.mkv *.avi *.mov *.m4v *.wmv *.mpg *.mpeg *.ts *.webm);;All files (*.*)",
        )
        if not path:
            return
        self.file_edit.setText(path)
        self.convert_box.setChecked(suggest_convert(Path(path)))

    def _start(self) -> None:
        if self._sharing:
            return
        self.start_btn.setEnabled(False)
        settings = self._collect()
        threading.Thread(target=self._start_worker, args=(settings,), daemon=True).start()

    def _collect(self) -> dict:
        width, height = self.size_box.currentData()
        audio_choice = self.audio_box.currentData()
        if self.mode_file.isChecked():
            mode = "file"
        elif self.mode_web.isChecked():
            mode = "web"
        else:
            mode = "desktop"
        return {
            "mode": mode,
            "width": width,
            "height": height,
            "encoder": self.encoder_box.currentData(),
            "audio": audio_choice != "off",
            "device_index": None if audio_choice in (None, "off") else audio_choice,
            "path": self.file_edit.text().strip(),
            "convert": self.convert_box.isChecked(),
            "url": self.url_edit.text().strip(),
        }

    def _start_worker(self, settings: dict) -> None:
        try:
            if settings["mode"] == "desktop":
                self.engine.share_desktop(
                    settings["width"], settings["height"], settings["encoder"],
                    settings["audio"], settings["device_index"],
                )
            elif settings["mode"] == "file":
                if not settings["path"]:
                    raise RuntimeError("Choose a video file first.")
                self.engine.share_file(Path(settings["path"]), settings["convert"], settings["encoder"])
            else:
                if not settings["url"].startswith("http"):
                    raise RuntimeError("Paste a full link, starting with http.")
                self.engine.share_url(settings["url"], settings["encoder"])
        except Exception as exc:
            self.bridge.log.emit(str(exc))
            self.bridge.sharing.emit(False)
            return
        self.bridge.sharing.emit(True)

    def _stop(self) -> None:
        self.stop_btn.setEnabled(False)
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        try:
            self.engine.stop_share()
        except Exception as exc:
            self.bridge.log.emit(str(exc))
        self.bridge.sharing.emit(False)

    def _push(self) -> None:
        threading.Thread(target=self.engine.push_to_tv, daemon=True).start()

    def _on_sharing(self, active: bool) -> None:
        self._sharing = active
        self.start_btn.setEnabled(not active)
        self.stop_btn.setEnabled(active)
        self.push_btn.setEnabled(active)
        self.ip_box.setEnabled(not active)
        for widget in (self.mode_desktop, self.mode_file, self.mode_web, self.size_box, self.encoder_box, self.audio_box):
            widget.setEnabled(not active)
        if active:
            self.status.setText(f"Sharing · {self.engine.friendly_name} · {self.engine.ip}")
        elif self.engine.port:
            self.status.setText(f"Ready · {self.engine.ip}")

    def closeEvent(self, event) -> None:
        try:
            self.engine.shutdown()
        except Exception:
            pass
        event.accept()


_STYLE = """
QMainWindow, QWidget { background: #17191d; color: #ece8e1; font-family: "Segoe UI"; font-size: 10.5pt; }
QLabel#title { font-size: 18pt; font-weight: 600; }
QLabel#intro, QLabel#hint { color: #b7b1a6; }
QLabel#status { color: #e6c98a; }
QLineEdit, QComboBox, QPlainTextEdit {
    background: #101216; color: #ece8e1; border: 1px solid #343841; border-radius: 4px; padding: 6px;
}
QPlainTextEdit { font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; }
QPushButton { background: #2a303a; border: 1px solid #3d4452; border-radius: 4px; padding: 8px 14px; }
QPushButton:hover { background: #343b48; }
QPushButton#start { background: #e6c98a; color: #1b1710; font-weight: 600; border: none; }
QPushButton#start:disabled { background: #5c5344; color: #1b1710; }
QComboBox QAbstractItemView { background: #101216; color: #ece8e1; selection-background-color: #3d4452; }
"""


def main() -> None:
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
