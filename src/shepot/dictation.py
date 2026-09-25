"""Ядро диктовки: модель Whisper, очередь распознавания, обработка клавиш.

Не зависит от ОС. Платформа передаёт сюда события клавиш в общем виде
(`on_key(имя, значение)`), а текст вставляет через `typer(text)`.

Один поток-транскрайбер владеет моделью и разбирает очередь `jobs`:
    ("chunk", session, audio)  — распознать кусок
    ("finish", session)        — сессия кончилась: склеить и вставить
    ("switch", name)           — сменить модель (скачает при необходимости)
    ("device", pref)           — сменить устройство (auto/cuda/cpu) и перезагрузить модель
Так порядок чанков сохраняется, а смена модели не требует блокировок.
"""

import gc
import os
import queue
import threading
import time

import numpy as np

from .audio import Recorder, Session, log_text
from .config import save_config
from .gpu import prepare_cuda_libs
from .models import ModelManager, download_model, is_downloaded
from .settings import (COMPUTE_TYPE, DEVICE_LABELS, INIT_PROMPT, LANGUAGE, MIN_SECONDS,
                       SAMPLE_RATE, TYPE_DELAY, device_name)

# значения событий клавиш (как в evdev)
KEY_UP, KEY_DOWN, KEY_REPEAT = 0, 1, 2


class Dictation:
    """Диктовка по удержанию клавиши + запуск чтения по одиночному нажатию.

    tray   — объект трея (TrayBase): статус, иконка, выбранные клавиши и устройство;
    reader — Reader или None (чтение недоступно);
    typer  — функция вставки текста в позицию курсора.
    """

    def __init__(self, tray, config, reader, typer):
        self.tray, self.config, self.reader, self.typer = tray, config, reader, typer
        self.jobs = queue.Queue()
        self.manager = ModelManager(tray, self.jobs, config)
        self.state = {"model": None, "ready": False, "device": None}   # device — где реально работает модель
        self.has_gpu = False
        self.rec = None
        # состояние клавиш
        self.pressed, self.session, self.gen = False, None, 0
        self.tts_down, self.tts_solo = False, False   # клавиша чтения зажата / без других клавиш
        tray.jobs = self.jobs

    # -- запуск --

    def start(self):
        """Открыть микрофон, запустить транскрайбер и первую загрузку модели.

        Вызывается из фонового потока: импорт ctranslate2 и загрузка
        модели идут секунды, трей в это время уже виден.
        """
        prepare_cuda_libs()
        import ctranslate2                     # движок faster-whisper: проверка наличия GPU
        try:
            self.has_gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception as e:                 # нет драйвера / битая CUDA — работаем на CPU
            print("проверка GPU не удалась:", e, flush=True)
            self.has_gpu = False
        self.rec = Recorder()
        threading.Thread(target=self._transcriber, daemon=True).start()
        self.jobs.put(("switch", self.manager.current))   # первая загрузка — тем же путём
        self.manager.push()

    # -- модель --

    def _open_model(self, src, name, device, compute):
        """Загрузить модель на устройство и прогнать секунду тишины (прогрев)."""
        from faster_whisper import WhisperModel
        self.tray.set_status(f"Загрузка {name} в {device_name(device)}…")
        model = WhisperModel(src, device=device, compute_type=compute)
        segs, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), beam_size=1)
        list(segs)
        self.state["device"] = device
        print(f"модель {name} загружена: {device}/{compute}", flush=True)
        return model

    def load_model(self, name):
        """Скачать при необходимости и загрузить модель с прогревом.

        Автопереключение на CPU: если видеокарты нет — сразу грузим на CPU;
        если есть, но загрузка на неё упала (нет CUDA/cuDNN, не хватило
        VRAM) — пробуем ещё раз на CPU. На CPU всегда int8: float16 он
        не умеет, а int8 — самый быстрый вариант для процессора.
        """
        src = self.manager.path_or_name(name)   # путь локальной папки или имя/репо
        if not os.path.isdir(src) and not is_downloaded(name):
            self.tray.set_status(f"Скачивание {name}…")
            download_model(name, self.tray)
        if self.tray.device_pref == "cpu":      # CPU выбран вручную
            return self._open_model(src, name, "cpu", "int8")
        # auto или GPU: без видеокарты / при ошибке — откат на CPU, чтобы
        # диктовка работала всегда; в меню будет видно «GPU → CPU»
        if not self.has_gpu:
            print("видеокарта CUDA не найдена — работаю на CPU", flush=True)
            return self._open_model(src, name, "cpu", "int8")
        try:
            return self._open_model(src, name, "cuda", COMPUTE_TYPE)
        except Exception as e:
            print(f"не удалось загрузить {name} на GPU: {e} — пробую CPU", flush=True)
            gc.collect()                        # освободить то, что успело занять VRAM
            return self._open_model(src, name, "cpu", "int8")

    def _do_switch(self, name):
        tray, state = self.tray, self.state
        old_name = self.manager.current
        state["model"], state["ready"] = None, False
        gc.collect()                            # освободить VRAM старой модели
        try:
            state["model"] = self.load_model(name)
            state["ready"] = True
            self.manager.set_current(name)
            tray.icon("idle" if tray.enabled else "off")
            tray.set_status(f"Готов ({device_name(state['device'])})")
        except Exception as e:
            tray.set_status(f"Ошибка {name}: {e}")
            print("ошибка смены модели:", e, flush=True)
            if name != old_name:                # откат на прежнюю рабочую модель
                self.jobs.put(("switch", old_name))
            else:
                tray.icon("off")
        tray.call_soon(tray.set_device_menu, tray.device_pref,
                       state["device"] if state["ready"] else None, self.has_gpu)

    def _do_device(self, pref):
        """Сменить устройство и перезагрузить текущую модель; не вышло — вернуть прежнее."""
        tray = self.tray
        old = tray.device_pref
        if pref == old:
            return
        tray.device_pref = pref
        self._do_switch(self.manager.current)
        if not self.state["ready"]:
            print(f"устройство {pref} не работает — возвращаю {old}", flush=True)
            tray.device_pref = old
            self._do_switch(self.manager.current)
            tray.set_status(f"{DEVICE_LABELS[pref]} недоступен — оставил {DEVICE_LABELS[old]}")
            return
        self.config["device"] = pref
        save_config(self.config)

    # -- распознавание --

    def _transcribe_one(self, audio):
        segments, _ = self.state["model"].transcribe(
            audio, language=LANGUAGE or None, beam_size=1, vad_filter=True,
            condition_on_previous_text=False, initial_prompt=INIT_PROMPT or None)
        return " ".join(s.text.strip() for s in segments).strip()

    def _transcriber(self):
        tray = self.tray
        while True:
            job = self.jobs.get()
            kind = job[0]
            try:
                if kind == "device":
                    self._do_device(job[1])
                elif kind == "switch":
                    self._do_switch(job[1])
                elif kind == "chunk":
                    _, session, audio = job
                    if not session.recording:
                        tray.set_status("Распознавание…")
                    text = self._transcribe_one(audio)
                    if text:
                        session.texts.append(text)
                        log_text(text)
                elif kind == "finish":
                    session = job[1]
                    text = " ".join(session.texts).strip()
                    print("РАСПОЗНАНО:", repr(text), flush=True)
                    tray.set_last(text)
                    tray.set_status("Готов")
                    if text:
                        log_text("--- конец сессии ---")
                        time.sleep(TYPE_DELAY)
                        self.typer(text + " ")
            except Exception as e:             # поток-транскрайбер не должен умирать
                print(f"ошибка задания {kind}:", e, flush=True)
                tray.set_status(f"Ошибка: {e}")

    def _pump(self, session, gen):
        """Пока идёт запись — отдавать готовые чанки транскрайберу;
        после отпускания клавиши — отдать хвост и маркер конца."""
        rec, tray = self.rec, self.tray
        while session.recording:
            time.sleep(0.2)
            chunk = rec.take_chunk(gen)
            if chunk is not None:
                session.chunks += 1
                self.jobs.put(("chunk", session, chunk))
            if session.recording:
                m, s = divmod(int(time.time() - session.t0), 60)
                done = f", готово {len(session.texts)}" if session.texts else ""
                tray.set_status(f"Запись… {m}:{s:02d}{done}")
        tail = rec.drain(gen)
        if tail.size >= MIN_SECONDS * SAMPLE_RATE:
            self.jobs.put(("chunk", session, tail))
        elif session.chunks == 0:
            tray.set_status("Готов")            # случайное короткое нажатие
            return
        self.jobs.put(("finish", session))

    # -- клавиши --

    def on_key(self, name, value):
        """Событие клавиши от платформы: name — имя из keys.py (или OTHER),
        value — KEY_DOWN / KEY_UP / KEY_REPEAT. Вызывается из одного потока."""
        tray, reader = self.tray, self.reader
        # Чтение вслух: срабатывает на ОТПУСКАНИЕ клавиши, и только если
        # пока она была зажата, не нажималось ничего другого — так
        # AltGr+8 или Alt+Tab не запускают чтение. Автоповтор — мимо.
        # Клавиши берём из tray (меняются из меню на лету).
        if reader and name == tray.read_key_name:
            if value == KEY_DOWN:
                self.tts_down, self.tts_solo = True, True
            elif value == KEY_UP and self.tts_down:
                self.tts_down = False
                if self.tts_solo and tray.tts_enabled:
                    threading.Thread(target=reader.toggle, daemon=True).start()
            return
        if self.tts_down and value == KEY_DOWN:
            self.tts_solo = False
        if name != tray.dictate_key_name or not tray.enabled:
            return
        if value == KEY_DOWN and not self.pressed:
            if not self.state["ready"]:
                tray.set_status("Модель ещё не готова — подожди")
                return
            if reader and reader.reading:       # колонки не должны попасть в микрофон
                threading.Thread(target=reader.stop, daemon=True).start()
            self.pressed = True
            self.gen = self.rec.start()
            self.session = Session()
            tray.icon("rec")
            tray.set_status("Запись…")
            print("нажатие зафиксировано", flush=True)
            threading.Thread(target=self._pump, args=(self.session, self.gen),
                             daemon=True).start()
        elif value == KEY_UP and self.pressed:
            self.pressed = False
            self.session.recording = False
            self.rec.pause()
            tray.icon("idle")
