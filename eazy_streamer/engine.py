"""Один стрім = один процес ffmpeg. Впав чи завис — піднімаємо знову."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtGui import QImage

from . import ffmpeg_cmd
from .devices import Device
from .log import log, mask

_PROGRESS_KEYS = ("frame", "fps", "bitrate", "total_size", "out_time", "dup_frames",
                  "drop_frames", "speed", "progress", "stream_")
_FFMPEG_LINES_PER_WINDOW = 30   # більше за 10 с у журнал не пишемо — лише лічимо

IDLE, STARTING, PREVIEW, LIVE, RETRY = "idle", "starting", "preview", "live", "retry"

_STALL_SECONDS = 12         # стільки без жодного кадру — вважаємо, що завис
_STOP_GRACE_MS = 6000       # стільки чекаємо чемного виходу (закриття RTMPS — кілька секунд), далі вбиваємо
_RETRY_STEPS = (2, 4, 8, 15)
_HEALTHY_SECONDS = 30       # пропрацював стільки — лічильник спроб з нуля


class Engine(QObject):
    frame = Signal(QImage)
    changed = Signal(str, str)      # стан, пояснення

    def __init__(self, parent=None):
        super().__init__(parent)
        self._want = None           # (device, url, quality) або None
        self._running = None        # те саме для живого процесу
        self._proc: QProcess | None = None
        self._buf = bytearray()
        self._err = bytearray()
        self._stopping = False
        self._got_frame = False
        self._started_at = 0.0
        self._last_frame_at = 0.0
        self._fails = 0
        self._retry_left = 0
        self._reason = ""
        self._cpu_prev = None
        self.label = "?"            # назва секції — для журналу
        self.stats: dict[str, str] = {}
        self.net: dict[str, float] = {}     # заповнює netwatch: rtt (мс), loss (%)
        self.src_fps: float | None = None   # справжня частота кадрів джерела
        self._rate_prev = None
        self._reports = 0
        self._err_line = bytearray()
        self._pending: dict[str, str] = {}
        self._quiet_since = 0.0
        self._quiet_count = 0
        self.state = IDLE
        self.live_since = 0.0

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

    # --- що хоче вікно -------------------------------------------------

    def show(self, device: Device | None, url: str | None, quality: ffmpeg_cmd.Quality):
        """device=None — нічого не показувати; url=None — лише перегляд."""
        want = None if device is None else (device, url or None, quality)
        if want == self._want and self.state != RETRY:
            return
        self._want = want
        self._fails = 0
        self._retry_left = 0
        self._apply()

    def pid(self) -> int:
        """PID ffmpeg, що зараз в ефірі; 0 — ефіру немає."""
        if self._proc is None or not (self._running and self._running[1]):
            return 0
        return int(self._proc.processId())

    def is_live_wanted(self) -> bool:
        return bool(self._want and self._want[1])

    def request_stop(self):
        """Закриття: попросити ffmpeg вийти, не чекаючи."""
        self._want = None
        self._tick.stop()
        if self._proc is not None and not self._stopping:
            self._stopping = True
            self._proc.terminate()

    def wait_stopped(self):
        proc = self._proc
        if proc is None:
            return
        if not proc.waitForFinished(_STOP_GRACE_MS):
            log.warning("[%s] ffmpeg не вийшов сам при закритті — вбиваю", self.label)
            proc.kill()
            proc.waitForFinished(1000)

    # --- процес --------------------------------------------------------

    def _apply(self):
        if self._proc is not None:
            if self._running != self._want:
                self._stop()
            return
        if self._want is None:
            self._set(IDLE, "")
            return
        self._start()

    def _start(self):
        device, url, quality = self._want
        try:
            args = ffmpeg_cmd.build(device, url, quality)
        except NotImplementedError as e:
            self._set(IDLE, str(e))
            return
        self._running = self._want
        self._buf.clear()
        self._err.clear()
        self._err_line.clear()
        self._pending = {}
        self.stats = {}
        self.net = {}
        self.src_fps = None
        self._rate_prev = None
        self._reports = 0
        self._cpu_prev = None
        self._stopping = False
        self._got_frame = False
        self._started_at = self._last_frame_at = time.monotonic()

        proc = QProcess(self)
        if url and ffmpeg_cmd.HW_NODE and ffmpeg_cmd.HW_DRIVER:
            env = QProcessEnvironment.systemEnvironment()
            env.insert("LIBVA_DRIVER_NAME", ffmpeg_cmd.HW_DRIVER)
            proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        self._set(STARTING, "Підключення…" if url else "")
        size = ffmpeg_cmd.source_size(device, quality.height if url else 480)
        log.info("[%s] старт %s: джерело «%s» %s, якість %s, бітрейт %s, кодер %s", self.label,
                 "ЕФІРУ" if url else "перегляду", device.name,
                 "%dx%d" % size if size else "розмір як дасть карта", quality.label if url else "-",
                 "%dk" % ffmpeg_cmd.stream_kbit(device, quality) if url else "-",
                 (ffmpeg_cmd.HW_NODE or "CPU") if url else "-")
        log.debug("[%s] ffmpeg %s", self.label, mask(" ".join(args)))
        proc.start("ffmpeg", args)

    def _stop(self):
        proc = self._proc
        if proc is None or self._stopping:
            return
        self._stopping = True
        log.info("[%s] зупиняю ffmpeg (pid %s)", self.label, proc.processId())
        proc.terminate()
        QTimer.singleShot(_STOP_GRACE_MS, lambda: self._kill_if_same(proc))

    def _kill_if_same(self, proc: QProcess):
        if self._proc is proc and proc.state() != QProcess.ProcessState.NotRunning:
            log.warning("[%s] ffmpeg не вийшов сам за %d мс — вбиваю", self.label, _STOP_GRACE_MS)
            proc.kill()

    def _on_stdout(self):
        proc = self._proc
        if proc is None or self.sender() is not proc:
            return
        self._buf += proc.readAllStandardOutput().data()
        size = ffmpeg_cmd.PREVIEW_FRAME_BYTES
        count = len(self._buf) // size
        if count == 0:
            return
        # Показуємо лише найсвіжіший кадр — старі нікому не потрібні
        last = bytes(self._buf[(count - 1) * size:count * size])
        del self._buf[:count * size]
        self._last_frame_at = time.monotonic()
        if not self._got_frame:
            self._got_frame = True
            log.info("[%s] перший кадр через %.1f с після старту", self.label,
                     time.monotonic() - self._started_at)
            if self._running and self._running[1]:
                self.live_since = time.monotonic()
                self._set(LIVE, "")
            else:
                self._set(PREVIEW, "")
        image = QImage(last, ffmpeg_cmd.PREVIEW_W, ffmpeg_cmd.PREVIEW_H,
                       ffmpeg_cmd.PREVIEW_W * 3, QImage.Format.Format_RGB888)
        self.frame.emit(image.copy())

    def _on_stderr(self):
        proc = self._proc
        if proc is None or self.sender() is not proc:
            return
        self._err_line += proc.readAllStandardError().data()
        *lines, rest = bytes(self._err_line).split(b"\n")
        self._err_line = bytearray(rest)
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._on_ffmpeg_line(line)

    def _on_report(self):
        """Звіт ffmpeg раз на 2 с: рахуємо частоту кадрів джерела і пишемо в журнал."""
        s = self.stats
        try:
            cur = (time.monotonic(), int(s["frame"]), int(s["dup_frames"]), int(s["drop_frames"]))
        except (KeyError, ValueError):
            return
        prev, self._rate_prev = self._rate_prev, cur
        out_fps = None
        if prev and cur[0] > prev[0]:
            dt = cur[0] - prev[0]
            out_fps = (cur[1] - prev[1]) / dt
            # вихід = джерело + подвоєні − викинуті, звідси джерело
            self.src_fps = max(0.0, ((cur[1] - prev[1]) - (cur[2] - prev[2]) + (cur[3] - prev[3])) / dt)
        self._reports += 1
        speed = s.get("speed", "").rstrip("x")
        slow = speed.replace(".", "", 1).isdigit() and float(speed) < 0.95
        lossy = self.net.get("loss", 0) > 1
        cpu = self._cpu_percent()
        # Щоразу писати — це 25 МБ на добу. Пишемо кожен п'ятий звіт і кожен поганий
        if self._reports % 5 != 1 and not slow and not lossy:
            return
        log.log(30 if slow or lossy else 20,
                "[%s] хід: кадрів %s, в ефір %s к/с, джерело %s к/с, %s, викинуто %s, подвоєно %s, "
                "швидкість %s, CPU %s, мережа: затримка %s мс, повторів %s%%, усього повторно %s байт",
                self.label, s.get("frame", "?"), "%.1f" % out_fps if out_fps is not None else "?",
                "%.1f" % self.src_fps if self.src_fps is not None else "?", s.get("bitrate", "?"),
                s.get("drop_frames", "?"), s.get("dup_frames", "?"), s.get("speed", "?"), cpu,
                "%.0f" % self.net["rtt"] if "rtt" in self.net else "?",
                "%.2f" % self.net["loss"] if "loss" in self.net else "?",
                "%d" % self.net["retrans_total"] if "retrans_total" in self.net else "?")

    def _cpu_percent(self) -> str:
        """Скільки одного ядра з'їв цей ffmpeg від минулого звіту (100% = ядро повністю)."""
        try:
            with open("/proc/%d/stat" % self._proc.processId()) as f:
                fields = f.read().rsplit(")", 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])       # utime + stime
        except (OSError, ValueError, IndexError, AttributeError):
            return "?"
        now, prev = time.monotonic(), self._cpu_prev
        self._cpu_prev = (now, ticks)
        if not prev or now <= prev[0]:
            return "?"
        return "%.0f%%" % ((ticks - prev[1]) / os.sysconf("SC_CLK_TCK") / (now - prev[0]) * 100)

    def _on_ffmpeg_line(self, line: str):
        key, sep, value = line.partition("=")
        if sep and key.startswith(_PROGRESS_KEYS) and " " not in key:
            self._pending[key] = value.strip()
            if key == "progress":
                self.stats, self._pending = self._pending, {}
                self._on_report()
            return
        self._err += line.encode("utf-8") + b"\n"
        del self._err[:-4000]
        # ffmpeg уміє сипати одним попередженням на кожен кадр — такий потік лише лічимо
        now = time.monotonic()
        if now - self._quiet_since > 10:
            if self._quiet_count > _FFMPEG_LINES_PER_WINDOW:
                log.warning("[%s] ffmpeg: …ще %d рядків за 10 с не записано", self.label,
                            self._quiet_count - _FFMPEG_LINES_PER_WINDOW)
            self._quiet_since, self._quiet_count = now, 0
        self._quiet_count += 1
        if self._quiet_count <= _FFMPEG_LINES_PER_WINDOW:
            log.warning("[%s] ffmpeg: %s", self.label, mask(line))

    def _on_error(self, error):
        # Сюди потрапляємо, коли ffmpeg взагалі не запустився: finished не прийде
        if error != QProcess.ProcessError.FailedToStart or self.sender() is not self._proc:
            return
        self._proc.deleteLater()
        self._proc = None
        self._running = None
        self._want = None
        log.error("[%s] ffmpeg не запустився взагалі", self.label)
        self._set(IDLE, "Не знайдено ffmpeg. Встановіть: sudo apt install ffmpeg")

    def _on_finished(self, *_):
        proc = self.sender()
        if proc is not self._proc:
            return
        url = self._running[1] if self._running else None
        reason = ffmpeg_cmd.explain(bytes(self._err).decode("utf-8", "replace"), url)
        ran = time.monotonic() - self._started_at
        intended = self._stopping
        log.log(20 if intended else 40,
                "[%s] ffmpeg завершився: код %s, %s, пропрацював %.1f с, %s%s", self.label,
                proc.exitCode(), "сам" if proc.exitStatus() == QProcess.ExitStatus.NormalExit else "вбитий/впав",
                ran, "зупинка з нашого боку" if intended else "НЕСПОДІВАНО",
                "" if intended else " — причина: " + reason)
        if (not intended and url and ffmpeg_cmd.HW_NODE and not self._got_frame
                and "vaapi" in bytes(self._err).decode("utf-8", "replace").lower()):
            # Проба пройшла, а справжній потік відеокарта не потягнула — далі процесором
            ffmpeg_cmd.HW_NODE = None
            reason = "GPU не впорався, кодую на CPU"
            log.error("[%s] GPU відмовив на справжньому потоці — усі стріми далі кодує CPU", self.label)
        proc.deleteLater()
        self._proc = None
        self._running = None
        self._stopping = False

        if intended:
            # Самі зупинили (зміна режиму або стоп). Карті треба мить, щоб звільнитись
            if self._want is None:
                self._set(IDLE, "")
            else:
                self._set(STARTING, "Підключення…" if self._want[1] else "")
                QTimer.singleShot(300, self._start_if_free)
            return
        if self._want is None:
            self._set(IDLE, "")
            return
        if ran > _HEALTHY_SECONDS:
            self._fails = 0
        self._retry_left = _RETRY_STEPS[min(self._fails, len(_RETRY_STEPS) - 1)]
        self._fails += 1
        self._reason = reason
        self._set(RETRY, self.retry_text())

    def _start_if_free(self):
        if self._proc is None and self._want is not None and self._retry_left == 0:
            self._start()

    def retry_text(self) -> str:
        return "%s. Нова спроба через %d с" % (self._reason[:1].upper() + self._reason[1:],
                                               self._retry_left)

    def _on_tick(self):
        if self.state == RETRY and self._proc is None:
            self._retry_left -= 1
            if self._retry_left > 0:
                self._set(RETRY, self.retry_text())
                return
            self._retry_left = 0
            if self._want is None:
                self._set(IDLE, "")
            else:
                self._start()
            return
        if self._proc is not None and not self._stopping:
            if time.monotonic() - self._last_frame_at > _STALL_SECONDS:
                # Процес живий, а кадрів нема: мережа або карта зависла
                self._err += b"\nstalled"
                log.error("[%s] %d с без жодного кадру — вбиваю ffmpeg", self.label, _STALL_SECONDS)
                self._proc.kill()
        if self.state == LIVE:
            self.changed.emit(LIVE, "")     # щоб вікно оновило лічильник часу

    def _set(self, state: str, text: str):
        if state != self.state:
            log.info("[%s] стан: %s -> %s %s", self.label, self.state, state, text)
        self.state = state
        self.changed.emit(state, text)
