"""Налаштування секцій. У посиланнях є ключі стрімів, тому файл не для чужих очей.

Linux: ~/.config/eazy_streamer/. Windows: тека data поруч із програмою.
"""

from __future__ import annotations

import json
import os
import sys

from . import osdep


def _profile_path() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "eazy_streamer", "streams.json")


def config_path() -> str:
    home = osdep.home_dir()
    return os.path.join(home, "data", "streams.json") if home else _profile_path()


def _read() -> dict:
    # Спершу нове місце; якщо там порожньо — старе в профілі (версії до 0.5.3 на Windows)
    for path in dict.fromkeys((config_path(), _profile_path())):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def load() -> list[dict]:
    data = _read()
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        return []
    return [s for s in streams if isinstance(s, dict)]


def load_settings() -> dict:
    data = _read()
    settings = data.get("settings") if isinstance(data, dict) else None
    return settings if isinstance(settings, dict) else {}


def save(streams: list[dict], settings: dict | None = None) -> None:
    path = config_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    # 0600: файл із ключами читає лише власник. Спершу тимчасовий файл,
    # потім заміна — щоб збій живлення не лишив половину файла.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"streams": streams, "settings": settings or {}}, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
