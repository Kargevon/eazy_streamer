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
