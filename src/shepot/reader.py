"""Чтение текста вслух (TTS): голоса Piper, конвейер синтеза и воспроизведения.

Ядро не зависит от ОС: откуда брать текст («источники»), решает платформа
(`shepot.platforms.*.make_sources`) и передаёт их в Reader. Запуск из
терминала — для проверки:

    python -m shepot.reader --say "Проверка связи. Второе предложение."
    python -m shepot.reader --say-file статья.txt --rate 1.3
    python -m shepot.reader --voices                 каталог голосов
    python -m shepot.reader --download ru_RU-denis-medium
    python -m shepot.reader --test                   мини-тесты резки текста

Конвейер: текст → split_blocks() → блоки по ~300 символов → поток-синтезатор
(Piper, по предложению за раз) → очередь на 2 блока → поток-плеер
(sounddevice, куски по 50 мс, между ними проверка стоп-флага). Первое слово
слышно через ~0.3 с, память не растёт на длинных текстах, GPU не нужен.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np

from .config import load_config, save_config
from .paths import VOICES_DIR

# --- настройки через переменные окружения -------------------------------
TTS_ENGINE    = os.environ.get("SHEPOT_TTS_ENGINE", "auto")        # auto / piper / espeak
TTS_VOICE     = os.environ.get("SHEPOT_TTS_VOICE", "ru_RU-irina-medium")
TTS_RATE      = float(os.environ.get("SHEPOT_TTS_RATE", "1.0"))    # 0.5 … 2.0
TTS_SOURCE    = os.environ.get("SHEPOT_TTS_SOURCE", "auto")        # auto / atspi / keys / clipboard
TTS_MAX_CHARS = int(os.environ.get("SHEPOT_TTS_MAX_CHARS", "20000"))
TTS_PRELOAD   = bool(os.environ.get("SHEPOT_TTS_PRELOAD", ""))     # грузить голос при старте

# Макс. длина блока текста. Пик памяти ONNX и задержка первого слова растут
# с длиной одного предложения: 300 симв. ≈ 17 с речи ≈ +240 МБ на пике.
# На слабом ПК можно уменьшить до 150–200.
BLOCK_MAX     = int(os.environ.get("SHEPOT_TTS_BLOCK", "300"))
PLAY_CHUNK_S  = 0.05    # кусок вывода звука, сек — задаёт скорость реакции на стоп
QUEUE_AHEAD   = 2       # сколько блоков синтезировать вперёд (обратное давление)
RATE_MIN, RATE_MAX = 0.5, 2.0

# Русские голоса из официального каталога Piper (все medium, ≈60 МБ каждый).
# Офлайн-список; точный каталог подтягивается с HuggingFace по запросу.
DEFAULT_VOICES = ["ru_RU-irina-medium", "ru_RU-denis-medium",
                  "ru_RU-dmitri-medium", "ru_RU-ruslan-medium"]
VOICES_JSON_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                   "voices.json?download=true")


NO_TEXT = ""   # источник уверен: текста нет (терминал, поле пароля)


def rss_mb():
    """Память процесса (RSS) в МБ — для логов производительности (только Linux, иначе -1)."""
    try:
        with open("/proc/self/status") as f:
            return int(f.read().split("VmRSS:")[1].split()[0]) // 1024
    except (OSError, IndexError, ValueError):
        return -1


def clamp_rate(rate):
    """Скорость чтения в допустимых границах; мусор → 1.0."""
    try:
        return max(RATE_MIN, min(RATE_MAX, float(rate)))
    except (TypeError, ValueError):
        return 1.0


# --- резка текста на блоки -----------------------------------------------

_SENT_END   = re.compile(r"(?<=[.!?…])\s+")    # конец предложения
_SOFT_SPLIT = re.compile(r"(?<=[;,:])\s+")     # запасные точки разреза


def _pack(parts, max_len, overflow):
    """Жадно склеивать куски через пробел, пока блок не длиннее max_len.

    Кусок, который сам длиннее max_len, отдаётся в overflow(кусок) -> список
    кусков поменьше.
    """
    out, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_len:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(overflow(p))
        elif cur and len(cur) + 1 + len(p) > max_len:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}" if cur else p
    if cur:
        out.append(cur)
    return out


def _hard_split(s, max_len):
    """Нет ни одного знака препинания: рубим по последнему пробелу перед
    max_len; нет и пробела (одно «слово» на полкилометра) — режем как есть."""
    out = []
    while len(s) > max_len:
        cut = s.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


def _split_paragraph(para, max_len):
    """Абзац → блоки ≤ max_len: сначала по предложениям, длинное предложение
    — по ; , :, совсем длинное — по пробелам."""
    if len(para) <= max_len:
        return [para]
    return _pack(_SENT_END.split(para), max_len,
                 lambda s: _pack(_SOFT_SPLIT.split(s), max_len,
                                 lambda t: _hard_split(t, max_len)))


def split_blocks(text, max_len=BLOCK_MAX):
    """Текст → список блоков для синтеза.

    Абзацы (через пустую строку) не склеиваются — между ними естественная
    пауза. Внутри абзаца переносы строк схлопываются в пробел. Блоки без
    единой буквы или цифры («---», «***») выбрасываются — читать нечего.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if not re.search(r"\w", para):
            continue
        blocks.extend(_split_paragraph(para, max_len))
    return blocks


# --- голоса Piper --------------------------------------------------------

class VoiceManager:
    """Каталог голосов Piper: что известно, что скачано, сколько весит.

    Устроен как ModelManager в models.py: выбор пользователя — в конфиге,
    список и размеры можно обновить с HuggingFace, скачанное можно удалить.
    status — колбэк для строки статуса (трей или print).
    """

    def __init__(self, config, status=print):
        self.config, self.status = config, status
        self.current = config.get("tts_voice") or TTS_VOICE
        self.remote_sizes = {}    # имя -> размер скачивания (байт), с HuggingFace

    def model_path(self, name):
        return os.path.join(VOICES_DIR, name + ".onnx")

    def is_downloaded(self, name):
        p = self.model_path(name)
        return (os.path.isfile(p) and os.path.getsize(p) > 0
                and os.path.isfile(p + ".json"))

    def known_names(self):
        """Офлайн-список + голоса из каталога HF + всё, что лежит в папке."""
        names = list(DEFAULT_VOICES)
        for n in list(self.remote_sizes) + self.local_names():
            if n not in names:
                names.append(n)
        return names

    def local_names(self):
        """Имена голосов, у которых в папке есть .onnx (любые, не только ru)."""
        try:
            files = os.listdir(VOICES_DIR)
        except OSError:
            return []
        return sorted(f[:-5] for f in files if f.endswith(".onnx"))

    def catalog(self):
        """Записи для меню трея: имя, скачан ли, размер, можно ли удалить."""
        entries = []
        for name in self.known_names():
            dl = self.is_downloaded(name)
            if dl:
                size = os.path.getsize(self.model_path(name))
            else:
                size = self.remote_sizes.get(name, 60 * (1 << 20))
            entries.append({"name": name, "downloaded": dl,
                            "size": f"{size / (1 << 20):.0f} МБ",
                            "removable": dl and name != self.current})
        return entries

    def set_current(self, name):
        """Голос переключён — запомнить в конфиге."""
        self.current = name
        self.config["tts_voice"] = name
        save_config(self.config)

    def delete(self, name):
        """Удалить скачанный голос (активный удалять нельзя)."""
        if name == self.current:
            return
        for ext in (".onnx", ".onnx.json"):
            try:
                os.remove(os.path.join(VOICES_DIR, name + ext))
            except OSError:
                pass

    def download(self, name):
        """Скачать .onnx.json и .onnx голоса с прогрессом в статусе.

        Формат URL берём из самого piper (download_voices.URL_FORMAT), чтобы
        не разойтись с ним при смене хостинга. Качаем во временный файл и
        переименовываем — недокачанный голос не будет считаться скачанным.
        """
        from piper.download_voices import URL_FORMAT, VOICE_PATTERN
        m = VOICE_PATTERN.match(name)
        if not m:
            raise ValueError(f"Не похоже на имя голоса Piper: {name}")
        args = dict(lang_family=m["lang_family"],
                    lang_code=f'{m["lang_family"]}_{m["lang_region"]}',
                    voice_name=m["voice_name"], voice_quality=m["voice_quality"])
        os.makedirs(VOICES_DIR, exist_ok=True)
        for ext in (".onnx.json", ".onnx"):
            url = URL_FORMAT.format(extension=ext, **args)
            dst = os.path.join(VOICES_DIR, name + ext)
            tmp = dst + ".part"
            with urllib.request.urlopen(url, timeout=30) as r, open(tmp, "wb") as f:
                total, done, last = int(r.headers.get("Content-Length") or 0), 0, -1
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(100 * done / total) if total else 0
                    if ext == ".onnx" and pct != last:   # json мелкий — не мигаем
                        self.status(f"Скачивание голоса {name}: {pct}%")
                        last = pct
            os.replace(tmp, dst)

    def refresh_remote(self):
        """Подтянуть с HuggingFace список русских голосов и точные размеры."""
        with urllib.request.urlopen(VOICES_JSON_URL, timeout=15) as r:
            data = json.load(r)
        for key, info in data.items():
            if key.startswith("ru_"):
                self.remote_sizes[key] = sum(
                    f.get("size_bytes", 0) for f in info.get("files", {}).values())
        return len(self.remote_sizes)


# --- движки --------------------------------------------------------------

class PiperEngine:
    """Нейросетевой синтез Piper на CPU: отдаёт звук по предложению."""
    name = "piper"

    def __init__(self):
        self.voice = None        # загруженный PiperVoice
        self.voice_name = None   # имя загруженного голоса

    def load(self, name, path):
        """Загрузить голос (.onnx + .onnx.json рядом) и прогреть сессию ONNX:
        первый вызов synthesize медленнее последующих в несколько раз.

        Сессию собираем сами, а не через PiperVoice.load(): тот включает
        арену памяти onnxruntime, которая растёт до размера самого длинного
        предложения и не возвращает память (замер: предложение в 350 симв.
        → процесс 793 МБ навсегда; без арены — пик 275 МБ и память
        освобождается). Цена — синтез на ~20 % медленнее, всё равно в 19 раз
        быстрее реального времени.
        """
        import onnxruntime
        from piper import PiperVoice
        from piper.config import PiperConfig
        with open(path + ".json", encoding="utf-8") as f:
            cfg = PiperConfig.from_dict(json.load(f))
        opts = onnxruntime.SessionOptions()
        opts.enable_cpu_mem_arena = False
        session = onnxruntime.InferenceSession(
            path, sess_options=opts, providers=["CPUExecutionProvider"])
        voice = PiperVoice(config=cfg, session=session)
        for _ in voice.synthesize("Готово."):
            pass
        self.voice, self.voice_name = voice, name

    def unload(self):
        self.voice, self.voice_name = None, None

    def synth(self, block, rate):
        """Блок текста → (audio float32, sample_rate) по одному предложению.
        length_scale обратно пропорционален скорости: 0.5 → вдвое быстрее."""
        from piper import SynthesisConfig
        cfg = SynthesisConfig(length_scale=1.0 / rate)
        for ch in self.voice.synthesize(block, cfg):
            yield ch.audio_float_array.astype(np.float32, copy=False), ch.sample_rate

    def stop(self):
        pass   # синтез прерывается стоп-флагом между предложениями


class EspeakEngine:
    """Запасной движок: espeak-ng. Робот, но ставится одним apt и весит 3 МБ.

    Синтезирует через `espeak-ng --stdout` в WAV, который мы сами
    раскладываем в float32 — звук идёт через тот же плеер, что и Piper, стоп
    работает так же. speech-dispatcher (spd-say) не используем: на машине с
    PipeWire его поток зависал в состоянии init и END не приходил никогда.
    """
    name = "espeak"

    def __init__(self):
        self.voice_name = "espeak-ng"

    def load(self, name, path):
        pass

    def unload(self):
        pass

    @staticmethod
    def available():
        return bool(shutil.which("espeak-ng"))

    def synth(self, block, rate):
        # скорость espeak-ng — слов в минуту, 175 = обычная
        wpm = int(175 * rate)
        r = subprocess.run(["espeak-ng", "-v", "ru", "-s", str(wpm), "--stdout", "--", block],
                           capture_output=True, check=False)
        wav = r.stdout
        i = wav.find(b"data")
        if i < 0 or len(wav) < 44:
            return
        sr = int.from_bytes(wav[24:28], "little")
        pcm = np.frombuffer(wav[i + 8:], dtype="<i2")
        if len(pcm):
            yield pcm.astype(np.float32) / 32768.0, sr

    def stop(self):
        pass   # процесс короткий, стоп-флаг проверяется между блоками


def make_engine(kind):
    """Выбрать движок: auto — Piper, если он установлен, иначе espeak-ng.
    Нет ни того, ни другого — RuntimeError с подсказкой, что поставить."""
    if kind in ("auto", "piper"):
        try:
            import piper  # noqa: F401 — проверяем только наличие
            return PiperEngine()
        except ImportError as e:
            if kind == "piper":
                raise
            print(f"piper недоступен ({e}), откат на espeak-ng", flush=True)
    if EspeakEngine.available():
        return EspeakEngine()
    raise RuntimeError("нет движка: pip install piper-tts или apt install espeak-ng")



# --- конвейер чтения -----------------------------------------------------

# --- конвейер чтения -----------------------------------------------------

class ConsoleUI:
    """Заглушка интерфейса для запуска из терминала (в демоне — Tray)."""

    def set_status(self, text):
        print("СТАТУС:", text, flush=True)

    def set_reading(self, on):
        pass


class Reader:
    """Текст → блоки → синтез → звук. Один сеанс чтения за раз.

    ui      — объект с set_status(text) и set_reading(bool); все вызовы идут
              из фоновых потоков, поэтому трей сам переносит их в поток UI.
    config  — общий словарь конфига (тот же объект, что у трея и диктовки).
    """

    def __init__(self, config, ui=None, engine_kind=None, sources=None):
        self.config, self.ui = config, ui or ConsoleUI()
        self.rate = clamp_rate(config.get("tts_rate", TTS_RATE))
        self.engine_kind = engine_kind or config.get("tts_engine") or TTS_ENGINE
        self.voices = VoiceManager(config, self.ui.set_status)
        self.sources = sources or []         # источники текста от платформы, по порядку опроса
        self.engine = None                   # создаётся лениво в ensure_ready()
        self.lock = threading.Lock()         # start/stop/ensure_ready — по одному
        self.stop_flag = threading.Event()
        self.reading = False
        self.threads = []
        self.audio_q = None
        self.toggle_lock = threading.Lock()  # второе нажатие во время grab() — игнор
        self.on_voices_changed = None        # колбэк трея: перестроить меню голосов
        if TTS_PRELOAD:
            threading.Thread(target=self.ensure_ready, daemon=True).start()

    # -- подготовка движка --

    def ensure_ready(self):
        """Создать движок и загрузить выбранный голос (скачать при нужде).

        Ошибка Piper (нет сети, битый файл) → откат на espeak-ng до
        перезапуска, чтобы клавиша чтения не оказалась мёртвой.
        """
        with self.lock:
            if self.engine is None:
                self.engine = make_engine(self.engine_kind)
            eng = self.engine
            if not isinstance(eng, PiperEngine) or eng.voice_name == self.voices.current:
                return
            name = self.voices.current
            try:
                if not self.voices.is_downloaded(name):
                    self.ui.set_status(f"Скачивание голоса {name}…")
                    self.voices.download(name)
                self.ui.set_status(f"Загрузка голоса {name}…")
                t0 = time.time()
                eng.load(name, self.voices.model_path(name))
                print(f"голос {name} загружен за {time.time() - t0:.1f} с", flush=True)
                self.ui.set_status("Готов")
            except Exception as e:
                print(f"ошибка голоса {name}: {e} — откат на espeak-ng", flush=True)
                self.ui.set_status(f"Ошибка голоса {name}: {e}")
                self.engine = EspeakEngine() if EspeakEngine.available() else None
                if self.engine is None:
                    raise

    def set_rate(self, rate):
        """Сменить скорость; действует со следующего блока."""
        self.rate = clamp_rate(rate)
        self.config["tts_rate"] = self.rate
        save_config(self.config)

    def select_voice(self, name):
        """Переключить голос: остановить чтение и загрузить новый в фоне."""
        if name == self.voices.current:
            return
        self.stop()
        old = self.voices.current
        self.voices.set_current(name)
        if self.engine:
            self.engine.unload()

        def go():
            self.ensure_ready()
            if isinstance(self.engine, EspeakEngine) and self.engine_kind != "espeak":
                # загрузка нового не удалась — вернуть прежний рабочий голос
                self.voices.set_current(old)
                self.engine = None
                self.ensure_ready()
            self._voices_changed()
        threading.Thread(target=go, daemon=True).start()

    def set_engine(self, kind):
        """Сменить движок (piper / espeak): пересоздаётся при следующем чтении."""
        if kind == self.engine_kind:
            return
        self.stop()
        self.engine_kind = kind
        self.config["tts_engine"] = kind
        save_config(self.config)
        if self.engine:
            self.engine.unload()
        self.engine = None
        self._voices_changed()

    def delete_voice(self, name):
        """Удалить скачанный голос (кроме активного) и обновить меню."""
        self.voices.delete(name)
        self.ui.set_status(f"Голос {name} удалён")
        self._voices_changed()

    def refresh_voices(self):
        """Подтянуть каталог голосов с HuggingFace в фоне."""
        def go():
            self.ui.set_status("Обновление списка голосов…")
            try:
                n = self.voices.refresh_remote()
                self.ui.set_status(f"Список обновлён: {n} русских голосов на сайте")
            except Exception as e:
                self.ui.set_status(f"Сеть недоступна: {e}")
            self._voices_changed()
        threading.Thread(target=go, daemon=True).start()

    def _voices_changed(self):
        if self.on_voices_changed:
            self.on_voices_changed()

    # -- управление --

    def grab(self):
        """Опросить источники по порядку: (текст, имя источника) или (None, None).

        Если текст взяли клавиши (выделив от курсора до конца), а другой
        источник умеет вернуть курсор (AT-SPI на Linux), — вернуть его на
        место (← в LibreOffice Writer этого не делает).
        """
        restorer = next((s for s in self.sources if hasattr(s, "caret_restore")), None)
        for src in self.sources:
            t0 = time.time()
            try:
                text = src.grab()
            except Exception as e:
                print(f"источник {src.name}: ошибка {e}", flush=True)
                text = None
            got = f"{len(text)} симв." if text and text.strip() else "нет текста"
            print(f"источник {src.name}: {got} за {time.time() - t0:.2f} с", flush=True)
            if text and text.strip():
                if restorer and getattr(src, "moved_caret", False):
                    if restorer.caret_restore():
                        print("курсор возвращён через AT-SPI", flush=True)
                return text, src.name
            if text is NO_TEXT:        # источник уверен: читать нечего
                break
        return None, None

    def toggle(self):
        """Горячая клавиша: читает → стоп; иначе достать текст и читать.
        Пока идёт grab() (до ~3 с), повторные нажатия игнорируются."""
        if self.reading:
            self.stop()
            return
        if not self.toggle_lock.acquire(blocking=False):
            return
        try:
            self.ui.set_status("Ищу текст…")
            text, _ = self.grab()
            if not text:
                self.ui.set_status("Нет текста — поставь курсор в текст или выдели")
                return
            self.start(text)
        finally:
            self.toggle_lock.release()

    def start(self, text):
        """Начать читать текст (обрезается до TTS_MAX_CHARS)."""
        self.stop()
        note = ""
        if len(text) > TTS_MAX_CHARS:
            text, note = text[:TTS_MAX_CHARS], f" (обрезано до {TTS_MAX_CHARS} симв.)"
        blocks = split_blocks(text)
        if not blocks:
            self.ui.set_status("Нет текста — поставь курсор в текст или выдели")
            return
        try:
            self.ensure_ready()
        except Exception as e:
            print("движок недоступен:", e, flush=True)
            self.ui.set_status(f"Чтение недоступно: {e}")
            return
        with self.lock:
            self.stop_flag.clear()
            self.reading = True
            self.audio_q = queue.Queue(maxsize=QUEUE_AHEAD)
            self.ui.set_reading(True)
            self.ui.set_status(f"Читаю… 1/{len(blocks)}{note}")
            print(f"ЧТЕНИЕ: {len(text)} симв., {len(blocks)} блоков, "
                  f"движок={self.engine.name}", flush=True)
            self.threads = [
                threading.Thread(target=self._synth_loop, args=(blocks,), daemon=True),
                threading.Thread(target=self._play_loop, args=(len(blocks), note),
                                 daemon=True)]
            for t in self.threads:
                t.start()

    def stop(self):
        """Остановить чтение: флаг → движок → дождаться потоков (≤ 1 с)."""
        with self.lock:
            if not self.reading:
                return
            self.stop_flag.set()
            if self.engine:
                self.engine.stop()
            for t in self.threads:
                t.join(timeout=1.0)
            self.threads = []
            print("ЧТЕНИЕ: остановлено", flush=True)
            self._finish("Остановлено")

    def _finish(self, status):
        """Общий конец сеанса — и по стопу, и по последнему блоку."""
        self.reading = False
        self.ui.set_reading(False)
        self.ui.set_status(status)

    # -- потоки --

    def _put(self, item):
        """Положить в очередь, не зависая, если пришёл стоп."""
        while not self.stop_flag.is_set():
            try:
                self.audio_q.put(item, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def _synth_loop(self, blocks):
        """Синтез блоков по порядку → очередь. Время считаем только внутри
        движка: ожидание места в очереди (обратное давление) — не синтез."""
        eng, n = self.engine, len(blocks)
        total_audio, total_time = 0.0, 0.0
        try:
            for i, block in enumerate(blocks):
                if self.stop_flag.is_set():
                    return
                secs, dt = 0.0, 0.0
                gen = eng.synth(block, self.rate)
                while True:
                    t0 = time.time()
                    item = next(gen, None)
                    dt += time.time() - t0
                    if item is None:
                        break
                    audio, sr = item
                    secs += len(audio) / sr
                    if not self._put((i, audio, sr)):
                        return
                total_audio, total_time = total_audio + secs, total_time + dt
                if secs:
                    print(f"блок {i + 1}/{n}: синтез {secs:.1f} с аудио за {dt:.2f} с "
                          f"(x{secs / dt if dt else 0:.0f})", flush=True)
            if total_time:
                print(f"итого: {total_audio:.1f} с аудио за {total_time:.2f} с "
                      f"(x{total_audio / total_time:.0f}), RSS {rss_mb()} МБ", flush=True)
        except Exception as e:
            print("ошибка синтеза:", e, flush=True)
            self.ui.set_status(f"Ошибка синтеза: {e}")
        finally:
            self._put(None)   # маркер конца для плеера

    def _play_loop(self, n, note):
        """Играть блоки из очереди кусками по PLAY_CHUNK_S, проверяя стоп."""
        q, stream, cur_sr, last_i = self.audio_q, None, None, -1
        try:
            while not self.stop_flag.is_set():
                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    break
                i, audio, sr = item
                if stream is None or sr != cur_sr:
                    if stream:
                        stream.close()
                    import sounddevice as sd
                    stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32")
                    stream.start()
                    cur_sr = sr
                if i != last_i:
                    self.ui.set_status(f"Читаю… {i + 1}/{n}{note}")
                    last_i = i
                step = int(PLAY_CHUNK_S * sr)
                data = audio.reshape(-1, 1)
                for pos in range(0, len(data), step):
                    if self.stop_flag.is_set():
                        break
                    stream.write(data[pos:pos + step])
        except Exception as e:
            print("ошибка воспроизведения:", e, flush=True)
            self.ui.set_status(f"Ошибка звука: {e}")
        finally:
            if stream:
                if self.stop_flag.is_set():
                    stream.abort()   # выбросить недоигранный буфер
                stream.close()
            if not self.stop_flag.is_set():
                self._finish("Готов")



# --- запуск из терминала -------------------------------------------------

def _selftest():
    """Мини-тесты split_blocks: без потерь слов, блоки не длиннее лимита."""
    def words(blocks):
        return " ".join(blocks).split()

    cases = {
        "пусто": ("", []),
        "пробелы": ("   \n\n  \t ", []),
        "только знаки": ("---\n\n***\n\n. . .", []),
        "абзацы": ("Раз. Два.\n\nТри.", ["Раз. Два.", "Три."]),
        "перенос строки": ("строка один\nстрока два", ["строка один строка два"]),
        "windows-переносы": ("а\r\n\r\nб", ["а", "б"]),
    }
    ok = True
    for name, (src, want) in cases.items():
        got = split_blocks(src)
        print(f"{'ok ' if got == want else 'FAIL'} {name}: {got}")
        ok &= got == want

    long_sent = " ".join(f"Предложение номер {i}." for i in range(60))
    long_plain = "слово " * 300
    long_word = "ы" * 950
    for name, src in [("60 предложений", long_sent), ("без знаков", long_plain),
                      ("одно слово 950 симв.", long_word),
                      ("длинное с запятыми", ", ".join(["часть"] * 200))]:
        got = split_blocks(src)
        good = (len(got) > 1 and all(len(b) <= BLOCK_MAX for b in got)
                and words(got) == src.split())
        # одно слово без пробелов рубится посреди — там слова не сохраняются
        if name.startswith("одно слово"):
            good = len(got) == -(-len(src) // BLOCK_MAX) and "".join(got) == src
        print(f"{'ok ' if good else 'FAIL'} {name}: {len(got)} блоков, "
              f"макс {max(len(b) for b in got)} симв.")
        ok &= good
    print("ВСЕ ТЕСТЫ ОК" if ok else "ЕСТЬ ОШИБКИ")
    return ok


def main():
    """Проверка чтения из терминала (без трея и диктовки)."""
    import argparse
    ap = argparse.ArgumentParser(description="Чтение текста вслух (проверка из терминала)")
    ap.add_argument("--say", help="прочитать текст")
    ap.add_argument("--say-file", help="прочитать файл")
    ap.add_argument("--engine", choices=["auto", "piper", "espeak"], help="движок")
    ap.add_argument("--voice", help="голос Piper, напр. ru_RU-denis-medium")
    ap.add_argument("--rate", type=float, help="скорость 0.5 … 2.0")
    ap.add_argument("--voices", action="store_true", help="каталог голосов")
    ap.add_argument("--download", metavar="NAME", help="скачать голос")
    ap.add_argument("--test", action="store_true", help="мини-тесты резки текста")
    ap.add_argument("--grab", action="store_true",
                    help="через --delay секунд достать текст из окна в фокусе и напечатать")
    ap.add_argument("--read", action="store_true",
                    help="как --grab, но затем прочитать вслух")
    ap.add_argument("--source", choices=["auto", "atspi", "keys", "clipboard"],
                    help="источник текста (по умолчанию SHEPOT_TTS_SOURCE)")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="пауза перед --grab/--read, чтобы кликнуть в окно (сек)")
    a = ap.parse_args()

    if a.test:
        sys.exit(0 if _selftest() else 1)

    sources = []
    if a.grab or a.read:
        from .platforms import current
        sources = current().make_sources(a.source or TTS_SOURCE, own_loop=True)
    config = load_config()
    reader = Reader(config, engine_kind=a.engine, sources=sources)
    if a.voice:
        reader.voices.current = a.voice     # только на этот запуск, в конфиг не пишем
    if a.rate:
        reader.rate = clamp_rate(a.rate)

    if a.voices:
        try:
            reader.voices.refresh_remote()
        except Exception as e:
            print(f"каталог HuggingFace недоступен: {e}")
        for e in reader.voices.catalog():
            mark = "скачан" if e["downloaded"] else "скачать"
            cur = " ←" if e["name"] == reader.voices.current else ""
            print(f'{e["name"]:26} {e["size"]:>7}  {mark}{cur}')
        return

    if a.download:
        reader.voices.download(a.download)
        print("скачан:", reader.voices.model_path(a.download))
        return

    if a.grab or a.read:
        print(f"через {a.delay:g} с возьму текст из окна в фокусе — кликни в текст…",
              flush=True)
        time.sleep(a.delay)
        text, src = reader.grab()
        if not text:
            print("текста нет")
            return
        print(f"--- источник: {src}, {len(text)} символов ---")
        print(text[:600] + ("…" if len(text) > 600 else ""))
        if not a.read:
            return
        _play_until_done(reader, text)
        return

    text = a.say
    if a.say_file:
        with open(a.say_file, encoding="utf-8") as f:
            text = f.read()
    if not text:
        ap.print_help()
        return
    _play_until_done(reader, text)


def _play_until_done(reader, text):
    """Читать текст и ждать конца; Ctrl+C — стоп."""
    try:
        reader.start(text)
        while reader.reading:
            time.sleep(0.1)
    except KeyboardInterrupt:
        reader.stop()
        print("\nостановлено")


if __name__ == "__main__":
    main()
