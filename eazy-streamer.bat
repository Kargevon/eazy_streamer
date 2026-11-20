@echo off
rem Eazy Streamer launcher for Windows.
rem First start: downloads Python, Qt and ffmpeg into the "runtime" folder next to this file.
rem Next starts: just opens the program. Nothing is installed into the system.
rem Needs Windows 10 or newer (uses the built-in curl and tar).
setlocal
cd /d "%~dp0"
set "RT=%~dp0runtime"
set "PY=%RT%\python\python.exe"
set "PYVER=3.12.10"
set "FFMPEG_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
set "FFMPEG_URL2=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
if not exist "%RT%" mkdir "%RT%"

if exist "%PY%" goto have_python
echo [1/4] Downloading Python %PYVER% ...
curl -L --fail --retry 3 -o "%RT%\python.zip" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip" || goto fail
if exist "%RT%\python.tmp" rmdir /s /q "%RT%\python.tmp"
mkdir "%RT%\python.tmp"
tar -xf "%RT%\python.zip" -C "%RT%\python.tmp" || goto fail
del "%RT%\python.zip"
rem Let this Python see its own packages and the program folder (two levels up).
(echo python312.zip& echo .& echo ..\..& echo import site)> "%RT%\python.tmp\python312._pth"
move "%RT%\python.tmp" "%RT%\python" >nul || goto fail
:have_python

"%PY%" -m pip --version >nul 2>&1 && goto have_pip
echo [2/4] Installing pip ...
curl -L --fail --retry 3 -o "%RT%\get-pip.py" "https://bootstrap.pypa.io/get-pip.py" || goto fail
"%PY%" "%RT%\get-pip.py" --no-warn-script-location || goto fail
del "%RT%\get-pip.py"
:have_pip

"%PY%" -c "import PySide6.QtWidgets" >nul 2>&1 && goto have_qt
echo [3/4] Installing Qt ...
"%PY%" -m pip install --no-warn-script-location PySide6-Essentials || goto fail
:have_qt

if exist "%RT%\ffmpeg\bin\ffmpeg.exe" goto have_ffmpeg
echo [4/4] Downloading ffmpeg ...
curl -L --fail --retry 3 -o "%RT%\ffmpeg.zip" "%FFMPEG_URL%" || curl -L --fail --retry 3 -o "%RT%\ffmpeg.zip" "%FFMPEG_URL2%" || goto fail
if exist "%RT%\ffmpeg.tmp" rmdir /s /q "%RT%\ffmpeg.tmp"
mkdir "%RT%\ffmpeg.tmp"
tar -xf "%RT%\ffmpeg.zip" -C "%RT%\ffmpeg.tmp" --strip-components=1 || goto fail
del "%RT%\ffmpeg.zip"
if not exist "%RT%\ffmpeg.tmp\bin\ffmpeg.exe" goto fail
move "%RT%\ffmpeg.tmp" "%RT%\ffmpeg" >nul || goto fail
:have_ffmpeg

if "%~1"=="--setup-only" exit /b 0
start "" "%RT%\python\pythonw.exe" -m eazy_streamer
exit /b 0

:fail
echo.
echo Setup failed. Check the internet connection and start this file again.
if not "%~1"=="--setup-only" pause
exit /b 1
