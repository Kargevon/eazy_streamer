"""Журнал роботи. Детальний, але без ключів стрімів: посилання маскуємо."""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys

log = logging.getLogger("eazy")

MAX_FILE_BYTES = 4_000_000
FILES = 5


def log_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "eazy_streamer")


def log_path() -> str:
    return os.path.join(log_dir(), "eazy_streamer.log")


def setup() -> None:
    try:
        os.makedirs(log_dir(), mode=0o700, exist_ok=True)
        # 5 файлів по 4 МБ по колу: більше 20 МБ журнал не займе ніколи, хоч би що сипав ffmpeg.
        # На п'яти стрімах цього вистачає приблизно на тиждень історії
        handler = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=MAX_FILE_BYTES, backupCount=FILES - 1, encoding="utf-8")
        os.chmod(log_path(), 0o600)
    except OSError:
        handler = logging.StreamHandler()       # нема куди писати — хоч у термінал
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    def on_crash(kind, value, trace):
        log.critical("Необроблена помилка", exc_info=(kind, value, trace))
        sys.__excepthook__(kind, value, trace)

    sys.excepthook = on_crash


def mask(text: str) -> str:
    """rtmps://host:8443/live/KEY?t=TOKEN -> rtmps://host:8443/live/***"""
    return re.sub(r"(rtmps?://[^/\s]+/(?:[^/\s?]+/)*)[^\s]*", r"\1***", text)


def system_summary() -> None:
    log.info("=" * 60)
    log.info("Запуск. Python %s, %s", platform.python_version(), platform.platform())
    for path, keys in (("/etc/os-release", ("PRETTY_NAME",)), ("/proc/meminfo", ("MemTotal", "MemAvailable"))):
        for line in _lines(path):
            if line.split("=")[0].split(":")[0] in keys:
                log.info("  %s", line.strip())
    models = {ln.split(":", 1)[1].strip() for ln in _lines("/proc/cpuinfo") if ln.startswith("model name")}
    log.info("  CPU: %s, ядер: %s", ", ".join(models) or "?", os.cpu_count())
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5).stdout
        log.info("  %s", out.splitlines()[0] if out else "ffmpeg не відповів")
    except (OSError, subprocess.SubprocessError) as e:
        log.error("  ffmpeg не запускається: %s", e)


_cpu_prev: tuple[int, int] | None = None


def _cpu_busy() -> str:
    """Завантаження всіх ядер разом від минулого виклику, у відсотках."""
    global _cpu_prev
    first = (_lines("/proc/stat") or [""])[0].split()
    if len(first) < 6 or first[0] != "cpu":
        return "?"
    nums = [int(x) for x in first[1:9]]
    total, idle = sum(nums), nums[3] + nums[4]      # idle + iowait
    prev, _cpu_prev = _cpu_prev, (total, idle)
    if not prev or total == prev[0]:
        return "?"
    return "%.0f%%" % (100 * (1 - (idle - prev[1]) / (total - prev[0])))


def load_line() -> str:
    """Навантаження машини одним рядком — пишемо періодично, поки йдуть стріми."""
    load = (_lines("/proc/loadavg") or ["?"])[0].split()[:3]
    free = next((ln.split()[1] for ln in _lines("/proc/meminfo") if ln.startswith("MemAvailable")), "0")
    temps = []
    for i in range(12):
        kind = (_lines("/sys/class/thermal/thermal_zone%d/type" % i) or [""])[0].strip()
        value = (_lines("/sys/class/thermal/thermal_zone%d/temp" % i) or [""])[0].strip()
        if kind in ("x86_pkg_temp", "cpu-thermal", "acpitz") and value.lstrip("-").isdigit():
            temps.append("%s=%d°C" % (kind, int(value) // 1000))
    return "CPU зайнятий на %s | load %s | вільної пам'яті %d МБ | %s" % (
        _cpu_busy(), " ".join(load), int(free) // 1024, " ".join(temps) or "t°=?")


def _lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError:
        return []
