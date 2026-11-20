"""Не давати екрану гаснути, поки відкрите вікно.

Тримаємо запущеним `systemd-inhibit --what=idle … cat`: поки цей процес живий, система
вважає, що за комп'ютером працюють, — екран не гасне і сон через бездіяльність не настає.
`cat` читає наш канал і завершується сам, щойно програма закрилась чи впала, тож
заборона ніколи не лишається висіти після нас.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QProcess

from . import osdep
from .log import log

# Windows: «система потрібна» + «екран потрібен», поки не скасуємо
_ES_CONTINUOUS, _ES_SYSTEM_REQUIRED, _ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


class KeepAwake(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._win_on = False

    def set(self, on: bool):
        if osdep.IS_WIN:
            self._set_windows(on)
            return
        if on == (self._proc is not None):
            return
        if not on:
            proc, self._proc = self._proc, None
            proc.closeWriteChannel()        # cat бачить кінець вводу і виходить
            if not proc.waitForFinished(1500):
                proc.kill()
            proc.deleteLater()
            log.info("Екран: гасіння знову дозволено")
            return
        proc = QProcess(self)
        proc.errorOccurred.connect(lambda _e, p=proc: self._on_error(p))
        proc.start("systemd-inhibit", ["--what=idle", "--who=Eazy Streamer",
                                       "--why=Відкрите вікно стрімів", "--mode=block", "cat"])
        self._proc = proc
        log.info("Екран: гасіння заборонено, поки відкрите вікно")

    def _set_windows(self, on: bool):
        if on == self._win_on:
            return
        import ctypes

        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED if on else 0)
        # Діє, поки живий наш процес: закрились чи впали — Windows скасовує сама
        ok = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
        self._win_on = on
        log.info("Екран: %s%s", "гасіння заборонено, поки відкрите вікно" if on else "гасіння знову дозволено",
                 "" if ok else " (Windows відмовила)")

    def _on_error(self, proc: QProcess):
        if proc is self._proc and proc.error() == QProcess.ProcessError.FailedToStart:
            self._proc = None
            proc.deleteLater()
            log.warning("Екран: немає systemd-inhibit — заборонити гасіння не вдалося")
