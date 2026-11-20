"""Один стрім = один процес ffmpeg. Впав чи завис — піднімаємо знову."""

from __future__ import annotations

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
_STOP_GRACE_MS = 2500       # стільки чекаємо чемного виходу, далі вбиваємо
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
        self.label = "?"            # назва секції — для журналу
        self.stats: dict[str, str] = {}
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

    def is_live_wanted(self) -> bool:
        return bool(self._want and self._want[1])

    def shutdown(self):
        """Закриття програми: зупинити і дочекатися."""
        self._want = None
        self._tick.stop()
        proc = self._proc
        if proc is None:
            return
        self._stopping = True
        proc.terminate()
        if not proc.waitForFinished(_STOP_GRACE_MS):
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
        log.info("[%s] старт %s: джерело «%s», якість %s, кодер %s", self.label,
                 "ЕФІРУ" if url else "перегляду", device.name, quality.label if url else "-",
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

    def _on_ffmpeg_line(self, line: str):
        key, sep, value = line.partition("=")
        if sep and key.startswith(_PROGRESS_KEYS) and " " not in key:
            self._pending[key] = value.strip()
            if key == "progress":
                self.stats, self._pending = self._pending, {}
                log.info("[%s] хід: кадрів %s, %s к/с, %s, пропущено %s, подвоєно %s, швидкість %s",
                         self.label, self.stats.get("frame", "?"), self.stats.get("fps", "?"),
                         self.stats.get("bitrate", "?"), self.stats.get("drop_frames", "?"),
                         self.stats.get("dup_frames", "?"), self.stats.get("speed", "?"))
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
