"""Пути к конфигу, данным, логам и кешу моделей — свои для каждой ОС.

Linux — прежние пути (~/.config/shepot, ~/.local/share/shepot, ~/shepot-log.txt),
чтобы существующая установка подхватила свои настройки и голоса.
Windows — %APPDATA%\\SHEPOT, macOS — ~/Library/Application Support/SHEPOT.
"""

import os
import subprocess
import sys

IS_WIN   = sys.platform == "win32"
IS_MAC   = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _base_dirs():
    """(папка конфига, папка данных) для текущей ОС."""
    if IS_WIN:
        root = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        d = os.path.join(root, "SHEPOT")
        return d, d
    if IS_MAC:
        d = os.path.expanduser("~/Library/Application Support/SHEPOT")
        return d, d
    return os.path.expanduser("~/.config/shepot"), os.path.expanduser("~/.local/share/shepot")


CONFIG_DIR, DATA_DIR = _base_dirs()
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")   # общий для диктовки и чтения

# голоса Piper (.onnx + .onnx.json)
VOICES_DIR = os.path.expanduser(
    os.environ.get("SHEPOT_TTS_VOICES_DIR", os.path.join(DATA_DIR, "voices")))

# лог распознанного текста — страховка от потери длинной диктовки
LOG_PATH = os.path.expanduser(os.environ.get(
    "SHEPOT_LOG", "~/shepot-log.txt" if IS_LINUX else os.path.join(DATA_DIR, "shepot-log.txt")))

# консольный вывод собранной программы без консоли (Windows/macOS)
CONSOLE_LOG_PATH = os.path.join(DATA_DIR, "shepot-console.log")


def _hf_cache():
    """Кеш HuggingFace, куда faster-whisper качает модели (учитывает HF_HOME и т.п.)."""
    try:
        from huggingface_hub import constants
        return constants.HF_HUB_CACHE
    except Exception:
        return os.path.expanduser(os.environ.get("HF_HUB_CACHE", "~/.cache/huggingface/hub"))


HF_CACHE = _hf_cache()


def open_folder(path):
    """Открыть папку в файловом менеджере ОС (создать, если её ещё нет)."""
    os.makedirs(path, exist_ok=True)
    if IS_WIN:
        os.startfile(path)   # noqa: S606 — штатный способ Windows
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
