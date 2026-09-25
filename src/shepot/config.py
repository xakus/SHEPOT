"""Конфиг пользователя: один JSON-файл на диктовку и чтение.

Словарь конфига — один общий объект на всю программу (трей, менеджер
моделей, чтение). Сохранять его могут разные потоки, поэтому запись
идёт под блокировкой и через временный файл: оборванная запись не
испортит конфиг.
"""

import json
import os
import threading

from .paths import CONFIG_DIR, CONFIG_PATH

_lock = threading.Lock()   # одна запись конфига за раз


def load_config():
    """Прочитать конфиг; нет файла или он битый — пустой словарь."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    """Записать конфиг атомарно (tmp + replace). Ошибки диска не роняют программу."""
    with _lock:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except OSError:
            pass
