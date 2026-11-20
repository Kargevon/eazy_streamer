"""Запуск Eazy Streamer на Windows без власного exe-файла.

EazyStreamer.exe у теці програми — це pythonw.exe з офіційної збірки Python, лише під іншим
іменем. Він підписаний Python Software Foundation, тому Windows (зокрема Smart App Control)
його пускає; власний непідписаний запускач вона блокувала.

Python при старті сам імпортує модуль sitecustomize. Якщо нас запустили під іменем
EazyStreamer.exe — відкриваємо програму. Під будь-яким іншим іменем цей файл нічого не робить.
"""

import os
import sys

if os.path.basename(sys.executable).lower() == "eazystreamer.exe":
    code = 1
    try:
        from eazy_streamer.app import main

        code = main()
    except BaseException as error:          # вікна консолі немає — помилку показуємо віконцем
        if not isinstance(error, SystemExit):
            import ctypes
            import traceback

            ctypes.windll.user32.MessageBoxW(
                None, "Програма не запустилась.\n\n" + traceback.format_exc()[-1500:], "Eazy Streamer", 0x10)
        elif isinstance(error.code, int):
            code = error.code
    import logging

    logging.shutdown()
    os._exit(code or 0)
