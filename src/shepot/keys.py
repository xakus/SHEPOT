"""Горячие клавиши: общие имена для всех ОС и подписи для меню.

Имена клавиш — как в Linux evdev (KEY_RIGHTCTRL и т.п.): так они уже
хранятся в конфиге. Каждая платформа сама переводит свои события в эти
имена; всё, что не входит в список кандидатов, приходит как OTHER.
"""

import os

from .paths import IS_MAC, IS_WIN

OTHER = "OTHER"   # любая другая клавиша — нужна, чтобы отличить «одиночное» нажатие

# Клавиши, которые можно назначить на диктовку/чтение из меню. Только редкие
# и безопасные: буквы/цифры/Enter сюда не входят, чтобы нельзя было сломать
# набор текста. Какие из них реально есть — решает платформа.
KEY_CANDIDATES = [
    "KEY_RIGHTCTRL", "KEY_RIGHTALT", "KEY_RIGHTSHIFT", "KEY_RIGHTMETA",
    "KEY_COMPOSE", "KEY_PAUSE", "KEY_SCROLLLOCK", "KEY_CAPSLOCK", "KEY_INSERT",
]

# подписи в меню; у Mac свои названия модификаторов
_LABELS = {
    "KEY_RIGHTCTRL":  "Правый Ctrl",
    "KEY_RIGHTALT":   "Правый Option" if IS_MAC else "Правый Alt",
    "KEY_RIGHTSHIFT": "Правый Shift",
    "KEY_RIGHTMETA":  "Правый Cmd" if IS_MAC else ("Правый Win" if IS_WIN else "Правый Super (Win)"),
    "KEY_COMPOSE":    "Menu (клавиша меню)",
    "KEY_PAUSE":      "Pause / Break",
    "KEY_SCROLLLOCK": "Scroll Lock",
    "KEY_CAPSLOCK":   "Caps Lock",
    "KEY_INSERT":     "Insert",
}

# Клавиши по умолчанию. На Mac-клавиатурах правого Ctrl нет — диктовка на
# правом Option, чтение на правом Cmd. На Windows правый Alt при отпускании
# активирует меню окна — чтение на правом Shift.
if IS_MAC:
    _DEF_DICTATE, _DEF_READ = "KEY_RIGHTALT", "KEY_RIGHTMETA"
elif IS_WIN:
    _DEF_DICTATE, _DEF_READ = "KEY_RIGHTCTRL", "KEY_RIGHTSHIFT"
else:
    _DEF_DICTATE, _DEF_READ = "KEY_RIGHTCTRL", "KEY_RIGHTALT"

DEFAULT_DICTATE_KEY = os.environ.get("SHEPOT_KEY", _DEF_DICTATE)
DEFAULT_READ_KEY    = os.environ.get("SHEPOT_TTS_KEY", _DEF_READ)


def key_label(name):
    """Человекочитаемое имя клавиши для меню (иначе — имя без KEY_)."""
    if not name:
        return "—"
    return _LABELS.get(name, name.replace("KEY_", ""))


def labeled(names):
    """[(имя, подпись)] для построения меню."""
    return [(n, key_label(n)) for n in names]
