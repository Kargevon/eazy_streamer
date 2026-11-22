"""Оновлення з GitHub.

Якщо програма стоїть як клон репозиторію і в системі є git — оновлюємось через git pull.
Інакше (типово на Windows: тека з архіву) — дивимось номер останнього релізу на GitHub
і розпаковуємо його код поверх програми. Реліз з'являється лише після того, як збірка
пройшла перевірку, тож невдалий push до таких машин не доїде.
"""

from __future__ import annotations

import io
import os
import shutil
import threading
import urllib.request
import zipfile

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .log import log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TIMEOUT_MS = 40000
_REPO = "Kargevon/eazy_streamer"
_LATEST_URL = "https://github.com/%s/releases/latest" % _REPO      # переадресовує на …/tag/vX.Y.Z
_ZIP_URL = "https://github.com/%s/archive/refs/tags/v%%s.zip" % _REPO
# З архіву беремо лише код: Python, Qt і ffmpeg у теці runtime оновлення не чіпає
_UPDATED_PATHS = ("eazy_streamer/", "VERSION", "eazy-streamer")


def local_version() -> str:
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"


def uses_git() -> bool:
    return os.path.exists(os.path.join(ROOT, ".git")) and shutil.which("git") is not None


def can_update() -> bool:
    return True


def _as_numbers(version: str) -> tuple:
    return tuple(int(part) for part in version.strip().split(".") if part.isdigit())


def _latest_release() -> str:
    """Номер останнього релізу. Порожньо — релізів ще немає."""
    request = urllib.request.Request(_LATEST_URL, headers={"User-Agent": "eazy_streamer"})
    with urllib.request.urlopen(request, timeout=30) as response:
        final = response.geturl()
    return final.rsplit("/tag/v", 1)[1].strip("/") if "/tag/v" in final else ""


def _download(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "eazy_streamer"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("файл завеликий")
    return data


def _unpack(archive: bytes) -> None:
    """Розпакувати архів GitHub поверх програми. Усе в ньому лежить у теці «назва-main/»."""
    root = os.path.realpath(ROOT)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for item in z.infolist():
            relative = item.filename.split("/", 1)[1] if "/" in item.filename else ""
            if not relative or item.is_dir() or not relative.startswith(_UPDATED_PATHS):
                continue
            target = os.path.realpath(os.path.join(root, relative))
            if not target.startswith(root + os.sep):
                continue        # шлях веде за межі програми — такого не пишемо
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target + ".new", "wb") as f:
                f.write(z.read(item))
            os.replace(target + ".new", target)
            if not os.name == "nt" and (item.external_attr >> 16) & 0o111:
                os.chmod(target, 0o755)


class Updater(QObject):
    checking = Signal()
    up_to_date = Signal()
    available = Signal(str)         # версія, що чекає на GitHub
    failed = Signal(str)
    updated = Signal()
    _from_thread = Signal(str, str)     # результат фонового завантаження -> у потік вікна

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self.busy = False
        self._remote = ""
        self._from_thread.connect(self._on_thread_result)

    def check(self):
        if self.busy or not can_update():
            return
        self.busy = True
        self.checking.emit()
        if not uses_git():
            self._in_background(self._check_by_download)
            return
        self._git(["fetch", "--quiet"], self._after_fetch)

    def apply(self):
        if self.busy or not can_update():
            return
        self.busy = True
        self.checking.emit()
        if not uses_git():
            self._in_background(self._apply_by_download)
            return
        # --ff-only: лише «домотати» нові коміти. Якщо файли програми хтось правив
        # на місці, git відмовиться, а не зліпить щось сам
        self._git(["pull", "--ff-only", "--quiet"], self._after_pull)

    # --- без git: завантаження з GitHub ---------------------------------

    def _in_background(self, work):
        # Мережа може думати довго — не тримаємо вікно, працюємо в окремому потоці
        threading.Thread(target=work, daemon=True).start()

    def _check_by_download(self):
        try:
            remote = _latest_release()
        except Exception as e:
            log.info("Оновлення: не вдалося прочитати версію з GitHub: %s", e)
            self._from_thread.emit("failed", "Не вдалося зв'язатися з GitHub")
            return
        log.info("Оновлення: тут %s, останній реліз на GitHub %s", local_version(), remote or "немає")
        self._remote = remote
        newer = bool(remote) and _as_numbers(remote) > _as_numbers(local_version())
        self._from_thread.emit("available" if newer else "up_to_date", remote)

    def _apply_by_download(self):
        try:
            version = self._remote or _latest_release()
            _unpack(_download(_ZIP_URL % version, 50_000_000))
        except Exception as e:
            log.error("Оновлення: не встановилось: %s", e)
            self._from_thread.emit("failed", "Оновлення не встановилось: %s" % str(e)[:100])
            return
        log.info("Оновлення: файли замінено, версія %s", local_version())
        self._from_thread.emit("updated", "")

    def _on_thread_result(self, kind: str, text: str):
        if kind == "available":
            self._finish(self.available, text)
        elif kind == "failed":
            self._finish(self.failed, text)
        elif kind == "updated":
            self._finish(self.updated)
        else:
            self._finish(self.up_to_date)

    # --- кроки ---------------------------------------------------------

    def _after_fetch(self, code: int, _out: str, _err: str):
        if code != 0:
            self._finish(self.failed, "Не вдалося зв'язатися з GitHub")
            return
        self._git(["rev-list", "--count", "HEAD..@{upstream}"], self._after_count)

    def _after_count(self, code: int, out: str, _err: str):
        if code != 0 or not out.strip().isdigit():
            self._finish(self.failed, "Не вдалося порівняти версії")
        elif int(out.strip()) == 0:
            self._finish(self.up_to_date)
        else:
            self._git(["show", "@{upstream}:VERSION"], self._after_version)

    def _after_version(self, code: int, out: str, _err: str):
        version = out.strip().splitlines()[0][:20] if code == 0 and out.strip() else ""
        if version == local_version():
            version = ""            # номер забули підняти — це все одно новіша збірка
        self._finish(self.available, version)

    def _after_pull(self, code: int, _out: str, err: str):
        if code == 0:
            self._finish(self.updated)
        else:
            lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
            self._finish(self.failed, "Оновлення не встановилось: %s" % (lines[-1][:120] if lines else "git відмовив"))

    # --- git -----------------------------------------------------------

    def _finish(self, signal, *args):
        self.busy = False
        signal.emit(*args)

    def _git(self, args: list[str], then):
        proc = QProcess(self)
        self._proc = proc
        env = QProcessEnvironment.systemEnvironment()
        # Жодних запитань у термінал: немає доступу — значить, просто помилка
        env.insert("GIT_TERMINAL_PROMPT", "0")
        env.insert("GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oConnectTimeout=10")
        env.insert("LC_ALL", "C")
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(ROOT)

        def finished(code, status, p=proc):
            if p is not self._proc:
                return
            self._proc = None
            try:
                out = bytes(p.readAllStandardOutput().data()).decode("utf-8", "replace")
                err = bytes(p.readAllStandardError().data()).decode("utf-8", "replace")
            except RuntimeError:    # вікно закрили посеред перевірки: Qt уже прибрав процес
                return
            p.deleteLater()
            log.info("git %s: код %s %s", " ".join(args), code, (out + " " + err).strip()[:300])
            then(code if status == QProcess.ExitStatus.NormalExit else -1, out, err)

        def error(_e, p=proc):
            try:
                failed = p is self._proc and p.error() == QProcess.ProcessError.FailedToStart
            except RuntimeError:    # те саме: програму закривають
                return
            if failed:
                self._proc = None
                p.deleteLater()
                self._finish(self.failed, "Не знайдено git")

        proc.finished.connect(finished)
        proc.errorOccurred.connect(error)
        proc.start("git", args)
        QTimer.singleShot(_TIMEOUT_MS, lambda p=proc: p.kill() if p is self._proc else None)
