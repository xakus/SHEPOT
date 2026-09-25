"""Настройки распознавания из переменных окружения SHEPOT_* и константы.

Выбор модели, устройства и клавиш из меню трея хранится в конфиге и
важнее переменных окружения; переменные — значения по умолчанию.
"""

import os

LANGUAGE     = os.environ.get("SHEPOT_LANG", "ru")          # пусто = автоопределение
COMPUTE_TYPE = os.environ.get("SHEPOT_COMPUTE", "int8")     # Pascal (GTX 10xx): только int8
DEVICE       = os.environ.get("SHEPOT_DEVICE", "auto")      # auto / cuda / cpu
INIT_PROMPT  = os.environ.get("SHEPOT_PROMPT", "")          # термины и имена собственные
CLIP_ONLY    = bool(os.environ.get("SHEPOT_CLIPBOARD_ONLY", ""))   # только в буфер, без вставки
CHUNK_MAX    = float(os.environ.get("SHEPOT_CHUNK", "30"))  # макс. длина чанка, сек
DEFAULT_MODEL = os.environ.get("SHEPOT_MODEL", "large-v3")

# Варианты устройства для распознавания и их подписи в меню трея.
# auto — GPU, если он есть и модель на нём загрузилась, иначе CPU.
DEVICE_LABELS = {"auto": "Авто", "cuda": "GPU (CUDA)", "cpu": "CPU"}

TTS_RATES = [0.8, 1.0, 1.2, 1.5, 2.0]   # варианты скорости чтения в меню

SAMPLE_RATE = 16000                         # частота записи для Whisper
MIN_SECONDS = 0.4                           # короче — считаем случайным нажатием
CHUNK_MIN   = max(5.0, CHUNK_MAX * 2 / 3)   # раньше этой границы чанк не режем
TYPE_DELAY  = 0.06                          # пауза перед вставкой текста, сек


def device_name(dev):
    """«cuda»/«cpu» → «GPU»/«CPU» для строки статуса."""
    return "GPU" if dev == "cuda" else "CPU"
