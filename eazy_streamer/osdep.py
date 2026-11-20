"""Усе, що залежить від операційної системи і не стосується відео: шляхи, запуск, зупинка."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# На Windows кожна консольна програма, запущена з віконної, блимає чорним вікном — забороняємо
NO_WINDOW = {"creationflags": 0x08000000} if IS_WIN else {}


def home_dir() -> str | None:
    """Тека, куди на Windows кладемо журнал і налаштування: поруч із програмою.

    Так їх легко знайти і забрати разом із текою. Якщо в теку програми писати не можна
    (наприклад, її поклали в Program Files) — повертаємо None, і все їде в профіль користувача.
    Linux сюди не заходить: там журнал і налаштування лежать за правилами системи.
    """
    override = os.environ.get("EAZY_HOME")      # для перевірок: щоб не смітити в теку збірки
    if override:
        return override
    if not IS_WIN:
        return None
    probe = os.path.join(ROOT, ".write-test")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
    except OSError:
        return None
    return ROOT


def _filetime(ft) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def system_cpu_ticks() -> tuple[int, int] | None:
    """Windows: (зайнято, усього) процесорного часу всіх ядер від старту системи."""
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes

    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    total = _filetime(kernel) + _filetime(user)        # час ядра вже містить простій
    return total - _filetime(idle), total


def process_cpu_seconds(pid: int) -> float | None:
    """Windows: скільки процесорного часу з'їв процес від свого старту."""
    if not IS_WIN or not pid:
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, int(pid))      # лише право спитати про час
    if not handle:
        return None
    made, gone, kernel, user = (wintypes.FILETIME() for _ in range(4))
    ok = kernel32.GetProcessTimes(wintypes.HANDLE(handle), ctypes.byref(made), ctypes.byref(gone),
                                  ctypes.byref(kernel), ctypes.byref(user))
    kernel32.CloseHandle(wintypes.HANDLE(handle))
    return (_filetime(kernel) + _filetime(user)) / 1e7 if ok else None


def memory_free_mb() -> int | None:
    if not IS_WIN:
        return None
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in ("total", "free", "page_total", "page_free",
                                                    "virtual_total", "virtual_free", "extended")]

    status = Status()
    status.length = ctypes.sizeof(Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.free // (1024 * 1024))


def ffmpeg_path() -> str:
    """Спершу ffmpeg, який лаунчер поклав поруч із програмою, далі системний."""
    override = os.environ.get("EAZY_FFMPEG")
    if override:
        return override
    local = os.path.join(ROOT, "runtime", "ffmpeg", "bin", "ffmpeg.exe" if IS_WIN else "ffmpeg")
    if os.path.exists(local):
        return local
    return shutil.which("ffmpeg") or "ffmpeg"


def has_ffmpeg() -> bool:
    path = ffmpeg_path()
    return os.path.exists(path) or shutil.which(path) is not None


def ask_to_quit(proc) -> None:
    """Попросити ffmpeg завершитись чемно, щоб він закрив стрім на сервері.

    Linux: сигнал SIGTERM. Windows сигналів не має — там ffmpeg чекає літеру q у вводі.
    """
    if IS_WIN:
        proc.write(b"q")
    else:
        proc.terminate()


def restart() -> None:
    """Запустити програму наново (після оновлення)."""
    os.chdir(ROOT)
    command = [sys.executable, "-m", "eazy_streamer"]
    if os.path.basename(sys.executable).lower() == "eazystreamer.exe":
        command = [sys.executable]      # цей exe відкриває програму сам, без параметрів
    if IS_WIN:
        subprocess.Popen(command, cwd=ROOT, close_fds=True, **NO_WINDOW)
        os._exit(0)
    os.execv(sys.executable, command)
