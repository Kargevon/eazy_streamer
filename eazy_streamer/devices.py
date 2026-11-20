"""Пошук карт відеозахоплення.

Linux: питаємо ядро напряму (V4L2 ioctl), без сторонніх утиліт.
Інші системи поки повертають порожній список — туди піде окремий код.
"""

from __future__ import annotations

import glob
import os
import struct
import sys
from dataclasses import dataclass, field

TEST_KEY = "test"
TEST_NAME = "Тестова картинка (без карти)"

# Номери ioctl з <linux/videodev2.h>
_VIDIOC_QUERYCAP = 0x80685600
_VIDIOC_ENUM_FMT = 0xC0405602
_VIDIOC_ENUM_FRAMESIZES = 0xC02C564A
_VIDIOC_ENUM_FRAMEINTERVALS = 0xC034564B
_CAP_VIDEO_CAPTURE = 0x00000001
_CAP_DEVICE_CAPS = 0x80000000
_BUF_TYPE_VIDEO_CAPTURE = 1
_FRMSIZE_DISCRETE = 1

# Сирі вузли вбудованих камер ноутбуків: їх десятки, і ffmpeg з них не читає
_SKIP_DRIVERS = {"isys", "ipu6", "intel-ipu6-isys", "ipu3-cio2"}


@dataclass
class Device:
    key: str            # стале ім'я, яке зберігаємо в налаштуваннях
    path: str           # що віддаємо ffmpeg (/dev/videoN)
    name: str           # що бачить людина
    formats: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # (формат, ширина, висота) -> частоти кадрів, які карта вміє; порожньо — не сказала
    rates: dict[tuple[str, int, int], list[float]] = field(default_factory=dict)


def list_devices() -> list[Device]:
    if not sys.platform.startswith("linux"):
        return []
    links = _stable_links()
    found = []
    for node in sorted(glob.glob("/dev/video*"), key=_node_number):
        dev = _probe(node)
        if dev is None:
            continue
        dev.key = links.get(node, node)
        found.append(dev)
    return found


def pick_capture(dev: Device, want_height: int, preview: bool = False
                 ) -> tuple[str | None, tuple[int, int] | None, float | None]:
    """Формат, розмір і частота кадрів, які просимо в карти.

    Беремо найменший кадр, якого вистачає на потрібну якість, і 30 к/с, а не 60:
    усе зайве процесор однаково мусив би прийняти й викинути.
    """
    if not dev.formats:
        return None, None, None
    options = []
    for order, fourcc in enumerate(sorted(dev.formats, key=_format_rank)):
        size = _pick_size(dev.formats[fourcc], want_height)
        rates = dev.rates.get((fourcc,) + size, []) if size else []
        smooth = not rates or max(rates) >= 25      # невідомо — вважаємо, що плавно
        area = size[0] * size[1] if size else 0
        enough = bool(size) and size[1] >= want_height
        options.append((not smooth, not enough, area if enough else -area, order, fourcc, size, rates))
    # Спершу плавні, далі ті, яким вистачає висоти, далі менший кадр, далі нестиснений формат
    _, _, _, _, fourcc, size, rates = min(options, key=lambda o: o[:4])
    return fourcc, size, _pick_rate(rates, preview)


def _format_rank(fourcc: str) -> int:
    # Нестиснене не треба розтискати; MJPEG — наступний; решту ffmpeg може й не вміти
    return {"NV12": 0, "YUYV": 1, "MJPG": 2}.get(fourcc, 9)


def _pick_size(sizes: list[tuple[int, int]], want_height: int) -> tuple[int, int] | None:
    if not sizes:
        return None
    wide = [s for s in sizes if abs(s[0] / s[1] - 16 / 9) < 0.05] or sizes
    enough = [s for s in wide if s[1] >= want_height]
    if enough:
        return min(enough, key=lambda s: s[0] * s[1])
    return max(wide, key=lambda s: s[0] * s[1])


def _pick_rate(rates: list[float], preview: bool) -> float | None:
    if not rates:
        return None
    floor = 10 if preview else 25       # перегляду досить 10 к/с, ефіру треба плавність
    if not preview and any(abs(r - 30) < 0.5 for r in rates):
        return 30
    good = [r for r in rates if r >= floor]
    return min(good) if good else max(rates)


def _node_number(node: str) -> int:
    digits = node[len("/dev/video"):]
    return int(digits) if digits.isdigit() else 1 << 30


def _stable_links() -> dict[str, str]:
    """/dev/videoN -> ім'я, що не міняється після перезавантаження.

    by-id прив'язане до самої карти, by-path — до USB-гнізда.
    """
    links: dict[str, str] = {}
    for folder in ("/dev/v4l/by-path", "/dev/v4l/by-id"):
        for link in sorted(glob.glob(folder + "/*")):
            links[os.path.realpath(link)] = link
    return links


def _probe(node: str) -> Device | None:
    import fcntl

    try:
        fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        cap = bytearray(104)
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, cap)
        driver = cap[:16].split(b"\0", 1)[0].decode("ascii", "replace")
        if driver in _SKIP_DRIVERS:
            return None
        card = cap[16:48].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
        capabilities, device_caps = struct.unpack_from("<II", cap, 84)
        caps = device_caps if capabilities & _CAP_DEVICE_CAPS else capabilities
        if not caps & _CAP_VIDEO_CAPTURE:
            return None
        formats = _formats(fd)
        if not formats:
            return None
        rates = {(fourcc, w, h): _rates(fd, fourcc, w, h)
                 for fourcc, sizes in formats.items() for w, h in sizes}
        name = "%s (%s)" % (card or "Відеопристрій", os.path.basename(node))
        return Device(key=node, path=node, name=name, formats=formats, rates=rates)
    except OSError:
        return None
    finally:
        os.close(fd)


def _formats(fd: int) -> dict[str, list[tuple[int, int]]]:
    import fcntl

    formats: dict[str, list[tuple[int, int]]] = {}
    for index in range(64):
        desc = bytearray(64)
        struct.pack_into("<II", desc, 0, index, _BUF_TYPE_VIDEO_CAPTURE)
        try:
            fcntl.ioctl(fd, _VIDIOC_ENUM_FMT, desc)
        except OSError:
            break
        pixelformat = struct.unpack_from("<I", desc, 44)[0]
        fourcc = struct.pack("<I", pixelformat).decode("ascii", "replace")
        formats[fourcc] = _sizes(fd, pixelformat)
    return formats


def _rates(fd: int, fourcc: str, width: int, height: int) -> list[float]:
    import fcntl

    pixelformat = struct.unpack("<I", fourcc.encode("ascii", "replace")[:4].ljust(4))[0]
    rates: list[float] = []
    for index in range(32):
        ival = bytearray(52)
        struct.pack_into("<IIII", ival, 0, index, pixelformat, width, height)
        try:
            fcntl.ioctl(fd, _VIDIOC_ENUM_FRAMEINTERVALS, ival)
        except OSError:
            break
        kind, numerator, denominator = struct.unpack_from("<III", ival, 16)
        if kind != _FRMSIZE_DISCRETE or not numerator:
            return []       # карта приймає будь-яку частоту — не нав'язуємо
        rates.append(denominator / numerator)
    return rates


def _sizes(fd: int, pixelformat: int) -> list[tuple[int, int]]:
    import fcntl

    sizes: list[tuple[int, int]] = []
    for index in range(256):
        frm = bytearray(44)
        struct.pack_into("<II", frm, 0, index, pixelformat)
        try:
            fcntl.ioctl(fd, _VIDIOC_ENUM_FRAMESIZES, frm)
        except OSError:
            break
        kind, width, height = struct.unpack_from("<III", frm, 8)
        if kind != _FRMSIZE_DISCRETE:
            return []   # карта приймає будь-який розмір — попросимо потрібний
        sizes.append((width, height))
    return sizes
