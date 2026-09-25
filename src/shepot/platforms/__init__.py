"""Платформенный слой: всё, что зависит от ОС.

Каждый модуль платформы даёт одинаковый набор функций:

    create_tray(config, has_reader) -> TrayBase   иконка в трее с меню
    make_typer()                    -> fn(text)   вставка текста в позицию курсора
    make_sources(mode, own_loop)    -> [источник] откуда брать текст для чтения
    start_hotkeys(tray, on_key)                   слушать клавиатуру в фоне,
                                                  on_key(имя, значение) — в Dictation

Linux — linux.py (evdev + GTK/AppIndicator), Windows и macOS — desktop.py
(pynput + pystray).
"""

import importlib
import os

from ..paths import IS_LINUX


def current():
    """Модуль платформы для текущей ОС (импорт ленивый: у каждой ОС свои зависимости).

    SHEPOT_PLATFORM=desktop|linux — принудительно (для отладки трея
    Windows/macOS на Linux).
    """
    name = os.environ.get("SHEPOT_PLATFORM") or ("linux" if IS_LINUX else "desktop")
    return importlib.import_module(f"{__name__}.{name}")
