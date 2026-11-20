"""Проба апаратного кодування: шукаємо відеокарту, яка вміє H.264 через VAAPI."""

from __future__ import annotations

import glob
import sys

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from . import ffmpeg_cmd
from .log import log

_TIMEOUT_MS = 8000


_VENDORS = {"0x8086": "GPU Intel", "0x1002": "GPU Radeon", "0x10de": "GPU NVIDIA"}


def encoder_name() -> str:
    """Хто кодує: CPU або який саме GPU."""
    node = ffmpeg_cmd.HW_NODE
    if not node:
        return "CPU"
    try:
        with open("/sys/class/drm/%s/device/vendor" % node.rsplit("/", 1)[-1]) as f:
            return _VENDORS.get(f.read().strip().lower(), "GPU")
    except OSError:
        return "GPU"


class Probe(QObject):
    """Перебирає відеокарти і драйвери по черзі; першу пару, що впоралась, запам'ятовує."""

    done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nodes: list[tuple[str, str | None, bool]] = []
        self._proc: QProcess | None = None

    def start(self):
        if sys.platform.startswith("linux"):
            self._nodes = [(node, driver, low) for node in sorted(glob.glob("/dev/dri/renderD*"))
                           for driver, low in ffmpeg_cmd.HW_VARIANTS]
        self._next()

    def _next(self):
        if not self._nodes:
            log.info("Проба кодера: жоден GPU не кодує — працює CPU")
            self.done.emit()
            return
        node, driver, low = self._nodes.pop(0)
        proc = QProcess(self)
        self._proc = proc
        if driver:
            env = QProcessEnvironment.systemEnvironment()
            env.insert("LIBVA_DRIVER_NAME", driver)
            proc.setProcessEnvironment(env)
        proc.finished.connect(lambda code, _status, n=node, d=driver, lo=low, p=proc: self._on_finished(p, n, d, lo, code))
        proc.errorOccurred.connect(lambda _e, p=proc: self._on_error(p))
        proc.start("ffmpeg", ffmpeg_cmd.hw_probe_args(node, low))
        QTimer.singleShot(_TIMEOUT_MS, lambda p=proc: self._on_timeout(p))

    def _on_finished(self, proc: QProcess, node: str, driver: str | None, low: bool, code: int):
        if proc is not self._proc:
            return
        ok = code == 0 and proc.exitStatus() == QProcess.ExitStatus.NormalExit
        err = bytes(proc.readAllStandardError().data()).decode("utf-8", "replace").strip().splitlines()
        log.info("Проба кодера %s driver=%s low_power=%s: %s", node, driver or "auto", low,
                 "КОДУЄ" if ok else "ні — код %s — %s" % (code, " | ".join(err[:3])[:300]))
        self._proc = None
        proc.deleteLater()
        if ok:
            ffmpeg_cmd.HW_NODE = node
            ffmpeg_cmd.HW_DRIVER = driver
            ffmpeg_cmd.HW_LOW_POWER = low
            self._probe_pipe(node, driver, low)
        else:
            self._next()

    def _probe_pipe(self, node: str, driver: str | None, low: bool):
        """Кодер є. Друге питання: чи вміє GPU ще й приймати кадр, масштабувати й віддавати перегляд."""
        proc = QProcess(self)
        self._proc = proc
        if driver:
            env = QProcessEnvironment.systemEnvironment()
            env.insert("LIBVA_DRIVER_NAME", driver)
            proc.setProcessEnvironment(env)

        def finished(code, status, p=proc):
            if p is not self._proc:
                return
            self._proc = None
            ok = code == 0 and status == QProcess.ExitStatus.NormalExit
            err = bytes(p.readAllStandardError().data()).decode("utf-8", "replace").strip().splitlines()
            log.info("Проба повної обробки на GPU: %s", "ПРАЦЮЄ" if ok else
                     "ні — код %s — %s" % (code, " | ".join(err[:3])[:300]))
            p.deleteLater()
            ffmpeg_cmd.GPU_PIPE = ok
            self.done.emit()

        proc.finished.connect(finished)
        proc.start("ffmpeg", ffmpeg_cmd.hw_pipe_probe_args(node, low))
        QTimer.singleShot(_TIMEOUT_MS, lambda p=proc: self._on_timeout(p))

    def _on_error(self, proc: QProcess):
        if proc is self._proc and proc.error() == QProcess.ProcessError.FailedToStart:
            self._proc = None
            proc.deleteLater()
            self.done.emit()        # ffmpeg немає взагалі — далі перебирати нема сенсу

    def _on_timeout(self, proc: QProcess):
        if proc is self._proc:
            proc.kill()             # finished прийде з кодом збою і піде до наступної карти
