"""Збирає командний рядок ffmpeg. Тут немає Qt — лише списки аргументів."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .devices import TEST_KEY, Device, pick_capture

PREVIEW_W, PREVIEW_H, PREVIEW_FPS = 480, 270, 10
PREVIEW_FRAME_BYTES = PREVIEW_W * PREVIEW_H * 3
STREAM_FPS = 30

# ffmpeg-назви форматів, які карта віддає сама
_INPUT_FORMATS = {"MJPG": "mjpeg", "YUYV": "yuyv422", "NV12": "nv12", "H264": "h264"}


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

# Вузол відеокарти (/dev/dri/renderD…), який пройшов пробу кодування.
# None — кодуємо процесором. Виставляє hwenc.Probe, скидає engine при збої.
HW_NODE: str | None = None


def hw_probe_args(node: str) -> list[str]:
    """Короткий пробний запуск: чи вміє ця відеокарта кодувати H.264."""
    return ["-hide_banner", "-loglevel", "error", "-vaapi_device", node,
            "-f", "lavfi", "-i", "color=size=1280x720:rate=30", "-frames:v", "10",
            "-vf", "format=nv12,hwupload"] + _hw_encoder(2500) + ["-f", "null", "-"]


def _hw_encoder(kbit: int) -> list[str]:
    return ["-c:v", "h264_vaapi", "-b:v", "%dk" % kbit, "-maxrate", "%dk" % kbit,
            "-bufsize", "%dk" % (kbit * 2), "-bf", "0", "-g", str(STREAM_FPS * 2)]


def _cpu_encoder(kbit: int) -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "main",
            "-b:v", "%dk" % kbit, "-maxrate", "%dk" % kbit, "-bufsize", "%dk" % (kbit * 2),
            "-g", str(STREAM_FPS * 2), "-keyint_min", str(STREAM_FPS * 2), "-sc_threshold", "0"]


def quality(key: str) -> Quality:
    for q in QUALITIES:
        if q.key == key:
            return q
    return quality(DEFAULT_QUALITY)


def is_stream_url(url: str) -> bool:
    url = url.strip().lower()
    return url.startswith(("rtmp://", "rtmps://")) and len(url) > len("rtmps://") + 2


def build(device: Device | None, url: str | None, q: Quality) -> list[str]:
    """Аргументи ffmpeg (без самого слова ffmpeg).

    url=None — лише перегляд. Інакше одна карта йде і в ефір, і в перегляд:
    відкрити карту двома процесами не можна, тому перегляд — другий вихід.
    """
    hw = HW_NODE if url else None
    args = ["-hide_banner", "-nostdin", "-loglevel", "error"]
    if hw:
        args += ["-vaapi_device", hw]
    args += _input(device, q.height if url else 480)

    preview = "fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease:flags=fast_bilinear," \
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,format=rgb24" % (
                  PREVIEW_FPS, PREVIEW_W, PREVIEW_H, PREVIEW_W, PREVIEW_H)
    preview_out = ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]

    if not url:
        return args + ["-vf", preview, "-an"] + preview_out

    # Тиха звукова доріжка: частина серверів і плеєрів не приймає потік без звуку
    args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100"]
    air = "fps=%d,scale=w=-2:h='min(%d,ih)':flags=bilinear,%s" % (
        STREAM_FPS, q.height, "format=nv12,hwupload" if hw else "format=yuv420p")
    graph = "[0:v]split=2[a][p];[a]%s[air];[p]%s[pv]" % (air, preview)
    args += ["-filter_complex", graph, "-map", "[air]", "-map", "1:a"]
    args += _hw_encoder(q.kbit) if hw else _cpu_encoder(q.kbit)
    args += [
        "-c:a", "aac", "-b:a", "32k",
        "-shortest", "-flvflags", "no_duration_filesize",
        "-f", "flv", url.strip(),
        "-map", "[pv]",
    ]
    return args + preview_out


def _input(device: Device | None, want_height: int) -> list[str]:
    if device is None or device.key == TEST_KEY:
        return ["-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=%d" % STREAM_FPS]
    if sys.platform.startswith("linux"):
        fourcc, size = pick_capture(device, want_height)
        args = ["-f", "v4l2", "-thread_queue_size", "64"]
        if fourcc in _INPUT_FORMATS:
            args += ["-input_format", _INPUT_FORMATS[fourcc]]
        if size:
            args += ["-video_size", "%dx%d" % size]
        return args + ["-i", device.path]
    raise NotImplementedError("Захоплення з карт поки зроблено лише для Linux")


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
