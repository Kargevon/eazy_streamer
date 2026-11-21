"""Стан мережі кожного ефіру. RTMP іде по TCP, тому «втрачених пакетів» у потоці немає:
загублене TCP надсилає ще раз. Ядро Linux веде облік кожного з'єднання — його ми й читаємо
утилітою ss: скільки довелось надіслати повторно, скільки справді дійшло і скільки даних
стоїть у черзі, бо мережа не забирає.
"""

from __future__ import annotations

import re
import time

from PySide6.QtCore import QObject, QProcess, QTimer

_PERIOD_MS = 4000


class NetWatch(QObject):
    def __init__(self, engines, parent=None):
        """engines — функція, що повертає живі рушії (у кожного є pid() і словник net)."""
        super().__init__(parent)
        self._engines = engines
        self._prev: dict[int, dict] = {}    # pid -> попередній замір
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
        now = time.monotonic()
        for engine in self._engines():
            pid = engine.pid()
            if not pid or pid not in seen:
                continue
            cur = dict(seen[pid], at=now)
            prev = self._prev.get(pid)
            self._prev[pid] = cur
            net = {"rtt": cur["rtt"], "retrans_total": cur["retrans"], "queued": cur["notsent"],
                   "app_limited": cur["app_limited"], "kernel_kbit": cur["delivery"] / 1000}
            if prev and cur["sent"] > prev["sent"]:
                net["loss"] = max(0.0, 100.0 * (cur["retrans"] - prev["retrans"]) / (cur["sent"] - prev["sent"]))
            if prev and now > prev["at"]:
                # скільки справді дійшло до того боку за цей проміжок — це і є ширина каналу, якщо впираємось
                net["through_kbit"] = max(0, cur["acked"] - prev["acked"]) * 8 / 1000 / (now - prev["at"])
            # Мережа не встигає: ядро не каже «програма шле мало», і в черзі на відправку щось лежить.
            # Два заміри поспіль, щоб не блимало. Поріг — у байтах, а не в секундах потоку: черга
            # не росте понад буфер сокета (близько 2,5 МБ), і на 40 Мбіт/с «пів секунди потоку»
            # у нього просто не влазить — саме так перший варіант правила проспав повний буфер.
            jam = (not cur["app_limited"]) and cur["notsent"] > 200_000
            net["congested"] = bool(jam and prev and prev.get("jam"))
            # Хто саме тримає: якщо більшу частину проміжку з'єднання стояло через «вікно приймача»,
            # то це сервер не забирає дані; інакше — канал до нього
            if prev and now > prev["at"]:
                net["server_slow"] = (cur["rwnd_ms"] - prev["rwnd_ms"]) / 1000 / (now - prev["at"]) > 0.5
            cur["jam"] = jam
            engine.net = net
        for pid in [p for p in self._prev if p not in seen]:
            del self._prev[pid]


def parse(text: str) -> dict[int, dict]:
    """Вивід `ss -tinHp` -> pid: числа з'єднання (байти, мс, біт/с)."""
    result: dict[int, dict] = {}
    pids: list[int] = []
    for line in text.splitlines():
        if line[:1] not in (" ", "\t"):
            pids = [int(p) for p in re.findall(r"pid=(\d+)", line)]
            continue
        sent = re.search(r"\bbytes_sent:(\d+)", line)
        if not sent or not pids:
            continue

        def number(pattern: str) -> float:
            found = re.search(pattern, line)        # поля немає, поки воно нульове
            return float(found.group(1)) if found else 0.0

        info = {"sent": int(sent.group(1)), "retrans": int(number(r"\bbytes_retrans:(\d+)")),
                "acked": int(number(r"\bbytes_acked:(\d+)")), "notsent": int(number(r"\bnotsent:(\d+)")),
                "rtt": number(r"\brtt:([\d.]+)"), "delivery": number(r"\bdelivery_rate (\d+)bps"),
                "app_limited": bool(re.search(r"\bapp_limited\b", line)),
                # скільки мілісекунд з'єднання стояло, бо той бік не встигав приймати
                "rwnd_ms": number(r"\brwnd_limited:(\d+)ms")}
        for pid in pids:
            result[pid] = info
        pids = []
    return result
