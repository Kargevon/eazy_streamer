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


def pick_capture(dev: Device, want_height: int) -> tuple[str | None, tuple[int, int] | None]:
    """Формат і розмір, які просимо в карти.

    Беремо найменший кадр, якого вистачає на потрібну якість: слабкому ПК
    не треба розтискати 1080p, щоб віддати 480p.
    """
    if not dev.formats:
        return None, None
    fourcc = "MJPG" if "MJPG" in dev.formats else next(iter(dev.formats))
    sizes = dev.formats[fourcc]
    if not sizes:
        return fourcc, None
    wide = [s for s in sizes if abs(s[0] / s[1] - 16 / 9) < 0.05] or sizes
    enough = [s for s in wide if s[1] >= want_height]
    if enough:
        return fourcc, min(enough, key=lambda s: s[0] * s[1])
    return fourcc, max(wide, key=lambda s: s[0] * s[1])


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
        name = "%s (%s)" % (card or "Відеопристрій", os.path.basename(node))
        return Device(key=node, path=node, name=name, formats=formats)
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
