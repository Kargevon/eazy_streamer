"""Пошук карт захоплення на Windows. Питаємо в самого ffmpeg (DirectShow)."""

from __future__ import annotations

import re

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from . import osdep
from .devices import Device
from .log import log

import time

# Кожне опитування — це запуск ffmpeg.exe, який антивірус щоразу перевіряє. Тому опитуємо
# не по колу, а коли Windows сама повідомила, що пристрої змінились; раз на хвилину — про всяк випадок
_PERIOD_MS = 60000
_SETTLE_MS = 1500
_TIMEOUT_MS = 15000

_FORMAT_KEYS = {("vcodec", "mjpeg"): "MJPG", ("pixel_format", "yuyv422"): "YUYV",
                ("pixel_format", "nv12"): "NV12"}
_OPTION = re.compile(r"(pixel_format|vcodec)=(\S+)\s+min s=(\d+)x(\d+) fps=([\d.]+) max s=(\d+)x(\d+) fps=([\d.]+)")


def parse_devices(text: str) -> list[tuple[str, str]]:
    """Вивід `ffmpeg -list_devices true -f dshow -i dummy` -> [(назва, стале ім'я)].

    Розуміє і новий формат («"Назва" (video)»), і старий (заголовок «DirectShow video devices»).
    """
    found: list[list[str]] = []
    section = ""
    last_is_video = False
    for line in text.splitlines():
        low = line.lower()
        if "directshow video devices" in low:
            section = "video"
            continue
        if "directshow audio devices" in low:
            section = "audio"
            continue
        alt = re.search(r'Alternative name\s+"(.+)"', line)
        if alt:
            if last_is_video and found:
                found[-1][1] = alt.group(1)
            continue
        name = re.search(r'\]\s+"(.+)"(?:\s+\((\w+)\))?\s*$', line)
        if not name:
            continue
        kind = name.group(2) or section
        last_is_video = kind == "video"
        if last_is_video:
            found.append([name.group(1), name.group(1)])
    return [(n, a) for n, a in found]


def parse_options(text: str) -> tuple[dict, dict]:
    """Вивід `ffmpeg -list_options true -f dshow -i video=…` -> (формати, частоти кадрів)."""
    formats: dict[str, list[tuple[int, int]]] = {}
    rates: dict[tuple[str, int, int], list[float]] = {}
    for kind, name, w1, h1, fps_min, w2, h2, fps_max in _OPTION.findall(text):
        key = _FORMAT_KEYS.get((kind, name)) or ("pix:" if kind == "pixel_format" else "codec:") + name
        low, high = sorted((float(fps_min), float(fps_max)))
        for size in {(int(w1), int(h1)), (int(w2), int(h2))}:
            if size not in formats.setdefault(key, []):
                formats[key].append(size)
            # карта приймає будь-яку частоту в цих межах — пропонуємо звичні значення
            options = {r for r in (10.0, 25.0, 30.0) if low <= r <= high} | {low, high}
            known = set(rates.get((key,) + size, []))
            rates[(key,) + size] = sorted(known | options, reverse=True)
    return formats, rates


class Watcher(QObject):
    """Раз на кілька секунд дивиться, які карти підключені. Нову карту питає про формати один раз."""

    found = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._known: dict[str, Device] = {}     # стале ім'я -> карта з уже зчитаними форматами
        self._absent: dict[str, int] = {}       # скільки опитувань поспіль карти не видно
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setInterval(_PERIOD_MS)
        self._timer.timeout.connect(self.poll)
        self._poke = QTimer(self)
        self._poke.setSingleShot(True)
        self._poke.setInterval(_SETTLE_MS)      # Windows шле кілька повідомлень на одне підключення
        self._poke.timeout.connect(self.poll)
        self._started = 0.0

    def start(self):
        self._timer.start()
        self.poll()

    def stop(self):
        self._timer.stop()

    def devices_changed(self):
        """Windows повідомила про зміну пристроїв (WM_DEVICECHANGE)."""
        self._poke.start()

    def poll(self):
        if self._busy:
            self._poke.start()          # зайняті — спитаємо ще раз трохи згодом
            return
        self._busy = True
        self._started = time.monotonic()
        self._run(["-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"], self._on_list)

    def _on_list(self, text: str):
        present = parse_devices(text)
        new = [(name, alt) for name, alt in present if alt not in self._known]
        self._query(present, new)

    def _query(self, present, new):
        if not new:
            seen = {alt for _name, alt in present}
            # Карта, яку саме відкривають, може на мить зникнути зі списку. Один пропуск прощаємо:
            # інакше секція скидається посеред запуску
            self._absent = {alt: self._absent.get(alt, 0) + 1 for alt in self._known if alt not in seen}
            gone = [alt for alt, misses in self._absent.items() if misses >= 2]
            for alt in gone:
                del self._known[alt]
                del self._absent[alt]
            self._busy = False
            took = time.monotonic() - self._started
            if took > 2:        # звичайні пів секунди раз на хвилину журналу не потрібні
                log.debug("Опитування карт тривало %.1f с", took)
            self.found.emit(list(self._known.values()))
            return
        name, alt = new.pop(0)

        def on_options(text: str):
            formats, rates = parse_options(text)
            if not formats:
                # Карту тримає інша програма або вона мовчить — показуємо, формат обере ffmpeg
                log.info("  %s: формати не зчитано: %s", name, " | ".join(text.strip().splitlines()[-2:])[:200])
            self._known[alt] = Device(key=alt, path="video=" + alt, name=name, formats=formats,
                                      rates=rates, bus="DirectShow")
            self._query(present, new)

        self._run(["-hide_banner", "-list_options", "true", "-f", "dshow", "-i", "video=" + alt], on_options)

    def _run(self, args: list[str], then):
        proc = QProcess(self)

        def finished(*_):
            # ffmpeg друкує список у stderr і виходить з помилкою — це нормально
            text = bytes(proc.readAllStandardError().data()).decode("utf-8", "replace")
            proc.deleteLater()
            then(text)

        def error(_e):
            if proc.error() == QProcess.ProcessError.FailedToStart:
                proc.deleteLater()
                self._busy = False

        proc.finished.connect(finished)
        proc.errorOccurred.connect(error)
        proc.start(osdep.ffmpeg_path(), args)
        # Таймер — дитина процесу: зникає разом із ним і не смикає вже видалений об'єкт
        guard = QTimer(proc)
        guard.setSingleShot(True)
        guard.timeout.connect(proc.kill)
        guard.start(_TIMEOUT_MS)
