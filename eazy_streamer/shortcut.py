"""Ярлик «Eazy Streamer» з іконкою в теці програми (Windows).

Навіщо: EazyStreamer.exe — це підписаний pythonw.exe, і іконку самого файла змінити не можна,
не зламавши підпис. Ярлик має власну іконку і запускає той самий exe. Його можна скопіювати
на робочий стіл. У ярлику записано повний шлях, тому після перенесення теки програма
при наступному запуску з exe створить його наново.

Ярлик створюємо через системний інтерфейс Windows (IShellLink), без сторонніх програм.
"""

from __future__ import annotations

import os
import sys

from . import osdep
from .log import log

NAME = "Eazy Streamer.lnk"


def refresh() -> None:
    """Створити або оновити ярлик поруч із програмою. Тихо нічого не робить, якщо це не той запуск."""
    if not osdep.IS_WIN or os.path.basename(sys.executable).lower() != "eazystreamer.exe":
        return
    link = os.path.join(osdep.ROOT, NAME)
    icon = os.path.join(osdep.ROOT, "eazy_streamer", "icon.ico")
    try:
        if read_target(link).lower() == sys.executable.lower():
            return                      # ярлик уже є і веде куди треба
    except Exception:
        pass
    try:
        create(link, sys.executable, osdep.ROOT, icon, "Стрімінг з карт відеозахоплення")
        log.info("Ярлик створено: %s", link)
    except Exception as e:
        log.warning("Ярлик створити не вдалося: %s", e)


def _guid(text: str):
    import ctypes

    buf = (ctypes.c_byte * 16)()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(buf))
    return buf


def _call(obj, index: int, restype, *argtypes):
    """Метод COM-об'єкта за номером у таблиці методів."""
    import ctypes

    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])


def _open():
    """Повертає (IShellLinkW, IPersistFile). Номери методів — з оголошень Windows SDK."""
    import ctypes

    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    link = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(_guid("{00021401-0000-0000-C000-000000000046}")), None, 1,
                           ctypes.byref(_guid("{000214F9-0000-0000-C000-000000000046}")), ctypes.byref(link))
    persist = ctypes.c_void_p()
    hr = _call(link, 0, ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
        link, ctypes.cast(ctypes.byref(_guid("{0000010b-0000-0000-C000-000000000046}")), ctypes.c_void_p),
        ctypes.byref(persist))
    if hr != 0 or not persist:
        raise OSError("IPersistFile недоступний: %#x" % (hr & 0xFFFFFFFF))
    return link, persist


def _release(*objects):
    import ctypes

    for obj in objects:
        _call(obj, 2, ctypes.c_ulong)(obj)


def _check(hr: int, what: str):
    if hr != 0:
        raise OSError("%s: %#x" % (what, hr & 0xFFFFFFFF))


def create(link_path: str, target: str, workdir: str, icon: str, description: str) -> None:
    import ctypes

    W, HR = ctypes.c_wchar_p, ctypes.c_long
    link, persist = _open()
    try:
        _check(_call(link, 20, HR, W)(link, target), "SetPath")
        _check(_call(link, 9, HR, W)(link, workdir), "SetWorkingDirectory")
        _check(_call(link, 7, HR, W)(link, description), "SetDescription")
        _check(_call(link, 17, HR, W, ctypes.c_int)(link, icon, 0), "SetIconLocation")
        _check(_call(persist, 6, HR, W, ctypes.c_int)(persist, link_path, 1), "Save")
    finally:
        _release(persist, link)


def read_target(link_path: str) -> str:
    """Куди веде ярлик. Кидає помилку, якщо файла немає або це не ярлик."""
    import ctypes

    if not os.path.exists(link_path):
        raise FileNotFoundError(link_path)
    W, HR = ctypes.c_wchar_p, ctypes.c_long
    link, persist = _open()
    try:
        _check(_call(persist, 5, HR, W, ctypes.c_ulong)(persist, link_path, 0), "Load")
        buf = ctypes.create_unicode_buffer(1024)
        _check(_call(link, 3, HR, W, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong)(link, buf, 1024, None, 0), "GetPath")
        return buf.value
    finally:
        _release(persist, link)
