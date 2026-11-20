"""Збирає командний рядок ffmpeg. Тут немає Qt — лише списки аргументів."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from . import osdep
from .devices import TEST_KEY, Device, pick_capture

PREVIEW_W, PREVIEW_H, PREVIEW_FPS = 480, 270, 10
PREVIEW_FRAME_BYTES = PREVIEW_W * PREVIEW_H * 3
STREAM_FPS = 30

# ffmpeg-назви форматів, які карта віддає сама
_INPUT_FORMATS = {"MJPG": "mjpeg", "YUYV": "yuyv422", "NV12": "nv12", "H264": "h264"}
_DSHOW_FORMATS = {"MJPG": ["-vcodec", "mjpeg"], "YUYV": ["-pixel_format", "yuyv422"],
                  "NV12": ["-pixel_format", "nv12"]}


@dataclass(frozen=True)
class Quality:
    key: str
    label: str
    height: int
    kbit: int


QUALITIES = (
    Quality("low", "480p", 480, 1000),
    Quality("mid", "720p", 720, 2500),
    Quality("high", "1080p", 1080, 4500),
)
DEFAULT_QUALITY = "mid"

# Який апаратний кодер пройшов пробу: vaapi (Linux), nvenc / qsv / amf (Windows).
# None — кодуємо процесором. Виставляє hwenc.Probe.
HW_KIND: str | None = None
# Для vaapi — вузол відеокарти (/dev/dri/renderD…); для решти — просто назва кодера.
# Порожньо = апаратного кодера немає.
HW_NODE: str | None = None
# Якщо карта кодує лише з конкретним драйвером VAAPI (напр. старий i965 для Intel) — його ім'я
HW_DRIVER: str | None = None
# Режим low-power: інший кодер усередині Intel; вільний драйвер iHD вміє лише його
HW_LOW_POWER = False
# (драйвер, low-power) у порядку спроб. None = хай система обере драйвер сама
HW_VARIANTS = ((None, False), (None, True), ("iHD", True), ("i965", False), ("radeonsi", False))


# Кодери Windows у порядку спроб: дискретна NVIDIA, вбудований Intel, AMD
WIN_ENCODERS = ("nvenc", "qsv", "amf")


def hw_probe_args(node: str, low_power: bool = False, kind: str = "vaapi") -> list[str]:
    """Короткий пробний запуск: чи вміє ця відеокарта кодувати H.264."""
    head = ["-hide_banner", "-loglevel", "error"]
    if kind == "vaapi":
        head += ["-vaapi_device", node]
    return head + ["-f", "lavfi", "-i", "color=size=1280x720:rate=30", "-frames:v", "10",
                   "-vf", _to_encoder(kind)] + _hw_encoder(2500, low_power, kind=kind) + ["-f", "null", "-"]


def _to_encoder(kind: str | None) -> str:
    """Кінець ланцюжка фільтрів перед кодером: у якому вигляді кодер приймає кадр."""
    if kind == "vaapi":
        return "format=nv12,hwupload"       # кадр треба перенести в пам'ять GPU
    if kind:
        return "format=nv12"                # nvenc, qsv, amf беруть звичайний кадр і вивантажують самі
    return "format=yuv420p"


def _hw_encoder(kbit: int, low_power: bool, gop: int = STREAM_FPS * 2, kind: str | None = None) -> list[str]:
    kind = kind or HW_KIND or "vaapi"
    rate = ["-b:v", "%dk" % kbit, "-maxrate", "%dk" % kbit, "-bufsize", "%dk" % (kbit * 2),
            "-bf", "0", "-g", str(gop)]
    if kind == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "cbr"] + rate
    if kind == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0"] + rate
    if kind == "amf":
        return ["-c:v", "h264_amf", "-usage", "lowlatency", "-rc", "cbr"] + rate
    return ["-c:v", "h264_vaapi"] + rate + (["-low_power", "1"] if low_power else [])


def _cpu_encoder(kbit: int, gop: int = STREAM_FPS * 2) -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "main",
            "-b:v", "%dk" % kbit, "-maxrate", "%dk" % kbit, "-bufsize", "%dk" % (kbit * 2),
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]


def quality(key: str) -> Quality:
    for q in QUALITIES:
        if q.key == key:
            return q
    return quality(DEFAULT_QUALITY)


def is_stream_url(url: str) -> bool:
    url = url.strip().lower()
    return url.startswith(("rtmp://", "rtmps://")) and len(url) > len("rtmps://") + 2


# Скільки роботи бере на себе GPU:
#   0 — нічого, усе робить CPU;
#   1 — лише кодування H.264;
#   2 — ще й розтискання MJPEG, перетворення кольору і масштабування (CPU майже вільний).
# GPU_PIPE виставляє проба; LEVEL_CAP знижує рушій для джерела, з яким GPU не впорався.
GPU_PIPE = False
LEVEL_CAP: dict[str, int] = {}
LEVEL_NAMES = ("усе на CPU", "GPU кодує", "GPU розтискає, масштабує і кодує")


def _key(device: Device | None) -> str:
    return device.key if device is not None else TEST_KEY


def level_for(device: Device | None) -> int:
    top = 2 if (HW_NODE and GPU_PIPE and HW_KIND == "vaapi") else 1 if HW_NODE else 0
    if _key(device) == TEST_KEY:
        top = min(top, 1)       # тестову картинку малює CPU — ганяти її через GPU лише дорожче
    return min(top, LEVEL_CAP.get(_key(device), 2))


def lower_level(device: Device | None) -> int:
    LEVEL_CAP[_key(device)] = max(0, level_for(device) - 1)
    return LEVEL_CAP[_key(device)]


def capture(device: Device | None, want_height: int, preview: bool = False):
    """(формат, розмір, частота), які попросимо в джерела."""
    if device is None or device.key == TEST_KEY:
        return None, (1280, 720), float(STREAM_FPS)
    return pick_capture(device, want_height, preview)


def source_size(device: Device | None, want_height: int) -> tuple[int, int] | None:
    """Кадр, який реально прийде з джерела. None — карта дасть будь-який, який попросимо."""
    return capture(device, want_height)[1]


def stream_kbit(device: Device | None, q: Quality) -> int:
    """Бітрейт під справжню картинку: джерело 480p не варте 4,5 Мбіт/с, хоч би що натиснули."""
    size = source_size(device, q.height)
    if not size or size[1] >= q.height:
        return q.kbit
    fitting = [x.kbit for x in QUALITIES if x.height <= size[1]]
    return min(q.kbit, max(fitting) if fitting else QUALITIES[0].kbit)


def out_height(device: Device | None, q: Quality) -> int:
    """Висота кадру, що справді піде в ефір: картинку не розтягуємо понад джерело."""
    size = source_size(device, q.height)
    return min(q.height, size[1]) if size else q.height


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _fit(size: tuple[int, int], box_w: int, box_h: int) -> tuple[int, int]:
    """Вписати кадр у рамку, зберігши пропорції."""
    scale = min(box_w / size[0], box_h / size[1])
    return min(box_w, _even(size[0] * scale)), min(box_h, _even(size[1] * scale))


_PAD = "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,format=rgb24" % (PREVIEW_W, PREVIEW_H)
_CPU_PREVIEW = "fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease:flags=fast_bilinear,%s" % (
    PREVIEW_FPS, PREVIEW_W, PREVIEW_H, _PAD)


def _gpu_graph(fourcc: str | None, size: tuple[int, int], out_h: int, thin: str = "") -> str:
    """Рівень 2: кадр потрапляє на GPU одразу, на CPU повертається лише крихітний перегляд."""
    out_w = _even(size[0] * out_h / size[1])
    pw, ph = _fit(size, PREVIEW_W, PREVIEW_H)
    if fourcc == "MJPG":
        head = "[0:v]%ssplit=2[a][p]" % thin            # розтиснув GPU, кадр уже там
    else:
        head = "[0:v]%sformat=%s,hwupload,split=2[a][p]" % (
            thin, "yuyv422" if fourcc == "YUYV" else "nv12")
    return ("%s;[a]scale_vaapi=w=%d:h=%d:format=nv12[air];"
            "[p]fps=%d,scale_vaapi=w=%d:h=%d:format=nv12,hwdownload,format=nv12,%s[pv]" % (
                head, out_w, out_h, PREVIEW_FPS, pw, ph, _PAD))


def hw_pipe_probe_args(node: str, low_power: bool = False) -> list[str]:
    """Проба рівня 2: нестиснений кадр на GPU, масштаб, кодування і перегляд назад."""
    graph = _gpu_graph("YUYV", (1280, 720), 480)
    return ["-hide_banner", "-loglevel", "error", "-vaapi_device", node,
            "-f", "lavfi", "-i", "color=size=1280x720:rate=30", "-filter_complex", graph,
            "-map", "[air]", "-frames:v", "10"] + _hw_encoder(1000, low_power) + [
            "-f", "null", "-", "-map", "[pv]", "-frames:v", "3", "-f", "null", "-"]


def build(device: Device | None, url: str | None, q: Quality, level: int | None = None) -> list[str]:
    """Аргументи ffmpeg (без самого слова ffmpeg).

    url=None — лише перегляд. Інакше одна карта йде і в ефір, і в перегляд:
    відкрити карту двома процесами не можна, тому перегляд — другий вихід.
    """
    preview_out = ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    args = ["-hide_banner", "-loglevel", "warning", "-nostats"]
    if not osdep.IS_WIN:
        args.insert(1, "-nostdin")      # на Windows ввід потрібен: через нього просимо ffmpeg вийти
    if not url:
        return args + _input(device, capture(device, 480, preview=True)) + [
            "-vf", _CPU_PREVIEW, "-an"] + preview_out

    fourcc, size, rate = cap = capture(device, q.height)
    if level is None:
        level = level_for(device)
    if level == 2 and not size:
        level = 1               # розмір кадру невідомий — GPU-схему не зібрати
    # Раз на 2 с ffmpeg звітує в stderr: кадри, бітрейт, пропуски, швидкість
    args += ["-progress", "pipe:2", "-stats_period", "2"]
    if level == 2 and fourcc == "MJPG":
        args += ["-hwaccel", "vaapi", "-hwaccel_device", HW_NODE, "-hwaccel_output_format", "vaapi"]
    elif level >= 1 and HW_KIND == "vaapi":
        args += ["-vaapi_device", HW_NODE]
    args += _input(device, cap)
    # Тиха звукова доріжка: частина серверів і плеєрів не приймає потік без звуку
    args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100"]

    # В ефір іде стільки кадрів, скільки дає джерело: 25 — то 25, 10 — то 10, без подвоєнь.
    # Лише якщо карта не вміє менше ніж 60 — беремо кожен другий, щоб не кодувати зайвого.
    step = int(-(-rate // STREAM_FPS)) if rate and rate > STREAM_FPS * 1.2 else 1
    thin = "framestep=%d," % step if step > 1 else ""
    gop = max(20, int(round(2 * rate / step))) if rate else STREAM_FPS * 2     # ключовий кадр раз на 2 с
    if level == 2:
        graph = _gpu_graph(fourcc, size, min(q.height, size[1]), thin)
    else:
        air = "scale=w=-2:h='min(%d,ih)':flags=bilinear,%s" % (
            q.height, _to_encoder(HW_KIND if level == 1 else None))
        graph = "[0:v]%ssplit=2[a][p];[a]%s[air];[p]%s[pv]" % (thin, air, _CPU_PREVIEW)
    args += ["-filter_complex", graph, "-map", "[air]", "-map", "1:a"]
    kbit = stream_kbit(device, q)
    args += _hw_encoder(kbit, HW_LOW_POWER, gop) if level >= 1 else _cpu_encoder(kbit, gop)
    args += ["-fps_mode", "vfr"]
    args += [
        "-c:a", "aac", "-b:a", "32k",
        "-shortest", "-flvflags", "no_duration_filesize",
        "-f", "flv", url.strip(),
        "-map", "[pv]",
    ]
    return args + preview_out


def _input(device: Device | None, cap) -> list[str]:
    if device is None or device.key == TEST_KEY:
        return ["-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=%d" % STREAM_FPS]
    if sys.platform.startswith("linux"):
        fourcc, size, rate = cap
        args = ["-f", "v4l2"]
        if fourcc in _INPUT_FORMATS:
            args += ["-input_format", _INPUT_FORMATS[fourcc]]
        if size:
            args += ["-video_size", "%dx%d" % size]
        if rate:
            args += ["-framerate", "%g" % rate]
        return args + ["-i", device.path]
    if osdep.IS_WIN:
        fourcc, size, rate = cap
        # rtbufsize: запас на випадок, коли кодер на мить відстав; типових 3 МБ не вистачає й на кадр 1080p
        args = ["-f", "dshow", "-rtbufsize", "256M"]
        if fourcc in _DSHOW_FORMATS:
            args += _DSHOW_FORMATS[fourcc]
        elif fourcc and fourcc.startswith("pix:"):
            args += ["-pixel_format", fourcc[4:]]
        elif fourcc and fourcc.startswith("codec:"):
            args += ["-vcodec", fourcc[6:]]
        if size:
            args += ["-video_size", "%dx%d" % size]
        if rate:
            args += ["-framerate", "%g" % rate]
        return args + ["-i", device.path]
    raise NotImplementedError("Захоплення з карт зроблено лише для Linux і Windows")


# Помилки, після яких винен GPU, а не мережа чи карта захоплення
_HW_ERROR = re.compile(r"vaapi|nvenc|nvcuda|qsv|amf|mfx|hwaccel|hwupload|hwdownload|hardware|"
                       r"Impossible to convert|No capable devices|Error while opening encoder|"
                       r"Error reinitializing filters|Failed to (configure|inject)", re.I)


def is_hw_error(stderr_tail: str) -> bool:
    return bool(_HW_ERROR.search(stderr_tail[-800:]))


def explain(stderr_tail: str, url: str | None) -> str:
    """Останні рядки ffmpeg — людською мовою. Посилання з ключем назовні не віддаємо."""
    text = stderr_tail
    if url:
        text = text.replace(url.strip(), "…")
    low = text.lower()
    known = (
        ("stalled", "потік завис"),
        ("device or resource busy", "карта зайнята іншою програмою"),
        ("no such device", "карту від'єднано"),
        ("no such file or directory", "карту не знайдено"),
        ("cannot open video device", "карта не відкривається"),
        ("could not find video device", "карту не знайдено"),
        ("error parsing options", "ця версія ffmpeg не приймає команду програми — оновіть програму"),
        ("unrecognized option", "ця версія ffmpeg не приймає команду програми — оновіть програму"),
        ("could not run graph", "карта зайнята іншою програмою"),
        ("could not set video options", "карта не приймає цей формат"),
        ("failed to resolve hostname", "немає інтернету або невірна адреса сервера"),
        ("connection refused", "сервер відхилив з'єднання"),
        ("connection timed out", "сервер не відповідає"),
        ("network is unreachable", "немає мережі"),
        ("broken pipe", "зв'язок із сервером обірвався"),
        ("connection reset", "зв'язок із сервером обірвався"),
        ("error in the pull function", "зв'язок із сервером обірвався"),
        ("input/output error", "сервер не прийняв потік — перевірте посилання"),
        ("unknown encoder", "у цьому ffmpeg немає кодера H.264"),
    )
    for needle, human in known:
        if needle in low:
            return human
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:140] if lines else "потік зупинився"
