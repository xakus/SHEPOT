#!/usr/bin/env python3
"""Чтение текста вслух (TTS) для shepot.

Модуль независим от диктовки: свой движок, свои настройки, свой конвейер.
shepot.py только дёргает Reader.toggle() по горячей клавише и показывает
статус в трее. Запускается и сам по себе — для проверки из терминала:

    shepot_reader.py --say "Проверка связи. Второе предложение."
    shepot_reader.py --say-file статья.txt --rate 1.3
    shepot_reader.py --engine espeak --say "робот"
    shepot_reader.py --voices                 каталог голосов
    shepot_reader.py --download ru_RU-denis-medium
    shepot_reader.py --test                   мини-тесты резки текста

Конвейер: текст → split_blocks() → блоки по ~300 символов → поток-синтезатор
(Piper, по предложению за раз) → очередь на 2 блока → поток-плеер
(sounddevice, куски по 50 мс, между ними проверка стоп-флага). Первое слово
слышно через ~0.3 с, память не растёт на длинных текстах, GPU не нужен.
"""

import os, sys, re, time, json, threading, subprocess, queue, urllib.request
import shutil, uuid

import numpy as np
import sounddevice as sd

# --- настройки через переменные окружения -------------------------------
TTS_KEY_NAME  = os.environ.get("SHEPOT_TTS_KEY", "KEY_RIGHTALT")   # клавиша чтения
TTS_ENGINE    = os.environ.get("SHEPOT_TTS_ENGINE", "auto")        # auto / piper / espeak
TTS_VOICE     = os.environ.get("SHEPOT_TTS_VOICE", "ru_RU-irina-medium")
TTS_RATE      = float(os.environ.get("SHEPOT_TTS_RATE", "1.0"))    # 0.5 … 2.0
TTS_SOURCE    = os.environ.get("SHEPOT_TTS_SOURCE", "auto")        # auto / atspi / keys / clipboard
TTS_MAX_CHARS = int(os.environ.get("SHEPOT_TTS_MAX_CHARS", "20000"))
TTS_PRELOAD   = bool(os.environ.get("SHEPOT_TTS_PRELOAD", ""))     # грузить голос при старте
VOICES_DIR    = os.path.expanduser(
    os.environ.get("SHEPOT_TTS_VOICES_DIR", "~/.local/share/shepot/voices"))
# скан-коды для ydotool (Wayland): KEY_LEFTCTRL=29 KEY_LEFTSHIFT=42 KEY_END=107
# KEY_INSERT=110 KEY_LEFT=105. Под X11 те же действия делает xdotool по именам.
KEYS_COPY       = os.environ.get("SHEPOT_TTS_KEYS_COPY", "29:1 110:1 110:0 29:0").split()
KEYS_SELECT_END = os.environ.get("SHEPOT_TTS_KEYS_SELECT_END",
                                 "29:1 42:1 107:1 107:0 42:0 29:0").split()
KEYS_LEFT       = os.environ.get("SHEPOT_TTS_KEYS_LEFT", "105:1 105:0").split()

CONFIG_DIR  = os.path.expanduser("~/.config/shepot")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")   # общий с shepot.py

# Макс. длина блока текста. Пик памяти ONNX и задержка первого слова растут
# с длиной одного предложения: 300 симв. ≈ 17 с речи ≈ +240 МБ на пике.
# На слабом ПК можно уменьшить до 150–200.
BLOCK_MAX     = int(os.environ.get("SHEPOT_TTS_BLOCK", "300"))
PLAY_CHUNK_S  = 0.05    # кусок вывода звука, сек — задаёт скорость реакции на стоп
QUEUE_AHEAD   = 2       # сколько блоков синтезировать вперёд (обратное давление)
RATE_MIN, RATE_MAX = 0.5, 2.0
ATSPI_TIMEOUT_MS = 800    # зависшее приложение не держит нас дольше этого
ATSPI_WALK_MAX   = 3000   # предел узлов при обходе активного окна
ATSPI_WALK_S     = 1.5    # предел времени обхода, сек
GRAB_TIMEOUT_S   = 2.5    # сколько ждём источник текста
CLIP_POLL_S      = 0.05   # шаг опроса буфера обмена

# Русские голоса из официального каталога Piper (все medium, ≈60 МБ каждый).
# Офлайн-список; точный каталог подтягивается с HuggingFace по запросу.
DEFAULT_VOICES = ["ru_RU-irina-medium", "ru_RU-denis-medium",
                  "ru_RU-dmitri-medium", "ru_RU-ruslan-medium"]
VOICES_JSON_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                   "voices.json?download=true")


# --- конфиг (тот же файл, что у shepot.py) -----------------------------

def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def rss_mb():
    """Память процесса (RSS) в МБ — для логов производительности."""
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

    Устроен как ModelManager в shepot.py: выбор пользователя — в конфиге,
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


# --- источники текста ----------------------------------------------------
#
# Каждый источник отвечает на вопрос «что читать?» и возвращает текст,
# None («у меня текста нет — пробуй следующий») или NO_TEXT («текста нет
# точно, дальше не пробовать» — терминал, поле пароля). Порядок задаёт
# SHEPOT_TTS_SOURCE: auto = AT-SPI, затем клавиши+буфер.

NO_TEXT = ""

def call_in_glib(fn, timeout):
    """Выполнить fn() в потоке GLib main loop и вернуть результат.

    libatspi не потокобезопасна, а grab() зовётся из evdev-потока. В демоне
    main loop крутит Gtk.main(), в консольной проверке — свой поток.
    По таймауту возвращает None (приложение зависло — не ждём его).
    """
    from gi.repository import GLib
    done, box = threading.Event(), {}

    def wrapper():
        try:
            box["r"] = fn()
        except Exception as e:      # noqa: BLE001 — ошибку отдаём наверх
            box["e"] = e
        done.set()
        return False                # idle_add: не повторять

    GLib.idle_add(wrapper)
    if not done.wait(timeout):
        print("AT-SPI: таймаут запроса", flush=True)
        return None
    if "e" in box:
        raise box["e"]
    return box.get("r")


class AtspiSource:
    """Текст из объекта в фокусе через шину доступности (как Orca).

    Клавиши не эмулируются, буфер обмена не трогается. Курсор, выделение и
    текст спрашиваем у самого виджета. Фокус отслеживаем событиями
    object:state-changed:focused; если кэш пуст или устарел — обходим
    активное окно (не глубже ATSPI_WALK_MAX узлов).

    Ловушка биндингов: у Atspi.Accessible есть устаревший get_text(),
    который перекрывает Atspi.Text.get_text(start, end). Поэтому методы
    интерфейса Text зовём как функции класса: Atspi.Text.get_text(obj, a, b).
    """
    name = "atspi"

    def __init__(self, own_loop=False):
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi, GLib
        self.Atspi = Atspi
        Atspi.init()
        Atspi.set_timeout(ATSPI_TIMEOUT_MS, 15000)
        self.focused = None
        self.last_caret = None    # (объект, позиция) при последнем grab — для возврата курсора
        self.listener = Atspi.EventListener.new(self._on_focus)
        self.listener.register("object:state-changed:focused")
        if own_loop:                          # консольный режим: свой main loop
            self.loop = GLib.MainLoop()
            threading.Thread(target=self.loop.run, daemon=True).start()

    def _on_focus(self, ev):
        """Событие фокуса (в GLib-потоке): detail1 == 1 — фокус получен."""
        if ev.detail1 == 1:
            self.focused = ev.source

    def grab(self):
        return call_in_glib(self._grab_main, GRAB_TIMEOUT_S)

    def caret_restore(self):
        """Вернуть курсор туда, где он был до grab() (после эмуляции клавиш).

        Нужно для LibreOffice Writer: там ← после Ctrl+Shift+End оставляет
        курсор в конце документа. set_caret_offset возвращает его точно.
        """
        snap = self.last_caret
        if snap is None:
            return False

        def go():
            acc, offset = snap
            try:
                return bool(self.Atspi.Text.set_caret_offset(acc.get_text_iface(), offset))
            except Exception:
                return False
        return call_in_glib(go, 1.5)

    # -- всё ниже выполняется в GLib-потоке --

    def _alive_focused(self):
        """Кэшированный объект, если он ещё существует и всё ещё в фокусе."""
        acc = self.focused
        if acc is None:
            return None
        try:
            if acc.get_state_set().contains(self.Atspi.StateType.FOCUSED):
                return acc
        except Exception:
            pass
        self.focused = None
        return None

    def _find_focused(self):
        """Обход: активное окно → потомок со STATE_FOCUSED (в глубину).
        Ограничен и числом узлов, и временем — зависшее приложение не должно
        держать GTK-поток (в демоне это ещё и поток трея)."""
        A = self.Atspi
        desktop = A.get_desktop(0)
        budget = ATSPI_WALK_MAX
        deadline = time.time() + ATSPI_WALK_S
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            try:
                windows = [app.get_child_at_index(j) for j in range(app.get_child_count())]
            except Exception:
                continue
            for w in windows:
                try:
                    if w is None or not w.get_state_set().contains(A.StateType.ACTIVE):
                        continue
                except Exception:
                    continue
                stack = [w]
                while stack and budget > 0 and time.time() < deadline:
                    a = stack.pop()
                    budget -= 1
                    try:
                        if a.get_state_set().contains(A.StateType.FOCUSED):
                            return a
                        for k in range(min(a.get_child_count(), 200)):
                            c = a.get_child_at_index(k)
                            if c is not None:
                                stack.append(c)
                    except Exception:
                        pass
        return None

    def _grab_main(self):
        A = self.Atspi
        self.last_caret = None
        acc = self._alive_focused() or self._find_focused()
        if acc is None:
            # Firefox/Chrome строят дерево доступности лениво: первый запрос
            # его только «будит». Дать им 300 мс и спросить ещё раз.
            time.sleep(0.3)
            acc = self._find_focused()
        if acc is None:
            print("AT-SPI: объект с фокусом не найден", flush=True)
            return None
        try:
            role = acc.get_role()
            where = f"{acc.get_role_name()} в {acc.get_application().get_name()}"
        except Exception:
            role, where = None, "?"
        # Терминал: VTE на любой запрос Text строит текст всего буфера
        # прокрутки — секунды, и пока занят, не отвечает даже буферу обмена.
        # Пароль: читать вслух нельзя. В обоих случаях дальше не пробуем.
        if role in (A.Role.TERMINAL, A.Role.PASSWORD_TEXT):
            print(f"AT-SPI: {where} — не читаем", flush=True)
            return NO_TEXT
        text_iface = acc.get_text_iface()
        if text_iface is None:
            print(f"AT-SPI: {where} — без интерфейса Text", flush=True)
            return None
        T = A.Text
        n = T.get_character_count(text_iface)
        caret = T.get_caret_offset(text_iface)
        # запомнить курсор — если текст возьмут клавиши, вернём его на место
        if caret >= 0 and role in (A.Role.PARAGRAPH, A.Role.TEXT, A.Role.ENTRY,
                                   A.Role.EDITBAR):
            self.last_caret = (acc, caret)
        # Целиком текст держат только «цельные» виджеты: text (GtkTextView),
        # entry (поля ввода, textarea). LibreOffice Writer и веб-документы
        # дают фокус отдельному абзацу — от курсора до конца документа через
        # них не получить, пусть клавиши выделят Ctrl+Shift+End.
        if role not in (A.Role.TEXT, A.Role.ENTRY, A.Role.EDITBAR):
            print(f"AT-SPI: {where} — не цельный текст, отдаю следующему источнику",
                  flush=True)
            return None
        if n <= 0:
            print(f"AT-SPI: {where} — пусто", flush=True)
            return None
        if T.get_n_selections(text_iface) > 0:
            r = T.get_selection(text_iface, 0)
            if r.end_offset > r.start_offset:
                text = T.get_text(text_iface, r.start_offset, r.end_offset)
                if self._is_plain(text):
                    return text
                print(f"AT-SPI: {where} — выделение из вложенных блоков, "
                      "отдаю следующему источнику", flush=True)
                return None
        caret = max(caret, 0)
        text = T.get_text(text_iface, caret, min(n, caret + TTS_MAX_CHARS))
        if self._is_plain(text):
            return text
        print(f"AT-SPI: {where} — текст из вложенных блоков, "
              "отдаю следующему источнику", flush=True)
        return None

    @staticmethod
    def _is_plain(text):
        """Веб-документ отдаёт вместо дочерних блоков символы U+FFFC —
        такой «текст» читать нельзя, пусть сработает следующий источник."""
        if not text or not text.strip():
            return False
        return text.count("\ufffc") * 4 < len(text)


class Clipboard:
    """Буфер обмена: wl-clipboard под Wayland, xclip под X11.

    Умеет снять снимок содержимого с его MIME-типом и вернуть обратно —
    чтобы после чтения в буфере осталось то, что там было (текст или
    картинка; списки файлов восстанавливаются лишь частично).
    """
    SKIP_TYPES = {"TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS"}

    def __init__(self):
        if shutil.which("wl-copy") and shutil.which("wl-paste"):
            self.kind = "wl"
        elif shutil.which("xclip"):
            self.kind = "xclip"
        else:
            self.kind = None

    def _run(self, args, data=None):
        """Чтение: вывод перехватываем. Запись — нет: wl-copy/xclip оставляют
        фоновый процесс-владелец буфера, он унаследует наши пайпы, и
        capture_output будет ждать их закрытия вечно (проверено — висло)."""
        if data is None and args[-1] != "--clear":
            return subprocess.run(args, capture_output=True, check=False)
        return subprocess.run(args, input=data, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False)

    def read_text(self):
        """Текст из буфера или None (пусто / не текст)."""
        if self.kind == "wl":
            r = self._run(["wl-paste", "--no-newline"])
        elif self.kind == "xclip":
            r = self._run(["xclip", "-selection", "clipboard", "-o"])
        else:
            return None
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", errors="replace")

    def write(self, data, mime=None):
        if self.kind == "wl":
            self._run(["wl-copy"] + (["--type", mime] if mime else []), data)
        elif self.kind == "xclip":
            self._run(["xclip", "-selection", "clipboard", "-i"]
                      + (["-t", mime] if mime else []), data)

    def clear(self):
        if self.kind == "wl":
            self._run(["wl-copy", "--clear"])
        elif self.kind == "xclip":
            self._run(["xclip", "-selection", "clipboard", "-i"], b"")

    def snapshot(self):
        """(mime, bytes) текущего содержимого или None, если буфер пуст."""
        if self.kind == "wl":
            r = self._run(["wl-paste", "--list-types"])
        elif self.kind == "xclip":
            r = self._run(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"])
        else:
            return None
        types = [t for t in r.stdout.decode(errors="replace").split()
                 if t and t not in self.SKIP_TYPES]
        if not types:
            return None
        for pref in ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain",
                     "image/png"):
            if pref in types:
                mime = pref
                break
        else:
            mime = types[0]
        if self.kind == "wl":
            r = self._run(["wl-paste", "--type", mime])
        else:
            r = self._run(["xclip", "-selection", "clipboard", "-o", "-t", mime])
        return (mime, r.stdout) if r.returncode == 0 else None

    def restore(self, snap):
        if snap is None:
            self.clear()
        else:
            self.write(snap[1], snap[0])


class KeysSource:
    """Универсальный источник: эмуляция клавиш + буфер обмена.

    Тот же путь, что вставка текста в shepot.py: под X11 — xdotool, под
    Wayland — ydotool скан-кодами. Алгоритм:
      1. запомнить буфер, положить в него «соль» (уникальную строку);
      2. Ctrl+Insert — если буфер сменился, это выделение, читаем его;
      3. иначе Ctrl+Shift+End (выделить от курсора до конца) + Ctrl+Insert,
         затем ← — снять выделение, курсор возвращается на место;
      4. вернуть в буфер то, что там было.
    Ctrl+Insert, а не Ctrl+C: в терминале Ctrl+C убивает процесс.
    """
    name = "keys"

    def __init__(self):
        self.clip = Clipboard()
        self.moved_caret = False   # последний grab() выделял Ctrl+Shift+End
        wayland = os.environ.get("XDG_SESSION_TYPE", "") == "wayland"
        if not wayland and shutil.which("xdotool"):
            self.tool = "xdotool"
        elif shutil.which("ydotool"):
            self.tool = "ydotool"
        else:
            self.tool = None

    def available(self):
        return bool(self.clip.kind and self.tool)

    def _press(self, xdo_name, codes):
        if self.tool == "xdotool":
            subprocess.run(["xdotool", "key", "--clearmodifiers", xdo_name], check=False)
        else:
            subprocess.run(["ydotool", "key", "--key-delay", "25"] + codes, check=False)

    def _wait_clip(self, salt, timeout):
        """Ждать, пока буфер станет отличен от соли; вернуть новый текст."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = self.clip.read_text()
            if cur is not None and cur != salt and cur.strip():
                return cur
            time.sleep(CLIP_POLL_S)
        return None

    def grab(self):
        self.moved_caret = False
        snap = self.clip.snapshot()
        salt = f"\u2063shepot-tts-{uuid.uuid4().hex}"
        self.clip.write(salt.encode())
        # wl-copy асинхронный (150–250 мс) — дождаться, что соль в буфере
        deadline = time.time() + 3.0
        while self.clip.read_text() != salt:
            if time.time() > deadline:
                print("буфер обмена не отвечает", flush=True)
                self.clip.restore(snap)
                return None
            time.sleep(CLIP_POLL_S)
        try:
            self._press("ctrl+Insert", KEYS_COPY)
            text = self._wait_clip(salt, 0.4)             # было выделение?
            if text is None:
                self._press("ctrl+shift+End", KEYS_SELECT_END)
                self._press("ctrl+Insert", KEYS_COPY)
                text = self._wait_clip(salt, 0.8)         # от курсора до конца
                if text is not None:
                    self.moved_caret = True
                    self._press("Left", KEYS_LEFT)        # снять выделение
            return text
        finally:
            self.clip.restore(snap)


class ClipboardSource:
    """Режим SHEPOT_TTS_SOURCE=clipboard: читать то, что пользователь скопировал сам."""
    name = "clipboard"

    def __init__(self):
        self.clip = Clipboard()

    def grab(self):
        return self.clip.read_text()


def make_sources(mode, own_loop=False):
    """Источники в порядке опроса по SHEPOT_TTS_SOURCE."""
    sources = []
    if mode in ("auto", "atspi"):
        try:
            sources.append(AtspiSource(own_loop))
        except Exception as e:
            print(f"AT-SPI недоступен: {e}", flush=True)
    if mode in ("auto", "keys"):
        k = KeysSource()
        if k.available():
            sources.append(k)
        else:
            print("клавиши+буфер недоступны: нужен ydotool/xdotool и wl-clipboard/xclip",
                  flush=True)
    if mode == "clipboard":
        sources.append(ClipboardSource())
    return sources


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
              из фоновых потоков, поэтому Tray обязан оборачивать их в
              GLib.idle_add.
    config  — общий словарь конфига (тот же объект, что у shepot.py).
    """

    def __init__(self, config, ui=None, engine_kind=None, own_glib_loop=False,
                 source_mode=None):
        self.config, self.ui = config, ui or ConsoleUI()
        self.rate = clamp_rate(config.get("tts_rate", TTS_RATE))
        self.engine_kind = engine_kind or config.get("tts_engine") or TTS_ENGINE
        self.voices = VoiceManager(config, self.ui.set_status)
        self.sources = make_sources(source_mode or TTS_SOURCE, own_glib_loop)
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

        Если текст взяли клавиши после того, как AT-SPI видел курсор, —
        вернуть курсор на место через AT-SPI (← в Writer этого не делает).
        """
        atspi = next((s for s in self.sources if isinstance(s, AtspiSource)), None)
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
                if atspi and isinstance(src, KeysSource) and src.moved_caret:
                    if atspi.caret_restore():
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

    config = load_config()
    reader = Reader(config, engine_kind=a.engine, own_glib_loop=(a.grab or a.read),
                    source_mode=a.source)
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
        try:
            reader.start(text)
            while reader.reading:
                time.sleep(0.1)
        except KeyboardInterrupt:
            reader.stop()
        return

    text = a.say
    if a.say_file:
        with open(a.say_file, encoding="utf-8") as f:
            text = f.read()
    if not text:
        ap.print_help()
        return

    try:
        reader.start(text)
        while reader.reading:
            time.sleep(0.1)
    except KeyboardInterrupt:
        reader.stop()
        print("\nостановлено")


if __name__ == "__main__":
    main()
