"""Не давати екрану гаснути, поки відкрите вікно.

Тримаємо запущеним `systemd-inhibit --what=idle … cat`: поки цей процес живий, система
вважає, що за комп'ютером працюють, — екран не гасне і сон через бездіяльність не настає.
`cat` читає наш канал і завершується сам, щойно програма закрилась чи впала, тож
заборона ніколи не лишається висіти після нас.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QProcess

from .log import log


class KeepAwake(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None

    def set(self, on: bool):
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

    def _on_error(self, proc: QProcess):
        if proc is self._proc and proc.error() == QProcess.ProcessError.FailedToStart:
            self._proc = None
            proc.deleteLater()
            log.warning("Екран: немає systemd-inhibit — заборонити гасіння не вдалося")
