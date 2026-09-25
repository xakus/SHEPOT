"""Запись с микрофона и нарезка длинной записи на чанки по паузам.

Не зависит от ОС: sounddevice (PortAudio) есть на Linux, Windows и macOS.
"""

import datetime
import threading
import time

import numpy as np

from .paths import LOG_PATH
from .settings import CHUNK_MAX, CHUNK_MIN, SAMPLE_RATE


def find_split(audio, sr, min_s, max_s):
    """Индекс разреза чанка: центр самого тихого 100-мс окна в [min_s, max_s].

    Режем по минимуму RMS-энергии, чтобы попасть в паузу между словами,
    а не в середину слова.
    """
    lo = int(min_s * sr)
    hi = min(int(max_s * sr), len(audio))
    win = int(0.1 * sr)
    seg = audio[lo:hi]
    n = len(seg) // win
    if n < 1:
        return hi
    rms = np.sqrt(np.mean(seg[: n * win].reshape(n, win) ** 2, axis=1))
    return lo + int(np.argmin(rms)) * win + win // 2


class Recorder:
    """Микрофон открыт постоянно; кадры копятся только пока active=True.

    gen — счётчик поколений записи: защищает от гонки, когда поток старой
    сессии пытается забрать кадры уже начавшейся новой записи.
    """

    def __init__(self):
        import sounddevice as sd
        self.frames, self.active, self.gen = [], False, 0
        self.lock = threading.Lock()
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                     dtype="float32", blocksize=1024, callback=self._cb)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        with self.lock:
            if self.active:
                self.frames.append(indata.copy())

    def start(self):
        """Начать копить кадры; возвращает номер поколения этой записи."""
        with self.lock:
            self.gen += 1
            self.frames, self.active = [], True
            return self.gen

    def pause(self):
        """Прекратить копить кадры; накопленный буфер сохраняется до drain()."""
        with self.lock:
            self.active = False

    def take_chunk(self, gen):
        """Отрезать готовый чанк, если накопилось >= CHUNK_MAX секунд.

        Остаток буфера остаётся копиться дальше. None — чанк ещё не набрался
        или запись уже принадлежит другому поколению.
        """
        with self.lock:
            if gen != self.gen:
                return None
            total = sum(len(f) for f in self.frames)
            if total < CHUNK_MAX * SAMPLE_RATE:
                return None
            audio = np.concatenate(self.frames, axis=0).flatten()
            cut = find_split(audio, SAMPLE_RATE, CHUNK_MIN, CHUNK_MAX)
            self.frames = [audio[cut:].reshape(-1, 1)]
            return audio[:cut]

    def drain(self, gen):
        """Забрать весь остаток буфера (хвост записи после отпускания клавиши)."""
        with self.lock:
            if gen != self.gen or not self.frames:
                return np.zeros(0, dtype=np.float32)
            audio = np.concatenate(self.frames, axis=0).flatten()
            self.frames = []
            return audio


class Session:
    """Одна диктовка: копит распознанные куски текста в порядке чанков."""

    def __init__(self):
        self.texts = []           # распознанные чанки по порядку
        self.chunks = 0           # сколько чанков отправлено на распознавание
        self.recording = True
        self.t0 = time.time()


def log_text(text):
    """Дописать распознанный кусок в лог — страховка от потери текста."""
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except OSError:
        pass
