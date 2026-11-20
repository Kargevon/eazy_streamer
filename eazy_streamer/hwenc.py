"""Проба апаратного кодування: шукаємо відеокарту, яка вміє H.264 через VAAPI."""

from __future__ import annotations

import glob
import sys

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from . import ffmpeg_cmd

_TIMEOUT_MS = 8000


class Probe(QObject):
    """Перебирає відеокарти по черзі; першу, що впоралась, записує в ffmpeg_cmd.HW_NODE."""

    done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nodes: list[str] = []
        self._proc: QProcess | None = None

    def start(self):
        if sys.platform.startswith("linux"):
            self._nodes = sorted(glob.glob("/dev/dri/renderD*"))
        self._next()

    def _next(self):
        if not self._nodes:
            self.done.emit()
            return
        node = self._nodes.pop(0)
        proc = QProcess(self)
        self._proc = proc
        proc.finished.connect(lambda code, _status, n=node, p=proc: self._on_finished(p, n, code))
        proc.errorOccurred.connect(lambda _e, p=proc: self._on_error(p))
        proc.start("ffmpeg", ffmpeg_cmd.hw_probe_args(node))
        QTimer.singleShot(_TIMEOUT_MS, lambda p=proc: self._on_timeout(p))

    def _on_finished(self, proc: QProcess, node: str, code: int):
        if proc is not self._proc:
            return
        ok = code == 0 and proc.exitStatus() == QProcess.ExitStatus.NormalExit
        self._proc = None
        proc.deleteLater()
        if ok:
            ffmpeg_cmd.HW_NODE = node
            self.done.emit()
        else:
            self._next()

    def _on_error(self, proc: QProcess):
        if proc is self._proc and proc.error() == QProcess.ProcessError.FailedToStart:
            self._proc = None
            proc.deleteLater()
            self.done.emit()        # ffmpeg немає взагалі — далі перебирати нема сенсу

    def _on_timeout(self, proc: QProcess):
        if proc is self._proc:
            proc.kill()             # finished прийде з кодом збою і піде до наступної карти
