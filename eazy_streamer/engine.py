"""Один стрім = один процес ffmpeg. Впав чи завис — піднімаємо знову."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtGui import QImage

from . import ffmpeg_cmd, osdep
from .devices import Device
from .log import log, mask

_PROGRESS_KEYS = ("frame", "fps", "bitrate", "total_size", "out_time", "dup_frames",
                  "drop_frames", "speed", "progress", "stream_")
_FFMPEG_LINES_PER_WINDOW = 30   # більше за 10 с у журнал не пишемо — лише лічимо

IDLE, STARTING, PREVIEW, LIVE, RETRY = "idle", "starting", "preview", "live", "retry"

_STALL_SECONDS = 12         # стільки без жодного кадру — вважаємо, що завис
_EMPTY_START_SECONDS = 6    # карта відкрилась, але шле порожні кадри: перезапускаємо швидше
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
        self._level = 0
        self._cpu_prev = None
        self.label = "?"            # назва секції — для журналу
        self.stats: dict[str, str] = {}
        self.net: dict[str, float] = {}     # заповнює netwatch: rtt (мс), loss (%)
        self.src_fps: float | None = None   # справжня частота кадрів джерела
        self._fps_window: list[float] = []  # к/с у кожному 2-секундному звіті від минулого запису
        self._preview_frames = 0            # кадрів перегляду від минулого запису в журнал
        self._logged_at = 0                 # на якому звіті востаннє писали в журнал
        self._logged_kbit: float | None = None
        self._size_prev = 0
        self._size_last = 0
        self._recent: list[tuple[float, int]] = []     # (секунд, байт) за останні звіти
        self.kbit_now: float | None = None  # бітрейт зараз, а не середній від старту
        self.in_fps: float | None = None    # скільки кадрів за секунду дає джерело
        self.out_fps: float | None = None   # скільки йде в ефір
        self.rate_mode = "cbr"              # як керується бітрейт у цьому ефірі
        self.pace: float | None = None      # темп: 1.0 — ефір іде в ногу з часом, 0.5 — удвічі повільніше
        self.cpu_pct: float | None = None   # скільки одного ядра їсть ffmpeg
        self._pace_prev = None
        self._paces: list[float] = []
        self.note = ""                      # що відбувається, поки картинки ще немає
        self._empty_frames = False
        self._step = 1
        self.stalled = False                # ефір іде, а нових кадрів немає
        self._bad_streak = 0                # скільки поганих звітів поспіль
        self._gap_max = 0.0                 # найдовша пауза між кадрами перегляду від минулого запису
        self._even = None                   # рівна частота кадрів в ефір (Windows); None — як дає джерело
        self._rate_samples = []
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
            osdep.ask_to_quit(self._proc)

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
        self._level = ffmpeg_cmd.level_for(device) if url else 0
        try:
            args = ffmpeg_cmd.build(device, url, quality, self._level)
        except NotImplementedError as e:
            self._set(IDLE, str(e))
            return
        self._running = self._want
        self._buf.clear()
        self._err.clear()
        self._err_line.clear()
        self._pending = {}
        self.stats = {}
        self._logged_at, self._logged_kbit = 0, None
        self._preview_frames = 0
        self._size_prev = 0
        self._size_last = 0
        self._recent = []
        self.kbit_now = self.in_fps = self.out_fps = None
        self.pace = self.cpu_pct = self._pace_prev = None
        self._paces = []
        self.note = ""
        self._empty_frames = False
        self.stalled = False
        self._bad_streak = 0
        self.net = {}
        self.src_fps = None
        self._rate_prev = None
        self._reports = 0
        self._cpu_prev = None
        self._stopping = False
        self._got_frame = False
        self._started_at = self._last_frame_at = time.monotonic()

        proc = QProcess(self)
        if self._level and ffmpeg_cmd.HW_DRIVER and ffmpeg_cmd.HW_KIND == "vaapi":
            env = QProcessEnvironment.systemEnvironment()
            env.insert("LIBVA_DRIVER_NAME", ffmpeg_cmd.HW_DRIVER)
            proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        self._set(STARTING, "Підключення…" if url else "")
        fourcc, size, rate = ffmpeg_cmd.capture(device, quality.height if url else 480, preview=not url)
        self._step = ffmpeg_cmd.thin_step(rate) if url else 1
        self._even = ffmpeg_cmd.steady_rate(device, rate) if url else None
        self._rate_samples = []
        self.rate_mode = ffmpeg_cmd.rate_mode(self._level)
        out = ffmpeg_cmd.out_size(device, quality) if url else None
        log.info("[%s] старт %s: вхід «%s» %s %s %s к/с; вихід %s, стеля %s, бітрейт керується: %s, обробка: %s",
                 self.label, "ЕФІРУ" if url else "перегляду", device.name, fourcc or "-",
                 "%dx%d" % size if size else "розмір як дасть карта", "%g" % rate if rate else "?",
                 "%dx%d" % out if out else "-",
                 ("%d кбіт/с" % quality.kbit if quality.kbit else "немає") if url else "-",
                 self.rate_mode if url else "-",
                 ffmpeg_cmd.LEVEL_NAMES[self._level] if url else "-")
        log.debug("[%s] ffmpeg %s", self.label, mask(" ".join(args)))
        proc.start(osdep.ffmpeg_path(), args)

    def _stop(self):
        proc = self._proc
        if proc is None or self._stopping:
            return
        self._stopping = True
        log.info("[%s] зупиняю ffmpeg (pid %s)", self.label, proc.processId())
        if not self._got_frame:
            # Ще жодного кадру: ffmpeg чекає на карту й на прохання не відповість, а закривати
            # на сервері йому ще нічого. Не тримаємо людину 6 секунд — зупиняємо одразу.
            proc.kill()
            return
        osdep.ask_to_quit(proc)
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
        now = time.monotonic()
        # Пауза між кадрами перегляду = стільки мовчав вхід (карта, USB, розтискання).
        # Мережі й сервера тут немає, тож це відділяє «не дала карта» від «не пройшло далі».
        # Застереження: якщо зависло саме вікно програми, пауза вийде фальшива.
        if self._got_frame:
            gap = now - self._last_frame_at
            self._gap_max = max(self._gap_max, gap)
            if gap >= 1.0 and self.state == LIVE:
                log.warning("[%s] вхід мовчав %.1f с — на платформі це стоп-кадр і стрибок", self.label, gap)
        self._last_frame_at = now
        self._preview_frames += count
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
            # в ефір іде рівно стільки кадрів, скільки дало джерело (якщо не проріджуємо 60 -> 30)
            self.src_fps = max(0.0, ((cur[1] - prev[1]) - (cur[2] - prev[2]) + (cur[3] - prev[3])) / dt)
            self.out_fps = out_fps
            self.in_fps = self.src_fps * self._step
            self._learn_rate()
            # Темп — на скільки секунд відео просунувся ефір за секунду справжнього часу. «Швидкість»
            # самого ffmpeg рахується від старту й ховає те, що відбувається зараз; ця — за 6 с.
            try:
                media = int(s["out_time_us"]) / 1e6
            except (KeyError, ValueError):
                media = None
            if media is not None and self._pace_prev is not None and media >= self._pace_prev:
                self._paces = (self._paces + [(media - self._pace_prev) / dt])[-3:]
                self.pace = sum(self._paces) / len(self._paces)
            self._pace_prev = media
            try:
                size_now = int(s.get("total_size", "0"))
            except ValueError:
                size_now = self._size_last
            # бітрейт «зараз»: за три останні звіти (6 с), бо один звіт стрибає від ключових кадрів
            self._recent = (self._recent + [(dt, max(0, size_now - self._size_last))])[-3:]
            self._size_last = size_now
            self.kbit_now = sum(b for _d, b in self._recent) * 8 / 1000 / sum(d for d, _b in self._recent)
        self._reports += 1
        if out_fps is not None:
            self._fps_window.append(out_fps)
        speed = s.get("speed", "").rstrip("x")
        # «Швидкість» у ffmpeg рахується від самого старту, тому першу хвилину вона нижча за 1
        # просто через час на підключення. Справжня біда — коли кадри перестали йти зовсім.
        self.stalled = out_fps is not None and out_fps < 1
        slow = self.stalled or (len(self._paces) == 3 and self.pace < 0.9)
        lossy = self.net.get("loss", 0) > 1
        cpu = self._cpu_percent()
        self.cpu_pct = float(cpu[:-1]) if cpu.endswith("%") else None
        # Щоразу писати — це 25 МБ на добу. Першу хвилину ефіру пишемо раз на 10 с,
        # далі раз на хвилину; поганий звіт (ПК чи мережа не встигає) — завжди
        every = 5 if self._reports <= 30 else 30
        # ...і коли картинка різко змінилась: бітрейт стрибнув чи впав удвічі проти останнього запису.
        # Саме ці миті цікаві (з'явився сигнал, пішов сніг), а раз на хвилину їх не впіймати.
        # Не частіше ніж раз на 10 с, щоб довгий сніг не засипав журнал.
        jump = False
        if self.kbit_now is not None and self._reports - self._logged_at >= 5:
            was = self._logged_kbit
            jump = was is not None and (self.kbit_now > was * 2 + 300 or self.kbit_now * 2 + 300 < was)
        # Поганий звіт пишемо одразу, а далі раз на 10 с, поки погано: інакше доба поганого
        # ефіру з'їдає весь журнал. Перший добрий після поганих теж пишемо — видно, скільки тривало.
        bad = slow or lossy
        streak, self._bad_streak = self._bad_streak, (self._bad_streak + 1 if bad else 0)
        worth = (bad and streak % 5 == 0) or (not bad and streak > 0)
        if self._reports % every != 1 and not worth and not jump:
            return
        self._logged_at, self._logged_kbit = self._reports, self.kbit_now
        window, self._fps_window = self._fps_window, []
        try:
            size = int(s.get("total_size", "0"))
        except ValueError:
            size = self._size_prev
        sent, self._size_prev = size - self._size_prev, size
        preview, self._preview_frames = self._preview_frames, 0
        gap_max, self._gap_max = self._gap_max, 0.0
        spread = "?"
        if window:
            spread = "по 2 с: мін %.1f, макс %.1f" % (min(window), max(window))
            if len(window) <= 5 or max(window) - min(window) > 1.5:
                spread += ", усі: " + " ".join("%.1f" % f for f in window)
        log.log(30 if slow or lossy else 20,
                "[%s] хід: кадрів %s, в ефір %s к/с (%s), %s, викинуто %s, подвоєно %s, "
                "швидкість %s, CPU %s, мережа: затримка %s мс, повторів %s%%, усього повторно %s байт; "
                "від минулого запису: на сервер пішло %d КБ, у перегляд %d кадрів, найдовша пауза входу %.1f с; зараз %s кбіт/с, вхід %s к/с, "
                "темп %s; мережа за ядром: дійшло %s кбіт/с, у черзі %s КБ, оцінка каналу %s кбіт/с, %s%s",
                self.label, s.get("frame", "?"), "%.1f" % out_fps if out_fps is not None else "?",
                spread, s.get("bitrate", "?"),
                s.get("drop_frames", "?"), s.get("dup_frames", "?"), s.get("speed", "?"), cpu,
                "%.0f" % self.net["rtt"] if "rtt" in self.net else "?",
                "%.2f" % self.net["loss"] if "loss" in self.net else "?",
                "%d" % self.net["retrans_total"] if "retrans_total" in self.net else "?",
                sent // 1024, preview, gap_max, "%.0f" % self.kbit_now if self.kbit_now is not None else "?",
                "%.1f" % self.in_fps if self.in_fps is not None else "?",
                "%.2f" % self.pace if self.pace is not None else "?",
                "%.0f" % self.net["through_kbit"] if "through_kbit" in self.net else "?",
                "%d" % (self.net["queued"] // 1024) if "queued" in self.net else "?",
                "%.0f" % self.net["kernel_kbit"] if self.net.get("kernel_kbit") else "?",
                (("СЕРВЕР ПРИЙМАЄ ПОВІЛЬНО" if self.net.get("server_slow") else "МЕРЕЖА НЕ ВСТИГАЄ")
                 if self.net.get("congested") else
                 "шлемо менше, ніж канал може" if self.net.get("app_limited") else "без позначок") if self.net else "?",
                " — ЕФІР ЗАВМЕР" if self.stalled else "")

    def _learn_rate(self):
        """Хвилина рівного ефіру показує, скільки кадрів карта дає насправді. Якщо менше,
        ніж ми шлемо (карта казала 30, а дає 25), — запам'ятовуємо і перезапускаємо ефір уже
        з правильною частотою. Один раз на карту: нижче стандартних 24–25 не опускаємось."""
        if not self._even or self.state != LIVE or self._stopping:
            return
        self._rate_samples.append(self.src_fps)
        if len(self._rate_samples) < 30:
            return
        samples, self._rate_samples = sorted(self._rate_samples), []
        mid = samples[len(samples) // 2]
        rate = int(round(mid))
        steady = sum(abs(x - mid) <= 1.0 for x in samples) >= 0.85 * len(samples)
        if (rate not in ffmpeg_cmd.STANDARD_RATES or abs(mid - rate) > 0.5 or not steady
                or rate >= self._even - 0.5):
            return
        device = self._running[0] if self._running else None
        ffmpeg_cmd.learn_rate(device, rate)
        log.warning("[%s] карта дає %d к/с, а не %g — запам'ятав, перезапускаю ефір із рівними %d к/с",
                    self.label, rate, self._even, rate)
        self._stop()

    def _cpu_percent(self) -> str:
        """Скільки одного ядра з'їв цей ffmpeg від минулого звіту (100% = ядро повністю)."""
        try:
            if osdep.IS_WIN:
                used = osdep.process_cpu_seconds(self._proc.processId())
                if used is None:
                    return "?"
            else:
                with open("/proc/%d/stat" % self._proc.processId()) as f:
                    fields = f.read().rsplit(")", 1)[1].split()
                used = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")     # utime + stime
        except (OSError, ValueError, IndexError, AttributeError):
            return "?"
        now, prev = time.monotonic(), self._cpu_prev
        self._cpu_prev = (now, used)
        if not prev or now <= prev[0]:
            return "?"
        return "%.0f%%" % ((used - prev[1]) / (now - prev[0]) * 100)

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
        if not self._got_frame and "corrupted data (0 bytes)" in line and not self._empty_frames:
            # Дешеві HDMI-карти після відкриття якийсь час шлють порожні кадри
            self._empty_frames = True
            self.note = "Карта відкрилась, але картинки ще не дає"
            self.changed.emit(self.state, self.note)
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
        self._set(IDLE, "Не знайдено ffmpeg. Запустіть програму через eazy-streamer.bat" if osdep.IS_WIN
                  else "Не знайдено ffmpeg. Встановіть: sudo apt install ffmpeg")

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
        if (not intended and self._level and (not self._got_frame or ran < _HEALTHY_SECONDS)
                and ffmpeg_cmd.is_hw_error(bytes(self._err).decode("utf-8", "replace"))):
            # Проба пройшла, а це джерело GPU не потягнув — для нього спрощуємо на один крок
            device = self._running[0] if self._running else None
            new = ffmpeg_cmd.lower_level(device)
            reason = "GPU не впорався з цим джерелом, спрощую обробку"
            log.error("[%s] GPU відмовив (%s) — для цього джерела далі: %s", self.label,
                      ffmpeg_cmd.LEVEL_NAMES[self._level], ffmpeg_cmd.LEVEL_NAMES[new])
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
            limit = _EMPTY_START_SECONDS if (self._empty_frames and not self._got_frame) else _STALL_SECONDS
            if time.monotonic() - self._last_frame_at > limit:
                # Процес живий, а кадрів нема: мережа або карта зависла
                self._err += b"\nstalled"
                log.error("[%s] %d с без жодного кадру — вбиваю ffmpeg", self.label, limit)
                self._proc.kill()
        if self.state == LIVE:
            self.changed.emit(LIVE, "")     # щоб вікно оновило лічильник часу

    def _set(self, state: str, text: str):
        if state != self.state:
            log.info("[%s] стан: %s -> %s %s", self.label, self.state, state, text)
        self.state = state
        self.changed.emit(state, text)
