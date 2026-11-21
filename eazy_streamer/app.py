"""Вікно: сітка секцій, кожна секція — один стрім."""

from __future__ import annotations

import os
import shutil
import sys
import time

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QPainter
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from . import awake, config, devices, engine, ffmpeg_cmd, hwenc, netwatch, osdep, updater
from . import log as logs
from .log import log

CARD_MIN_W, CARD_MAX_W = 380, 620
REFRESH_MS = 3000

STYLE = """
QWidget { background: #16181d; color: #e6e8ec; font-size: 14px; }
QFrame#card { background: #1f232b; border: 1px solid #2e3440; border-radius: 10px; }
QFrame#card QLabel { background: transparent; }
QDoubleSpinBox { background: #12141a; border: 1px solid #3a4150; border-radius: 6px; padding: 6px 9px; }
QPushButton#preset { padding: 6px 4px; }
QPushButton#pencil { border: none; background: transparent; color: #8b93a3; font-size: 13px; padding: 0; }
QPushButton#pencil:hover { color: white; }
QPushButton#pencil:disabled { color: #4a5263; }
QLineEdit, QComboBox { background: #12141a; border: 1px solid #3a4150; border-radius: 6px;
    padding: 7px 9px; selection-background-color: #3b6fd4; }
QLineEdit:focus, QComboBox:focus { border-color: #5b8dee; }
QLineEdit:disabled, QComboBox:disabled { color: #7d8594; border-color: #2a2f3a; }
QLineEdit#name { font-size: 17px; font-weight: 600; border-color: transparent; background: transparent; }
QLineEdit#name:hover, QLineEdit#name:focus { border-color: #3a4150; background: #12141a; }
QComboBox QAbstractItemView { background: #12141a; border: 1px solid #3a4150;
    selection-background-color: #3b6fd4; }
QPushButton { background: #2a303b; border: 1px solid #3a4150; border-radius: 6px; padding: 7px 12px; }
QPushButton:hover { background: #343b48; }
QPushButton:disabled { color: #6b7280; }
QPushButton:checked { background: #3b6fd4; border-color: #5b8dee; color: white; font-weight: 600; }
QPushButton#add { background: #3b6fd4; border-color: #5b8dee; color: white; font-weight: 600; padding: 9px 18px; }
QPushButton#add:hover { background: #4a7fe6; }
QPushButton#update[fresh="true"] { background: #2e9d5b; border-color: #37b86b; color: white; font-weight: 600; }
QPushButton#update[fresh="true"]:hover { background: #37b86b; }
QPushButton#close { border: none; background: transparent; color: #7d8594; font-size: 18px; padding: 2px 8px; }
QPushButton#close:hover { color: #ff6b6b; }
QPushButton#go { font-size: 16px; font-weight: 700; padding: 11px; color: white; border: none; }
QPushButton#go[live="false"] { background: #2e9d5b; }
QPushButton#go[live="false"]:hover { background: #37b86b; }
QPushButton#go[live="true"] { background: #c94343; }
QPushButton#go[live="true"]:hover { background: #df5050; }
QLabel#hint { color: #8b93a3; font-size: 12px; }
QCheckBox { color: #b6bcc8; spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #4a5263; border-radius: 4px; background: #12141a; }
QCheckBox::indicator:checked { background: #3b6fd4; border-color: #5b8dee; }
QLabel#title { font-size: 18px; font-weight: 600; }
QScrollArea { border: none; }
"""

STATUS_COLORS = {"ok": "#4cd27e", "wait": "#e8b84a", "bad": "#ff7b6b", "calm": "#8b93a3"}


class SourceCombo(QComboBox):
    """Список джерел, який не реагує на коліщатко.

    Звичайний список міняє вибір, коли над ним крутять коліщатко. Людина гортає вікно,
    курсор проїжджає над списком — і джерело стріму тихо змінюється. Тому коліщатко
    віддаємо вікну, а вибір — лише клацанням.
    """

    def wheelEvent(self, event):
        event.ignore()


class PresetButton(QPushButton):
    """Кнопка якості: два рядки (бітрейт і розмір кадру) та олівець у кутку, щоб їх змінити."""

    def __init__(self, on_edit=None):
        super().__init__()
        self.setObjectName("preset")
        self.setCheckable(True)
        self.setMinimumHeight(48)
        self.pencil = None
        if on_edit is not None:
            self.pencil = QPushButton("✎", self)
            self.pencil.setObjectName("pencil")
            self.pencil.setFixedSize(20, 20)
            self.pencil.setCursor(Qt.CursorShape.PointingHandCursor)
            self.pencil.setToolTip("Змінити бітрейт і розмір кадру цієї кнопки")
            self.pencil.clicked.connect(on_edit)

    def show_quality(self, q):
        if q.kbit:
            self.setText("%s Мбіт/с\n%s" % (q.label, ffmpeg_cmd.height_name(q.height)))
            self.setToolTip("Бітрейт не вищий за %s Мбіт/с, кадр не більший за %s.\n"
                            "Спокійна картинка займає менше. Більше, ніж дає джерело, кадр не буває." % (
                                q.label, ffmpeg_cmd.height_name(q.height)))
        else:
            self.setText("Необмежено\nяк джерело")
            self.setToolTip("Картинка займає стільки, скільки їй треба (не більше %s Мбіт/с),\n"
                            "кадр — як дає джерело." % ffmpeg_cmd.mbit(ffmpeg_cmd.SAFETY_KBIT))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.pencil is not None:
            self.pencil.move(self.width() - 21, 1)


class PresetDialog(QDialog):
    """Олівець на кнопці якості: розмір кадру зі списку, бітрейт руками."""

    def __init__(self, parent, q):
        super().__init__(parent)
        self.setWindowTitle("Якість")
        self.height_box = QComboBox()
        for h in ffmpeg_cmd.HEIGHTS:
            self.height_box.addItem(ffmpeg_cmd.height_name(h), h)
        self.height_box.setCurrentIndex(max(0, self.height_box.findData(q.height)))
        self.rate_box = QDoubleSpinBox()
        self.rate_box.setDecimals(1)
        self.rate_box.setSingleStep(0.5)
        self.rate_box.setRange(ffmpeg_cmd.MIN_KBIT / 1000, ffmpeg_cmd.MAX_KBIT / 1000)
        self.rate_box.setValue(q.kbit / 1000)
        self.rate_box.setSuffix(" Мбіт/с")
        form = QFormLayout()
        form.addRow("Кадр не більший за", self.height_box)
        form.addRow("Бітрейт не вищий за", self.rate_box)
        save, cancel = QPushButton("Зберегти"), QPushButton("Скасувати")
        save.setObjectName("add")
        save.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 14)
        box.setSpacing(14)
        box.addLayout(form)
        box.addLayout(buttons)

    def result_numbers(self) -> tuple[int, int]:
        return int(self.height_box.currentData()), int(round(self.rate_box.value() * 1000))


class Preview(QWidget):
    """Картинка 16:9. Малює лише готовий кадр — жодної обробки тут немає."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QImage | None = None
        self._text = ""
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumSize(240, 135)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return width * 9 // 16

    def sizeHint(self) -> QSize:
        return QSize(400, 225)

    def set_image(self, image: QImage):
        self._image = image
        self.update()

    def set_text(self, text: str):
        """Прибрати кадр і показати напис замість нього."""
        self._image = None
        self._text = text
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0c0f"))
        if self._image is None:
            painter.setPen(QColor("#6b7280"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
            return
        size = self._image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                       size.width(), size.height())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self._image)


class Card(QFrame):
    def __init__(self, window: "Window", data: dict):
        super().__init__()
        self.setObjectName("card")
        self.setMaximumWidth(CARD_MAX_W)
        self.window_ = window
        self.live = False
        self.source_key = str(data.get("source") or "")
        self.source_name = str(data.get("source_name") or "")
        self._devices: list[devices.Device] = []
        self._note = ""             # разове повідомлення під кнопкою
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(6000)
        self._note_timer.timeout.connect(self._clear_note)
        self.engine = engine.Engine(self)

        self.name_edit = QLineEdit(str(data.get("name") or ""))
        self.name_edit.setObjectName("name")
        self.name_edit.setPlaceholderText("Назва стріму")
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("close")
        self.close_btn.setToolTip("Прибрати цю секцію")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.preview = Preview()

        self.source_combo = SourceCombo()
        self.source_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.source_combo.setToolTip("Карта відеозахоплення для цього стріму")

        self.url_edit = QLineEdit(str(data.get("url") or ""))
        self.url_edit.setPlaceholderText("rtmps://сервер/live/ключ")
        self.url_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.url_edit.setToolTip("Посилання на стрім. Приховане, бо містить ключ")
        self.eye_btn = QPushButton("👁")
        self.eye_btn.setCheckable(True)
        self.eye_btn.setToolTip("Показати або сховати посилання")
        self.eye_btn.setFixedWidth(44)

        self.quality_group = QButtonGroup(self)
        self.quality_buttons: dict[str, PresetButton] = {}
        saved_quality = ffmpeg_cmd.quality(str(data.get("quality") or "")).key
        # Числа на кнопках у кожної секції свої: їх міняють олівцем і зберігають разом із секцією
        saved_presets = data.get("presets") if isinstance(data.get("presets"), dict) else {}
        self.presets: dict[str, ffmpeg_cmd.Quality] = {}
        quality_row = QHBoxLayout()
        quality_row.setSpacing(6)
        for q in ffmpeg_cmd.QUALITIES:
            own = saved_presets.get(q.key)
            if q.kbit and isinstance(own, list) and len(own) == 2:
                try:
                    q = ffmpeg_cmd.make_quality(q.key, int(own[0]), int(own[1]))
                except (TypeError, ValueError):
                    pass
            self.presets[q.key] = q
            btn = PresetButton((lambda _checked=False, key=q.key: self._edit_preset(key)) if q.kbit else None)
            btn.show_quality(q)
            btn.setChecked(q.key == saved_quality)
            self.quality_group.addButton(btn)
            self.quality_buttons[q.key] = btn
            quality_row.addWidget(btn, 1)

        self.go_btn = QPushButton()
        self.go_btn.setObjectName("go")
        self.go_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(78)        # чотири рядки: щоб секція не стрибала, коли піде ефір
        self.status.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        head = QHBoxLayout()
        head.addWidget(self.name_edit, 1)
        head.addWidget(self.close_btn)
        url_row = QHBoxLayout()
        url_row.setSpacing(6)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.eye_btn)

        box = QVBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(8)
        box.addLayout(head)
        box.addWidget(self.preview)
        box.addWidget(self._hint("Джерело"))
        box.addWidget(self.source_combo)
        box.addWidget(self._hint("Посилання на стрім"))
        box.addLayout(url_row)
        box.addWidget(self._hint("Якість"))
        box.addLayout(quality_row)
        box.addSpacing(4)
        box.addWidget(self.go_btn)
        box.addWidget(self.status)

        self.engine.label = self.title()
        self.name_edit.textEdited.connect(lambda _t: setattr(self.engine, "label", self.title()))
        self.name_edit.textEdited.connect(window.save_soon)
        self.url_edit.textEdited.connect(self._on_url_edited)
        self.eye_btn.toggled.connect(self._on_eye)
        self.source_combo.activated.connect(self._on_source_picked)
        self.quality_group.buttonClicked.connect(lambda _b: window.save_soon())
        self.go_btn.clicked.connect(self._on_go)
        self.close_btn.clicked.connect(lambda: window.remove_card(self))
        self.engine.frame.connect(self.preview.set_image)
        self.engine.changed.connect(self._on_engine)
        self._refresh_view()

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hint")
        return label

    # --- дані ----------------------------------------------------------

    def title(self) -> str:
        return self.name_edit.text().strip() or "без назви"

    def quality_key(self) -> str:
        for key, btn in self.quality_buttons.items():
            if btn.isChecked():
                return key
        return ffmpeg_cmd.DEFAULT_QUALITY

    def data(self) -> dict:
        return {"name": self.name_edit.text().strip(), "url": self.url_edit.text().strip(),
                "source": self.source_key, "source_name": self.source_name,
                "quality": self.quality_key(),
                "presets": {key: [q.height, q.kbit] for key, q in self.presets.items() if q.kbit}}

    def current_quality(self):
        return self.presets[self.quality_key()]

    def _edit_preset(self, key: str):
        dialog = PresetDialog(self, self.presets[key])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        height, kbit = dialog.result_numbers()
        self.presets[key] = ffmpeg_cmd.make_quality(key, height, kbit)
        self.quality_buttons[key].show_quality(self.presets[key])
        log.info("[%s] кнопку якості змінено: %s Мбіт/с, %s", self.title(), self.presets[key].label,
                 ffmpeg_cmd.height_name(self.presets[key].height))
        self.window_.save_soon()
        self._refresh_view()

    def device(self) -> devices.Device | None:
        if self.source_key in devices.TESTS:
            return devices.Device(self.source_key, "", devices.TESTS[self.source_key])
        for dev in self._devices:
            if dev.key == self.source_key:
                return dev
        return None

    # --- джерела -------------------------------------------------------

    def set_devices(self, found: list[devices.Device]):
        self._devices = found
        dev = self.device()
        if dev is not None:
            self.source_name = dev.name     # номер videoN міг змінитись після перезавантаження
        combo = self.source_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("— оберіть джерело —", "")
        for dev in found:
            combo.addItem(dev.name, dev.key)
        for key, name in devices.TESTS.items():
            combo.addItem(name, key)
        index = combo.findData(self.source_key)
        if index < 0:
            # Обрана карта зараз не підключена — лишаємо її у списку, щоб не загубити вибір
            combo.addItem("%s — не підключено" % (self.source_name or "Карта"), self.source_key)
            index = combo.count() - 1
        combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self.sync()

    def _on_source_picked(self, index: int):
        key = self.source_combo.itemData(index) or ""
        owner = self.window_.owner_of(key, self)
        if owner is not None:
            log.info("[%s] вибір джерела відхилено: «%s» зайняте секцією «%s»", self.title(),
                     self.source_combo.itemText(index), owner.title())
            self._say("Це джерело вже зайняте секцією «%s»" % owner.title())
            self.source_combo.setCurrentIndex(self.source_combo.findData(self.source_key))
            return
        self._clear_note()
        self.source_key = key
        dev = self.device()
        self.source_name = dev.name if dev else ""
        log.info("[%s] обрано джерело: %s", self.title(), self.source_name or "жодного")
        self.window_.save_soon()
        self.sync()

    # --- керування -----------------------------------------------------

    def sync(self):
        """Привести ffmpeg у відповідність до того, що обрано в секції."""
        dev = self.device()
        if self.live:
            self.engine.show(dev, self.url_edit.text(), self.current_quality())
        else:
            self.engine.show(dev, None, ffmpeg_cmd.quality("low"))
        self._refresh_view()

    def _on_go(self):
        if self.live:
            log.info("[%s] КНОПКА: зупинити", self.title())
            self.live = False
            self._note = ""
            self.sync()
            return
        if not self.source_key:
            self._say("Спершу оберіть джерело")
        elif not ffmpeg_cmd.is_stream_url(self.url_edit.text()):
            self._say("Вставте посилання на стрім — воно починається з rtmp:// або rtmps://")
            self.url_edit.setFocus()
        else:
            self._clear_note()
            self.live = True
            log.info("[%s] КНОПКА: почати стрім, якість %s, джерело «%s», куди %s", self.title(),
                     self.quality_key(), self.source_name, logs.mask(self.url_edit.text().strip()))
        if self._note:
            log.info("[%s] старт відхилено: %s", self.title(), self._note)
        self.sync()

    def _say(self, text: str):
        """Разове повідомлення під кнопкою: показати і за кілька секунд прибрати."""
        self._note = text
        self._note_timer.start()
        self._refresh_view()

    def _clear_note(self):
        self._note_timer.stop()
        if self._note:
            self._note = ""
            self._refresh_view()

    def _on_url_edited(self, _text: str):
        self._note = ""
        self.window_.save_soon()
        self._refresh_view()

    def _on_eye(self, shown: bool):
        self.url_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)

    def _on_engine(self, state: str, text: str):
        if state == engine.IDLE and text and self.live:
            self.live = False       # ffmpeg не запустився взагалі
            self._note = text       # це не разове: без ffmpeg стрім не піде, хай висить
        self._refresh_view()

    def stop(self):
        self.live = False
        self.engine.request_stop()
        self.engine.wait_stopped()

    # --- вигляд --------------------------------------------------------

    def _refresh_view(self):
        live = self.live
        self.source_combo.setEnabled(not live)
        self.url_edit.setEnabled(not live)
        for btn in self.quality_buttons.values():
            btn.setEnabled(not live)
        self.go_btn.setText("■  Зупинити" if live else "▶  Почати стрім")
        if self.go_btn.property("live") != ("true" if live else "false"):
            self.go_btn.setProperty("live", "true" if live else "false")
            self.go_btn.style().unpolish(self.go_btn)
            self.go_btn.style().polish(self.go_btn)

        state, text = self.engine.state, ""
        tone = "calm"
        dev_now = self.device()
        have_device = dev_now is not None
        if self._note:
            text, tone = self._note, "bad"
        elif not self.source_key:
            text = "Оберіть джерело"
        elif not have_device:
            text = "Карту не підключено — почну, щойно з'явиться" if live else "Карту не підключено"
            tone = "wait" if live else "calm"
        elif state == engine.LIVE:
            text, tone = "● В ефірі  %s" % _clock(time.monotonic() - self.engine.live_since), "ok"
            q = self.current_quality()
            eng = self.engine
            came = ffmpeg_cmd.source_size(dev_now, q.height)
            goes = ffmpeg_cmd.out_size(dev_now, q)
            fps = lambda value: " %.0f к/с" % value if value is not None else ""
            text += "\nвхід: %s%s" % ("%d×%d" % came if came else "?", fps(eng.in_fps))
            text += "\nвихід: %s%s" % ("%d×%d" % goes if goes else "?", fps(eng.out_fps))
            parts = []
            stats, net = eng.stats, eng.net
            if eng.kbit_now is not None:
                now = ("%.1f" % (eng.kbit_now / 1000)).replace(".", ",")
                parts.append("%s/%s Мбіт/с" % (now, q.label) if q.kbit else "%s Мбіт/с" % now)
                # «ріже» кажемо лише там, де кодер тримає якість: тоді бітрейт під стелею означає,
                # що картинка хотіла б більше. У режимі рівного потоку він під стелею завжди.
                if q.kbit and eng.rate_mode == "quality" and eng.kbit_now >= q.kbit * 0.93:
                    parts.append("ріже")
            if "loss" in net:
                parts.append("втрати %.1f %%" % net["loss"])
            lagging = eng.pace is not None and eng.pace < 0.9 and time.monotonic() - eng.live_since > 10
            if eng.stalled:
                parts.append("кадри не йдуть")
                tone = "wait"
            elif net.get("congested"):
                # Linux: ядро саме каже, що дані стоять у черзі на відправку. Скільки проходить,
                # не пишемо — це й так перше число в рядку
                parts.append("сервер не вивозе" if net.get("server_slow") else "інтернет не вивозе")
                tone = "wait"
            elif lagging and eng.cpu_pct is not None and eng.cpu_pct < 80 and "queued" not in net:
                # Windows: ядро не спитати. Ефір відстає, а ffmpeg майже не працює — значить, чекає
                # мережу. Це здогад, тому «походу»
                parts.append("походу інтернет не вивозе")
                tone = "wait"
            elif lagging:
                # Процесор ffmpeg забитий — упевнено. Ні — інтернет ядро вже виключило, лишається
                # комп'ютер (відеокарта, сама карта захоплення), але це здогад
                parts.append("комп'ютер не вивозе" if (eng.cpu_pct or 0) >= 80 else "походу комп'ютер не вивозе")
                tone = "wait"
            if net.get("loss", 0) > 2:
                parts.append("інтернет губить")
                tone = "wait"
            text += "\n" + "  ·  ".join(parts)
        elif state == engine.STARTING:
            text, tone = ("Підключення до сервера…", "wait") if live else ("Відкриваю джерело…", "calm")
            if self.engine.note:
                text, tone = self.engine.note, "wait"
        elif state == engine.RETRY:
            text, tone = self.engine.retry_text(), ("bad" if live else "calm")
        elif state == engine.PREVIEW:
            text = "Готово до ефіру"
        self.status.setText(text)
        self.status.setStyleSheet("color: %s;" % STATUS_COLORS[tone])

        if state not in (engine.LIVE, engine.PREVIEW) or not have_device:
            if not self.source_key:
                self.preview.set_text("Оберіть джерело")
            elif not have_device:
                self.preview.set_text("Карту не підключено")
            elif state == engine.RETRY:
                self.preview.set_text("Немає сигналу")
            else:
                self.preview.set_text("…")


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Eazy Streamer")
        self.resize(1280, 800)
        self.cards: list[Card] = []
        self._device_keys: list[str] | None = None
        self._devices: list[devices.Device] = []
        self._cols = 0

        title = QLabel("Версія %s" % updater.local_version())
        title.setObjectName("title")
        self.update_btn = QPushButton("Перевірити оновлення")
        self.update_btn.setObjectName("update")
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.setVisible(updater.can_update())
        self.update_btn.clicked.connect(self._on_update_clicked)
        self.update_note = QLabel()
        self.update_note.setObjectName("hint")
        self.encoder_note = QLabel()
        self.encoder_note.setObjectName("hint")
        self._update_ready = False
        self._asked = False         # чи людина сама натиснула «перевірити»
        self._applying = False
        add_btn = QPushButton("+  Додати стрім")
        add_btn.setObjectName("add")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(lambda: self.add_card({}))
        top = QHBoxLayout()
        top.setContentsMargins(16, 12, 16, 4)
        top.setSpacing(12)
        top.addWidget(title)
        top.addWidget(self.update_btn)
        top.addWidget(self.update_note)
        top.addStretch(1)
        self.awake = awake.KeepAwake(self)
        self.awake_box = QCheckBox("Тримати екран увімкненим")
        self.awake_box.setToolTip("Поки це вікно відкрите, екран не гасне і комп'ютер не засинає від бездіяльності")
        self.awake_box.setChecked(bool(config.load_settings().get("keep_awake", True)))
        self.awake_box.toggled.connect(self._on_awake_toggled)
        top.addWidget(self.awake_box)
        top.addWidget(self.encoder_note)
        top.addWidget(add_btn)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(16, 8, 16, 16)
        self.grid.setSpacing(14)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.grid_host)

        central = QWidget()
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(top)
        column.addWidget(self.scroll, 1)
        self.setCentralWidget(central)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self.save_now)

        # Для яких карт GPU вже відмовляв: щоб не наступати на ті самі граблі при кожному запуску
        ffmpeg_cmd.recall_caps(config.load_settings().get("level_cap"))
        self._caps_saved = dict(ffmpeg_cmd.LEVEL_CAP)
        for key, level in ffmpeg_cmd.LEVEL_CAP.items():
            log.info("Пам'ятаю: для джерела %s обробка спрощена до «%s»", key, ffmpeg_cmd.LEVEL_NAMES[level])
        for item in config.load() or [{}]:
            self.add_card(item, save=False)
        self.awake.set(self.awake_box.isChecked())

        self.updater = updater.Updater(self)
        self.updater.checking.connect(self._on_update_checking)
        self.updater.up_to_date.connect(self._on_up_to_date)
        self.updater.available.connect(self._on_update_available)
        self.updater.failed.connect(self._on_update_failed)
        self.updater.updated.connect(self._on_updated)
        self._daily = QTimer(self)
        self._daily.setInterval(24 * 60 * 60 * 1000)
        self._daily.timeout.connect(self.updater.check)
        self._daily.start()
        QTimer.singleShot(1500, self.updater.check)

        self.hw_probe = hwenc.Probe(self)
        self.hw_probe.done.connect(self._show_encoder)
        self.hw_probe.start()

        # Втрати в мережі читаємо утилітою ss — вона є лише на Linux
        self.netwatch = netwatch.NetWatch(lambda: [c.engine for c in self.cards], self) if osdep.IS_LINUX else None

        self._load_timer = QTimer(self)
        self._load_timer.setInterval(60000)
        self._load_timer.timeout.connect(self._log_load)
        self._load_timer.start()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self.refresh_devices)
        self._refresh_timer.start()
        self.refresh_devices()
        self.device_watcher = None
        if osdep.IS_WIN:
            # На Windows карти шукає ffmpeg, це довго — робимо у фоні, а не в таймері вікна
            from . import devices_win
            self.device_watcher = devices_win.Watcher(self)
            self.device_watcher.found.connect(self._apply_devices)
            self.device_watcher.start()

    # --- секції --------------------------------------------------------

    def add_card(self, data: dict, save: bool = True):
        if not data.get("name"):
            data = dict(data, name="Стрім %d" % (len(self.cards) + 1))
        card = Card(self, data)
        self.cards.append(card)
        card.set_devices(self._devices)
        self._layout_cards(force=True)
        if save:
            self.save_soon()

    def remove_card(self, card: Card):
        question = "Прибрати секцію «%s»?" % card.title()
        if card.live:
            question = "Секція «%s» зараз в ефірі. Зупинити стрім і прибрати її?" % card.title()
        if not _ask(self, question):
            return
        card.stop()
        self.cards.remove(card)
        self.grid.removeWidget(card)
        card.deleteLater()
        self._layout_cards(force=True)
        self.save_soon()

    def owner_of(self, key: str, asking: Card) -> Card | None:
        """Одна карта — один стрім: хто вже тримає це джерело."""
        if not key or key in devices.TESTS:
            return None
        for card in self.cards:
            if card is not asking and card.source_key == key:
                return card
        return None

    def _layout_cards(self, force: bool = False):
        width = self.scroll.viewport().width() - 32
        cols = max(1, (width + 14) // (CARD_MIN_W + 14))
        if cols == self._cols and not force:
            return
        self._cols = cols
        for card in self.cards:
            self.grid.removeWidget(card)
        for i, card in enumerate(self.cards):
            card.setMinimumWidth(min(CARD_MIN_W, max(260, width)))
            self.grid.addWidget(card, i // cols, i % cols)

    if osdep.IS_WIN:
        def nativeEvent(self, event_type, message):
            # Windows розсилає WM_DEVICECHANGE усім вікнам, коли щось підключили чи від'єднали.
            # Це дешевше, ніж постійно питати ffmpeg про список карт.
            try:
                if self.device_watcher is not None and bytes(event_type) == b"windows_generic_MSG":
                    import ctypes
                    from ctypes import wintypes
                    if wintypes.MSG.from_address(int(message)).message == 0x0219:
                        self.device_watcher.devices_changed()
            except Exception:
                pass
            return super().nativeEvent(event_type, message)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_cards()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, lambda: self._layout_cards(force=True))

    # --- джерела і налаштування ----------------------------------------

    def _on_awake_toggled(self, on: bool):
        self.awake.set(on)
        self.save_soon()

    def _log_load(self):
        live = [c.title() for c in self.cards if c.live]
        load = logs.load_line()         # рахуємо завжди: наступний замір порівнюється з цим
        if ffmpeg_cmd.LEVEL_CAP != self._caps_saved:
            self.save_soon()            # GPU щойно відмовив на якійсь карті — запам'ятати
        if live:
            log.info("Машина: %s | в ефірі %d: %s", load, len(live), ", ".join(live))

    def _show_encoder(self):
        self.encoder_note.setText("Кодує " + hwenc.encoder_name())
        self.encoder_note.setToolTip(
            "Відео стискає GPU — CPU майже вільний" if ffmpeg_cmd.HW_NODE else
            "GPU не вміє стискати відео або немає драйвера — працює CPU")

    # --- оновлення -----------------------------------------------------

    def _set_update_button(self, text: str, fresh: bool, enabled: bool = True):
        self._update_ready = fresh
        self.update_btn.setText(text)
        self.update_btn.setEnabled(enabled)
        self.update_btn.setProperty("fresh", "true" if fresh else "false")
        self.update_btn.style().unpolish(self.update_btn)
        self.update_btn.style().polish(self.update_btn)

    def _on_update_clicked(self):
        if not self._update_ready:
            self._asked = True
            self.updater.check()
            return
        live = [c for c in self.cards if c.live]
        question = "Встановити оновлення? Програма перезапуститься."
        if live:
            question = "В ефірі стрімів: %d. Оновлення їх зупинить і перезапустить програму. Продовжити?" % len(live)
        if _ask(self, question):
            self._applying = True
            self.updater.apply()

    def _on_update_checking(self):
        self.update_btn.setEnabled(False)
        self.update_note.setText("Оновлюю…" if self._applying else "Перевіряю…")

    def _on_up_to_date(self):
        self._set_update_button("Перевірити оновлення", fresh=False)
        self.update_note.setText("Оновлень немає" if self._asked else "")
        self._asked = False

    def _on_update_available(self, version: str):
        self._set_update_button("Оновити до %s" % version if version else "Оновити", fresh=True)
        self.update_note.setText("Є нова версія")
        self._asked = False

    def _on_update_failed(self, text: str):
        self._set_update_button(self.update_btn.text(), fresh=self._update_ready)
        # Фонова перевірка мовчить: немає інтернету — не привід лякати людину
        self.update_note.setText(text if (self._asked or self._applying) else "")
        self._asked = self._applying = False

    def _on_updated(self):
        self._shutdown()
        osdep.restart()

    def _shutdown(self):
        log.info("Закриття програми")
        if self.device_watcher is not None:
            self.device_watcher.stop()
        self.awake.set(False)
        self._refresh_timer.stop()
        self._save_timer.stop()
        self.save_now()
        # Спершу просимо всіх зупинитись, потім чекаємо — так стріми закриваються разом, а не по черзі
        for card in self.cards:
            card.live = False
            card.engine.request_stop()
        for card in self.cards:
            card.engine.wait_stopped()

    def refresh_devices(self):
        self._show_encoder()
        if not osdep.IS_WIN:
            self._apply_devices(devices.list_devices())

    def _apply_devices(self, found: list):
        keys = [d.key + "|" + d.name for d in found]
        if keys == self._device_keys:
            return
        self._device_keys = keys
        self._devices = found
        log.info("Джерел знайдено: %d", len(found))
        for dev in found:
            log.info("  %s -> %s | %s", dev.name, dev.path, dev.bus)
            for fmt, sizes in dev.formats.items():
                log.info("      %s: %s", fmt, "; ".join(
                    "%dx%d @ %s" % (s + ("/".join("%g" % r for r in dev.rates.get((fmt,) + s, [])) or "?",))
                    for s in sizes) or "будь-який розмір")
        for card in self.cards:
            card.set_devices(found)

    def save_soon(self):
        self._save_timer.start()

    def save_now(self):
        try:
            config.save([card.data() for card in self.cards],
                        {"keep_awake": self.awake_box.isChecked(),
                         "level_cap": ffmpeg_cmd.remembered_caps()})
            self._caps_saved = dict(ffmpeg_cmd.LEVEL_CAP)
        except OSError as e:
            self.statusBar().showMessage("Не вдалося зберегти налаштування: %s" % e, 8000)

    def closeEvent(self, event):
        live = [c for c in self.cards if c.live]
        if live and not _ask(self, "В ефірі стрімів: %d. Зупинити їх і закрити програму?" % len(live)):
            event.ignore()
            return
        self._shutdown()
        event.accept()


def _ask(parent: QWidget, question: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle("Eazy Streamer")
    box.setText(question)
    yes = box.addButton("Так", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Ні", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is yes


def main() -> int:
    logs.setup()
    logs.system_summary()
    log.info("Eazy Streamer %s, папка %s", updater.local_version(), updater.ROOT)
    if osdep.IS_WIN:
        # Без власного «імені застосунку» Windows показує на панелі завдань іконку Python,
        # бо EazyStreamer.exe — це він і є
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Kargevon.EazyStreamer")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")))
    app.setApplicationName("Eazy Streamer")
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    if not osdep.has_ffmpeg():
        QMessageBox.warning(window, "Eazy Streamer",
                            "Не знайдено ffmpeg — без нього стрім не піде.\n" + (
                                "Запустіть програму через eazy-streamer.bat" if osdep.IS_WIN
                                else "Встановіть: sudo apt install ffmpeg"))
    return app.exec()
