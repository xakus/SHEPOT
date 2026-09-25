"""Самопроверка сборки без GUI: `shepot --selftest`.

Гоняется в CI на Linux, Windows и macOS после сборки PyInstaller: если в
сборку не попала библиотека (ctranslate2, onnxruntime, piper, PortAudio,
модуль платформы), проверка упадёт здесь, а не у пользователя.
Клавиши, трей и вставку текста так не проверить — это ручной чек-лист.
"""

import platform
import sys
import time
import traceback

from . import __version__


def _step(name, fn, results):
    """Выполнить один шаг, напечатать ok/FAIL и время."""
    t0 = time.time()
    try:
        info = fn()
        results.append(True)
        print(f"ok   {name} ({time.time() - t0:.1f} с){': ' + str(info) if info else ''}", flush=True)
    except Exception:
        results.append(False)
        print(f"FAIL {name}", flush=True)
        traceback.print_exc()


def _core():
    from . import audio, dictation, models, reader  # noqa: F401
    return "ядро импортируется"


def _split():
    from .reader import _selftest
    if not _selftest():
        raise AssertionError("резка текста работает неверно")


def _platform():
    from .platforms import current
    return current().__name__


def _gpu():
    from .gpu import prepare_cuda_libs
    dirs = prepare_cuda_libs()
    import ctranslate2
    try:
        n = ctranslate2.get_cuda_device_count()
    except Exception as e:
        n = f"нет ({e})"
    return f"ctranslate2 {ctranslate2.__version__}, GPU: {n}, папок CUDA-библиотек: {len(dirs)}"


def _whisper():
    import numpy as np
    from faster_whisper import WhisperModel
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)
    list(segs)
    return "tiny на CPU распознаёт"


def _piper():
    import onnxruntime
    import piper  # noqa: F401
    return f"onnxruntime {onnxruntime.__version__}"


def _sound():
    """Библиотека PortAudio должна загрузиться. Звукового сервера/устройств
    может не быть (CI) — тогда PortAudio не инициализируется, это не ошибка сборки."""
    try:
        import sounddevice as sd
    except Exception as e:             # PortAudioError не наследует OSError
        if "Error initializing PortAudio" in str(e):
            return f"библиотека есть, звуковой системы нет: {e}"
        raise
    return f"PortAudio {sd.get_portaudio_version()[1]}"


def run():
    """Все шаги; код выхода 0 — всё ok."""
    print(f"SHEPOT {__version__} selftest — {platform.system()} {platform.machine()}, "
          f"Python {platform.python_version()}, frozen={getattr(sys, 'frozen', False)}",
          flush=True)
    results = []
    for name, fn in [("импорт ядра", _core), ("резка текста", _split),
                     ("модуль платформы", _platform), ("CUDA/ctranslate2", _gpu),
                     ("звук (PortAudio)", _sound), ("Piper/onnxruntime", _piper),
                     ("Whisper tiny", _whisper)]:
        _step(name, fn, results)
    ok = all(results)
    print("SELFTEST OK" if ok else "SELFTEST FAILED", flush=True)
    return 0 if ok else 1
