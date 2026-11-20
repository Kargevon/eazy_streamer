"""Стан мережі кожного ефіру. RTMP іде по TCP, тому «втрачених пакетів» у потоці немає:
загублене TCP надсилає ще раз. Саме ці повтори ми й рахуємо — утилітою ss."""

from __future__ import annotations

import re

from PySide6.QtCore import QObject, QProcess, QTimer

_PERIOD_MS = 4000


class NetWatch(QObject):
    def __init__(self, engines, parent=None):
        """engines — функція, що повертає живі рушії (у кожного є pid() і словник net)."""
        super().__init__(parent)
        self._engines = engines
        self._prev: dict[int, tuple[int, int]] = {}    # pid -> (відправлено, з них повторно)
        self._proc: QProcess | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(_PERIOD_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _poll(self):
        if self._proc is not None or not any(e.pid() for e in self._engines()):
            return
        proc = QProcess(self)
        self._proc = proc
        proc.finished.connect(lambda *_: self._on_finished(proc))
        proc.errorOccurred.connect(lambda _e: self._on_error(proc))
        # -t TCP, -i подробиці з'єднання, -n без назв, -H без шапки, -p чий це сокет
        proc.start("ss", ["-tinHp"])

    def _on_error(self, proc: QProcess):
        if proc is self._proc and proc.error() == QProcess.ProcessError.FailedToStart:
            self._proc = None
            proc.deleteLater()
            self._timer.stop()      # ss немає — більше не пробуємо

    def _on_finished(self, proc: QProcess):
        if proc is not self._proc:
            return
        self._proc = None
        text = bytes(proc.readAllStandardOutput().data()).decode("utf-8", "replace")
        proc.deleteLater()
        seen = parse(text)
        for engine in self._engines():
            pid = engine.pid()
            if not pid or pid not in seen:
                continue
            sent, retrans, rtt = seen[pid]
            prev = self._prev.get(pid)
            self._prev[pid] = (sent, retrans)
            net = {"rtt": rtt, "retrans_total": retrans}
            if prev and sent > prev[0]:
                net["loss"] = max(0.0, 100.0 * (retrans - prev[1]) / (sent - prev[0]))
            engine.net = net
        for pid in [p for p in self._prev if p not in seen]:
            del self._prev[pid]


def parse(text: str) -> dict[int, tuple[int, int, float]]:
    """Вивід `ss -tinHp` -> pid: (байт відправлено, байт повторно, затримка мс)."""
    result: dict[int, tuple[int, int, float]] = {}
    pids: list[int] = []
    for line in text.splitlines():
        if line[:1] not in (" ", "\t"):
            pids = [int(p) for p in re.findall(r"pid=(\d+)", line)]
            continue
        sent = re.search(r"\bbytes_sent:(\d+)", line)
        if not sent or not pids:
            continue
        retrans = re.search(r"\bbytes_retrans:(\d+)", line)     # поля немає, поки повторів не було
        rtt = re.search(r"\brtt:([\d.]+)", line)
        for pid in pids:
            result[pid] = (int(sent.group(1)), int(retrans.group(1)) if retrans else 0,
                           float(rtt.group(1)) if rtt else 0.0)
        pids = []
    return result
