"""Общая логика иконки в трее — одинакова для всех ОС.

Здесь хранится состояние, которое читают диктовка и чтение (включено ли,
какие клавиши, какое устройство), и правила его изменения (сохранить в
конфиг, не дать назначить одну клавишу на две функции). Отрисовку меню
делают наследники: GtkTray (Linux) и PystrayTray (Windows, macOS).

Правило потоков: методы set_* с данными меню вызываются только в потоке UI
через call_soon(); set_status / set_last / icon / set_reading можно звать
из любого потока — наследник сам переносит их в поток UI.
"""

import threading

from .config import save_config
from .keys import DEFAULT_DICTATE_KEY, DEFAULT_READ_KEY, key_label
from .settings import DEVICE, DEVICE_LABELS

# состояния иконки; каждая платформа рисует их по-своему
ICON_STATES = ("load", "idle", "rec", "off", "read")


class TrayBase:
    """Состояние и правила трея. Наследник реализует отрисовку (см. ниже)."""

    def __init__(self, config, has_reader):
        self.config = config
        self.has_reader = has_reader   # есть ли модуль чтения вслух
        self.enabled = True            # «Слушать»: диктовка включена
        self.reader = None             # Reader; ставится в app.main()
        self.jobs = None               # очередь транскрайбера; ставится Dictation
        self.tts_enabled = bool(config.get("tts_enabled", True))
        self.available_keys = []       # [(имя, подпись)] — уточняет платформа

        # устройство распознавания: конфиг → env → auto
        self.device_pref = config.get("device") or DEVICE
        if self.device_pref not in DEVICE_LABELS:
            self.device_pref = "auto"

        # горячие клавиши: конфиг → env → умолчание для ОС. Диктовка читает
        # эти поля на каждом событии, поэтому смена применяется сразу.
        self.dictate_key_name = config.get("hotkey") or DEFAULT_DICTATE_KEY
        self.read_key_name = (config.get("tts_key") or DEFAULT_READ_KEY) if has_reader else None

    # -- то, что реализует платформа --

    def call_soon(self, fn, *args):
        """Выполнить fn(*args) в потоке UI."""
        raise NotImplementedError

    def set_status(self, text):
        raise NotImplementedError

    def set_last(self, text):
        raise NotImplementedError

    def icon(self, state):
        """Сменить иконку: одно из ICON_STATES."""
        raise NotImplementedError

    def set_model_catalog(self, entries, current, manager):
        raise NotImplementedError

    def set_device_menu(self, pref, actual, has_gpu):
        raise NotImplementedError

    def set_voice_catalog(self, entries, current, rate, engine_kind):
        raise NotImplementedError

    def rebuild_key_menus(self):
        raise NotImplementedError

    def set_reading(self, on):
        raise NotImplementedError

    def run(self, on_ready):
        """Главный цикл UI (блокирует). on_ready() зовётся, когда трей показан."""
        raise NotImplementedError

    def quit(self):
        raise NotImplementedError

    # -- общая логика --

    @staticmethod
    def short_last(text):
        """Последняя фраза для меню: не длиннее 60 символов."""
        t = (text[:60] + "…") if len(text) > 60 else text
        return t or "—"

    def idle_icon(self):
        """Иконка покоя с учётом чекбокса «Слушать»."""
        return "idle" if self.enabled else "off"

    def set_key_catalog(self, available):
        """Платформа сообщила, какие клавиши реально есть; перестроить меню."""
        if available:
            self.available_keys = available
        self.rebuild_key_menus()
        return False   # для GLib.idle_add — не повторять

    def set_enabled(self, on):
        """Чекбокс «Слушать»."""
        self.enabled = on
        self.icon(self.idle_icon())
        self.set_status("Готов" if on else "Выключено")

    def set_tts_enabled(self, on):
        """Чекбокс «Читать»: сохранить и остановить текущее чтение при выключении."""
        self.tts_enabled = on
        self.config["tts_enabled"] = on
        save_config(self.config)
        if self.reader and not on:
            threading.Thread(target=self.reader.stop, daemon=True).start()

    def pick_device(self, pref):
        """Пункт меню «Устройство»: перезагрузку модели делает транскрайбер."""
        if self.jobs:
            self.jobs.put(("device", pref))

    def set_dictate_key(self, name):
        """Назначить клавишу диктовки (диктовка подхватит сразу)."""
        if name == self.read_key_name:
            self.set_status(f"{key_label(name)} уже занята чтением")
            self.rebuild_key_menus()       # вернуть отметку на прежнюю
            return
        self.dictate_key_name = name
        self.config["hotkey"] = name
        save_config(self.config)
        self.rebuild_key_menus()
        self.set_status(f"Клавиша диктовки: {key_label(name)}")

    def set_read_key(self, name):
        """Назначить клавишу чтения (диктовка подхватит сразу)."""
        if name == self.dictate_key_name:
            self.set_status(f"{key_label(name)} уже занята диктовкой")
            self.rebuild_key_menus()
            return
        self.read_key_name = name
        self.config["tts_key"] = name
        save_config(self.config)
        self.rebuild_key_menus()
        self.set_status(f"Клавиша чтения: {key_label(name)}")

    @staticmethod
    def voice_short(current):
        """«ru_RU-irina-medium» → «irina» для подписи меню."""
        parts = current.split("-")
        return parts[1] if len(parts) == 3 else current

    @staticmethod
    def device_title(pref, actual):
        """Подпись «Устройство: …»; в режиме «Авто» или при откате — где реально."""
        tail = ""
        if actual and (pref == "auto" or actual != pref):
            tail = f" → {'GPU' if actual == 'cuda' else 'CPU'}"
        return f"Устройство: {DEVICE_LABELS[pref]}{tail}"
