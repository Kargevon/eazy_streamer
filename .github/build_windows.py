"""Складає теку програми для Windows. Запускає GitHub Actions, людям цей файл не потрібен.

    python .github/build_windows.py assemble   — зібрати build/EazyStreamer
    python .github/build_windows.py pack       — запакувати її в zip і порахувати контрольну суму

Версії закріплено: у реліз іде рівно те, що пройшло перевірку.
"""

import hashlib
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PYTHON = "3.12.10"
PYSIDE = "6.8.3"
# 7.1 — та сама гілка, що в Debian 13, на якій усе міряли. Новіші збірки вимагають
# найсвіжішого драйвера NVIDIA (9.1 відмовила на драйвері з NVENC API 13.0), а нам треба,
# щоб працювало на любе голубе
FFMPEG = "7.1.1"
PYTHON_URL = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (PYTHON, PYTHON)
FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/%s/ffmpeg-%s-essentials_build.zip" % (FFMPEG, FFMPEG)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(BUILD, "EazyStreamer")
TOOLS = os.path.join(BUILD, "tools")       # ffprobe для перевірки; у реліз не йде
SITE = os.path.join(DIST, "runtime", "python", "Lib", "site-packages")

# Частини Qt, якими програма не користується. Обережно: лише очевидно зайве.
QT_DROP_PREFIXES = ("Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6Designer", "Qt6Help", "Qt6Sql", "Qt6Test",
                    "Qt6Concurrent", "Qt6PrintSupport", "Qt6Network", "Qt6OpenGL", "Qt6Xml", "Qt6DBus",
                    "Qt6Svg", "Qt6LabsAnimation", "Qt6LabsFolderListModel", "Qt6LabsQmlModels",
                    "Qt6LabsSettings", "Qt6LabsSharedImage", "Qt6LabsWavefrontMesh", "Qt6ShaderTools",
                    "Qt6StateMachine", "Qt6UiTools", "Qt6VirtualKeyboard",
                    "QtQuick", "QtQml", "QtPdf", "QtDesigner", "QtHelp", "QtSql", "QtTest", "QtConcurrent",
                    "QtPrintSupport", "QtNetwork", "QtOpenGL", "QtXml", "QtDBus", "QtSvg", "QtUiTools",
                    "QtStateMachine", "opengl32sw", "pyside6qml")
QT_DROP_DIRS = ("translations", "qml", "resources", "include", "typesystems", "glue", "doc", "scripts",
                "metatypes", "QtAsyncio")
QT_KEEP_PLUGINS = ("platforms", "styles", "imageformats", "iconengines")


def note(title, text):
    print("::notice title=%s::%s" % (title, str(text).replace("%", "%25").replace("\n", "%0A")), flush=True)


def download(url):
    print("завантажую", url, flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "eazy_streamer-build"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def size_mb(path):
    total = 0
    for folder, _dirs, files in os.walk(path):
        total += sum(os.path.getsize(os.path.join(folder, f)) for f in files)
    return total / 1e6


def assemble():
    shutil.rmtree(BUILD, ignore_errors=True)
    python_dir = os.path.join(DIST, "runtime", "python")
    os.makedirs(python_dir)
    os.makedirs(TOOLS)

    zipfile.ZipFile(io.BytesIO(download(PYTHON_URL))).extractall(python_dir)
    # Цей Python бачить лише перелічене тут: себе, свої пакунки і теку програми двома рівнями вище
    with open(os.path.join(python_dir, "python312._pth"), "w", newline="\r\n") as f:
        f.write("python312.zip\n.\n..\\..\nimport site\n")

    # EazyStreamer.exe — це pythonw.exe під іншим іменем: він підписаний Python Software
    # Foundation, і Windows його пускає. Власний непідписаний запускач блокував Smart App Control.
    # Поруч йому потрібні бібліотеки Python і свій перелік шляхів; програму відкриває sitecustomize.
    shutil.copy(os.path.join(python_dir, "pythonw.exe"), os.path.join(DIST, "EazyStreamer.exe"))
    for name in os.listdir(python_dir):
        if name.lower() in ("python312.dll", "python3.dll") or name.lower().startswith("vcruntime"):
            shutil.copy(os.path.join(python_dir, name), DIST)
    with open(os.path.join(DIST, "python312._pth"), "w", newline="\r\n") as f:
        f.write("runtime\\python\\python312.zip\nruntime\\python\nruntime\\python\\Lib\\site-packages\n.\nimport site\n")
    shutil.copy(os.path.join(ROOT, "launcher", "sitecustomize.py"), DIST)

    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-compile", "--target", SITE,
                           "--platform", "win_amd64", "--python-version", "3.12", "--only-binary=:all:",
                           "PySide6-Essentials==" + PYSIDE])
    before = size_mb(SITE)
    trim_qt(os.path.join(SITE, "PySide6"))
    for extra in os.listdir(SITE):
        if extra.endswith(".dist-info") or extra == "bin":
            shutil.rmtree(os.path.join(SITE, extra), ignore_errors=True)

    with zipfile.ZipFile(io.BytesIO(download(FFMPEG_URL))) as z:
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1]
            if base == "ffmpeg.exe":
                target = os.path.join(DIST, "runtime", "ffmpeg", "bin", base)
            elif base == "ffprobe.exe":
                target = os.path.join(TOOLS, base)
            else:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(z.read(name))

    shutil.copytree(os.path.join(ROOT, "eazy_streamer"), os.path.join(DIST, "eazy_streamer"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(os.path.join(ROOT, "VERSION"), DIST)
    note("Тека", "Qt: було %.0f МБ, після чистки %.0f МБ\nPython з Qt: %.0f МБ\nffmpeg: %.0f МБ\nусього: %.0f МБ" % (
        before, size_mb(SITE), size_mb(python_dir), size_mb(os.path.join(DIST, "runtime", "ffmpeg")), size_mb(DIST)))


def trim_qt(qt):
    for name in os.listdir(qt):
        path = os.path.join(qt, name)
        if os.path.isdir(path):
            if name in QT_DROP_DIRS:
                shutil.rmtree(path)
            continue
        if name.startswith(QT_DROP_PREFIXES) or (name.endswith(".exe")):
            os.remove(path)
    plugins = os.path.join(qt, "plugins")
    for name in os.listdir(plugins):
        if name not in QT_KEEP_PLUGINS:
            shutil.rmtree(os.path.join(plugins, name))
            continue
        for plugin in os.listdir(os.path.join(plugins, name)):
            if "svg" in plugin or "pdf" in plugin:      # їм потрібні бібліотеки, які ми викинули
                os.remove(os.path.join(plugins, name, plugin))


def pack():
    version = open(os.path.join(ROOT, "VERSION")).read().strip()
    if not os.path.exists(os.path.join(DIST, "EazyStreamer.exe")):
        sys.exit("у теці немає EazyStreamer.exe — запускач не зібрано")
    # Ярлик, який програма створила під час перевірки, веде в теку збірки — у реліз він не йде
    for name in os.listdir(DIST):
        if name.lower().endswith(".lnk"):
            os.remove(os.path.join(DIST, name))
    for leftover in ("logs", "data"):
        if os.path.exists(os.path.join(DIST, leftover)):
            sys.exit("у теці збірки лишилось %s — перевірка писала не туди" % leftover)
    for folder, dirs, _files in os.walk(DIST):
        for d in [d for d in dirs if d == "__pycache__"]:
            shutil.rmtree(os.path.join(folder, d))     # сліди перевірки в реліз не йдуть
    name = "EazyStreamer-%s-windows.zip" % version
    archive = os.path.join(BUILD, name)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder, _dirs, files in os.walk(DIST):
            for f in sorted(files):
                full = os.path.join(folder, f)
                z.write(full, os.path.join("EazyStreamer", os.path.relpath(full, DIST)))
    digest = hashlib.sha256(open(archive, "rb").read()).hexdigest()
    with open(archive + ".sha256", "w", newline="\n") as f:
        f.write("%s  %s\n" % (digest, name))
    note("Архів", "%s: %.0f МБ\nрозпакований: %.0f МБ\nEazyStreamer.exe: %.0f КБ\nsha256: %s" % (
        name, os.path.getsize(archive) / 1e6, size_mb(DIST),
        os.path.getsize(os.path.join(DIST, "EazyStreamer.exe")) / 1e3, digest))


def _fail_loudly(kind, value, trace):
    """Журнал збірки без входу в GitHub не прочитати — помилку кладемо у видимий підсумок."""
    import traceback
    text = "".join(traceback.format_exception(kind, value, trace))[-1800:]
    print("::error title=Збій складання::%s" % text.replace("%", "%25").replace("\r", "").replace("\n", "%0A"), flush=True)
    sys.__excepthook__(kind, value, trace)


if __name__ == "__main__":
    # Консоль Windows типово не в UTF-8 і падає на кирилиці
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.excepthook = _fail_loudly
    {"assemble": assemble, "pack": pack}[sys.argv[1]]()
