"""Перевірка на Windows без відеокарти й карт: чи все запускається, стрімить і чемно зупиняється.

Запускає GitHub Actions. Підсумок друкує рядками ::notice:: — їх видно в результатах перевірки.
"""

import os
import subprocess
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="eazy-smoke-")
os.environ["APPDATA"] = os.path.join(TMP, "appdata")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "localappdata")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from eazy_streamer import app as A, ffmpeg_cmd as F, log as L, osdep, updater  # noqa: E402

FFMPEG = osdep.ffmpeg_path()
PORT = 19555
URL = "rtmp://127.0.0.1:%d/live/key" % PORT
OUT = os.path.join(TMP, "got.flv")
problems = []


def note(title, text):
    text = str(text).replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print("::notice title=%s::%s" % (title, text), flush=True)


def run(args, timeout=30):
    r = subprocess.run(args, capture_output=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


L.setup()
L.system_summary()

code, text = run([FFMPEG, "-hide_banner", "-version"])
code2, encoders = run([FFMPEG, "-hide_banner", "-encoders"])
note("ffmpeg", "%s\nPython %s\nкодери: %s" % (
    text.splitlines()[0] if text else "?", sys.version.split()[0],
    ", ".join(e for e in ("libx264", "h264_nvenc", "h264_qsv", "h264_amf") if e in encoders)))
if "libx264" not in encoders:
    problems.append("у цьому ffmpeg немає libx264")

code, text = run([FFMPEG, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
from eazy_streamer.devices_win import parse_devices  # noqa: E402
note("dshow", "розібрано: %s\nсирий вивід:\n%s" % (parse_devices(text), text[-900:]))

listener = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-listen", "1", "-i", URL,
                             "-c", "copy", "-f", "flv", "-y", OUT], stderr=subprocess.PIPE)
time.sleep(2)

qa = QApplication([])
qa.setStyleSheet(A.STYLE)
A._ask = lambda *a: True
window = A.Window()
window.resize(1000, 760)
window.show()
card = window.cards[0]
seen = {}


def go_live():
    seen["encoder"] = "%s / %s" % (F.HW_KIND, window.encoder_note.text())
    seen["devices"] = [d.name for d in window._devices]
    card.source_combo.setCurrentIndex(card.source_combo.findData("test"))
    card._on_source_picked(card.source_combo.currentIndex())
    card.url_edit.setText(URL)
    card._on_go()


def check_live():
    seen["live_state"] = card.engine.state
    seen["live_status"] = card.status.text()
    seen["stats"] = dict(card.engine.stats)
    seen["stop_at"] = time.monotonic()
    card._on_go()                       # зупинити


def check_stopped():
    seen["after_stop_state"] = card.engine.state
    window.awake_box.setChecked(False)
    window.awake_box.setChecked(True)
    window.updater.up_to_date.connect(lambda: seen.setdefault("update", "оновлень немає"))
    window.updater.available.connect(lambda v: seen.setdefault("update", "є версія " + v))
    window.updater.failed.connect(lambda t: seen.setdefault("update", "збій: " + t))
    window.updater.busy = False
    window.updater.check()


def finish():
    window.close()
    qa.quit()


QTimer.singleShot(9000, go_live)        # до цього часу проба кодерів уже завершилась
QTimer.singleShot(24000, check_live)
QTimer.singleShot(33000, check_stopped)
QTimer.singleShot(40000, finish)
qa.exec()

time.sleep(1)
if listener.poll() is None:
    listener.terminate()
listener_err = listener.stderr.read().decode("utf-8", "replace")[-300:]

log_text = open(L.log_path(), encoding="utf-8", errors="replace").read()
killed = "вбиваю" in log_text
code, probe = run([FFMPEG.replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error", "-show_entries",
                   "stream=codec_name,width,height,avg_frame_rate", "-show_entries", "format=duration,bit_rate",
                   "-of", "compact", OUT])

note("Ефір", "кодер: %s\nджерела: %s\nстан в ефірі: %s | %s\nзвіт ffmpeg: %s\nпісля зупинки: %s\nffmpeg довелось вбивати: %s\nотриманий файл: %s\nприймач: %s" % (
    seen.get("encoder"), seen.get("devices"), seen.get("live_state"), seen.get("live_status"),
    {k: seen.get("stats", {}).get(k) for k in ("frame", "fps", "bitrate", "speed")},
    seen.get("after_stop_state"), killed, probe.strip() or "порожньо", listener_err or "без помилок"))
note("Оновлення і журнал", "перевірка оновлень: %s (спосіб: %s)\nжурнал: %s, %d рядків\nпроби кодерів:\n%s" % (
    seen.get("update"), "git" if updater.uses_git() else "завантаження", L.log_path(), log_text.count("\n"),
    "\n".join(l[32:200] for l in log_text.splitlines() if "Проба" in l)))
bad = [l[24:260] for l in log_text.splitlines() if " ERROR " in l or " CRITICAL " in l or "Traceback" in l]
if bad:
    note("Помилки в журналі", "\n".join(bad[:12]))

if seen.get("live_state") != "live":
    problems.append("стрім не вийшов в ефір: %s" % seen.get("live_status"))
if "h264" not in probe:
    problems.append("приймач не отримав відео")
if killed:
    problems.append("ffmpeg не зупинився чемно — його вбивали")
if seen.get("after_stop_state") not in ("preview", "starting"):
    problems.append("після зупинки стан %s" % seen.get("after_stop_state"))
if bad:
    problems.append("у журналі є помилки")

for p in problems:
    print("::error title=Проблема::%s" % p, flush=True)
print("ПРОБЛЕМ: %d" % len(problems))
sys.exit(1 if problems else 0)
