"""Оновлення через git: програма стоїть як клон репозиторію, тож «оновити» = git pull."""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .log import log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TIMEOUT_MS = 40000


def local_version() -> str:
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"


def can_update() -> bool:
    return os.path.exists(os.path.join(ROOT, ".git"))


class Updater(QObject):
    checking = Signal()
    up_to_date = Signal()
    available = Signal(str)         # версія, що чекає на GitHub
    failed = Signal(str)
    updated = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self.busy = False

    def check(self):
        if self.busy or not can_update():
            return
        self.busy = True
        self.checking.emit()
        self._git(["fetch", "--quiet"], self._after_fetch)

    def apply(self):
        if self.busy or not can_update():
            return
        self.busy = True
        self.checking.emit()
        # --ff-only: лише «домотати» нові коміти. Якщо файли програми хтось правив
        # на місці, git відмовиться, а не зліпить щось сам
        self._git(["pull", "--ff-only", "--quiet"], self._after_pull)

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
            out = bytes(p.readAllStandardOutput().data()).decode("utf-8", "replace")
            err = bytes(p.readAllStandardError().data()).decode("utf-8", "replace")
            p.deleteLater()
            log.info("git %s: код %s %s", " ".join(args), code, (out + " " + err).strip()[:300])
            then(code if status == QProcess.ExitStatus.NormalExit else -1, out, err)

        def error(_e, p=proc):
            if p is self._proc and p.error() == QProcess.ProcessError.FailedToStart:
                self._proc = None
                p.deleteLater()
                self._finish(self.failed, "Не знайдено git")

        proc.finished.connect(finished)
        proc.errorOccurred.connect(error)
        proc.start("git", args)
        QTimer.singleShot(_TIMEOUT_MS, lambda p=proc: p.kill() if p is self._proc else None)
