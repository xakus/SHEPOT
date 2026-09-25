#!/usr/bin/env python3
"""Push-to-talk диктовка с индикатором в системном трее.

Длинные записи (10+ минут) не теряются: во время записи буфер режется на
чанки по ~SHEPOT_CHUNK секунд в самом тихом месте (чтобы не разрубить слово)
и распознаётся параллельно с записью. При отпускании клавиши остаётся
дораспознать только хвост, после чего весь текст склеивается и вставляется
одним куском. Каждый распознанный чанк сразу дописывается в SHEPOT_LOG.

Модель выбирается из меню в трее: список актуальных моделей faster-whisper
(+ обновление с HuggingFace), автоскачивание с прогрессом, размеры на диске,
удаление скачанных, добавление локальных. Выбор сохраняется в конфиге.

Чтение вслух (модуль shepot_reader.py, лежит рядом): одиночное нажатие
SHEPOT_TTS_KEY (правый Alt) читает выделенный текст или текст от курсора до
конца документа; повторное нажатие — стоп. Диктовка и чтение независимы;
единственная связь — нажатие клавиши диктовки останавливает чтение, чтобы
микрофон не записал колонки.
"""

import os, sys, time, gc, json, shutil, threading, subprocess, queue, datetime
import urllib.request
from selectors import DefaultSelector, EVENT_READ

import numpy as np
import sounddevice as sd
from evdev import InputDevice, ecodes, list_devices

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppInd
except Exception:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppInd

# Чтение вслух — отдельный модуль рядом с этим файлом. Без него демон
# работает как раньше (только диктовка).
try:
    import shepot_reader
except ImportError as e:
    shepot_reader = None
    print("модуль чтения shepot_reader недоступен:", e, flush=True)

# --- настройки через переменные окружения -------------------------------
HOTKEY_NAME  = os.environ.get("SHEPOT_KEY", "KEY_RIGHTCTRL")
HOTKEY       = getattr(ecodes, HOTKEY_NAME)
LANGUAGE     = os.environ.get("SHEPOT_LANG", "ru")
COMPUTE_TYPE = os.environ.get("SHEPOT_COMPUTE", "int8")   # Pascal: только int8
DEVICE       = os.environ.get("SHEPOT_DEVICE", "auto")   # auto / cuda / cpu; меню трея важнее
INIT_PROMPT  = os.environ.get("SHEPOT_PROMPT", "")
CLIP_ONLY    = bool(os.environ.get("SHEPOT_CLIPBOARD_ONLY", ""))
CHUNK_MAX    = float(os.environ.get("SHEPOT_CHUNK", "30"))  # макс. длина чанка, сек
LOG_PATH     = os.path.expanduser(os.environ.get("SHEPOT_LOG", "~/shepot-log.txt"))

# Варианты устройства для распознавания и их подписи в меню трея.
# auto — GPU, если он есть и модель на нём загрузилась, иначе CPU.
DEVICE_LABELS = {"auto": "Авто", "cuda": "GPU (CUDA)", "cpu": "CPU"}

TTS_KEY_NAME = shepot_reader.TTS_KEY_NAME if shepot_reader else None   # клавиша чтения
TTS_KEY      = getattr(ecodes, TTS_KEY_NAME) if TTS_KEY_NAME else None
TTS_RATES    = [0.8, 1.0, 1.2, 1.5, 2.0]        # варианты скорости в меню

# Клавиши, которые можно назначить на диктовку/чтение из меню. Только редкие
# и безопасные: буквы/цифры/Enter сюда не входят, чтобы нельзя было сломать
# набор текста. (имя из evdev.ecodes, человекочитаемый ярлык)
KEY_CANDIDATES = [
    ("KEY_RIGHTCTRL",  "Правый Ctrl"),
    ("KEY_RIGHTALT",   "Правый Alt"),
    ("KEY_RIGHTSHIFT", "Правый Shift"),
    ("KEY_RIGHTMETA",  "Правый Super (Win)"),
    ("KEY_COMPOSE",    "Menu (клавиша меню)"),
    ("KEY_PAUSE",      "Pause / Break"),
    ("KEY_SCROLLLOCK", "Scroll Lock"),
    ("KEY_CAPSLOCK",   "Caps Lock"),
    ("KEY_INSERT",     "Insert"),
]
KEY_LABELS = dict(KEY_CANDIDATES)


def key_label(name):
    """Человекочитаемое имя клавиши для меню (иначе — как в evdev)."""
    return KEY_LABELS.get(name, name.replace("KEY_", ""))

CONFIG_DIR  = os.path.expanduser("~/.config/shepot")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
HF_CACHE    = os.path.expanduser(
    os.environ.get("HF_HUB_CACHE", "~/.cache/huggingface/hub"))

SAMPLE_RATE = 16000
MIN_SECONDS = 0.4                       # короче — считаем случайным нажатием
CHUNK_MIN   = max(5.0, CHUNK_MAX * 2 / 3)  # раньше этой границы чанк не режем
TYPE_DELAY  = 0.06

ICON_LOAD = "content-loading-symbolic"
ICON_IDLE = "audio-input-microphone-symbolic"
ICON_REC  = "media-record-symbolic"
ICON_OFF  = "microphone-sensitivity-muted-symbolic"
ICON_READ = "audio-speakers-symbolic"

# Приблизительный размер скачивания моделей (МБ) — для тех, что ещё не
# скачаны. Точные размеры подтягиваются с HuggingFace по «Обновить список».
APPROX_MB = {
    "tiny": 75, "tiny.en": 75, "base": 145, "base.en": 145,
    "small": 484, "small.en": 484, "distil-small.en": 332,
    "medium": 1530, "medium.en": 1530, "distil-medium.en": 789,
    "large-v1": 3100, "large-v2": 3100, "large-v3": 3100, "large": 3100,
    "distil-large-v2": 1510, "distil-large-v3": 1510,
    "large-v3-turbo": 1620, "turbo": 1620,
}


# --- конфиг --------------------------------------------------------------

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


# --- утилиты для моделей -------------------------------------------------

def human_size(n):
    """Байты — в человекочитаемый вид."""
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} ГБ"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} МБ"
    return f"{n / 1024:.0f} КБ"


def dir_size(path):
    """Размер папки; симлинки не считаем — в кеше HuggingFace snapshots
    ссылаются на blobs, иначе каждый файл посчитается дважды."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            try:
                if not os.path.islink(p):
                    total += os.path.getsize(p)
            except OSError:
                pass
    return total


def repo_for(name):
    """Имя модели -> repo id на HuggingFace (откуда качает faster-whisper)."""
    if "/" in name:
        return name
    try:
        from faster_whisper.utils import _MODELS
        if name in _MODELS:
            return _MODELS[name]
    except Exception:
        pass
    return f"Systran/faster-whisper-{name}"


def model_cache_dir(name):
    """Папка модели в кеше HuggingFace (существует только у скачанных)."""
    return os.path.join(HF_CACHE, "models--" + repo_for(name).replace("/", "--"))


def is_downloaded(name):
    d = model_cache_dir(name)
    if not os.path.isdir(d):
        return False
    for root, _, files in os.walk(os.path.join(d, "snapshots")):
        if "model.bin" in files:
            return True
    return False


# --- аудио ---------------------------------------------------------------

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


def make_typer():
    """Выбрать способ вставки текста в позицию курсора по окружению."""
    wayland = os.environ.get("XDG_SESSION_TYPE", "") == "wayland"

    if shutil.which("wl-copy"):
        copy_cmd = ["wl-copy"]
    elif shutil.which("xclip"):
        copy_cmd = ["xclip", "-selection", "clipboard"]
    else:
        copy_cmd = None

    if CLIP_ONLY and copy_cmd:
        return lambda t: subprocess.run(copy_cmd, input=t.encode(), check=False)

    if not wayland and shutil.which("xdotool"):
        return lambda t: subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "1", "--", t], check=False)

    # Ctrl+V по скан-кодам: KEY_LEFTCTRL=29, KEY_V=47
    combo = os.environ.get("SHEPOT_PASTE", "29:1 47:1 47:0 29:0").split()

    if copy_cmd and shutil.which("ydotool"):
        can_verify = copy_cmd[0] == "wl-copy" and shutil.which("wl-paste")

        def paste(t):
            subprocess.run(copy_cmd, input=t.encode(), check=False)
            # wl-copy асинхронный: буфер меняется через 150-250 мс. Жать
            # Ctrl+V раньше — вставить СТАРОЕ содержимое. Ждём подтверждения.
            deadline = time.time() + 3.0
            while can_verify and time.time() < deadline:
                r = subprocess.run(["wl-paste", "--no-newline"],
                                   capture_output=True)
                if r.stdout.decode(errors="replace") == t:
                    break
                time.sleep(0.05)
            else:
                time.sleep(0.3)   # нет wl-paste — хотя бы щедрая пауза
            time.sleep(0.05)      # буфер наш; дать композитору дорисоваться
            subprocess.run(["ydotool", "key", "--key-delay", "25"] + combo,
                           check=False)
            print(f"ВСТАВКА: {len(t)} символов", flush=True)
        return paste

    if copy_cmd:
        return lambda t: subprocess.run(copy_cmd, input=t.encode(), check=False)

    return lambda t: print(t, flush=True)


def find_keyboards(key):
    """Найти все /dev/input-устройства, у которых есть нужная клавиша."""
    devs = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except OSError:
            continue
        if key in d.capabilities().get(ecodes.EV_KEY, []):
            devs.append(d)
    return devs


def find_all_keyboards():
    """Все физические клавиатуры: устройства с буквой A или одной из
    клавиш-кандидатов (KEY_CANDIDATES). Виртуальное устройство ydotool
    исключаем — иначе демон ловил бы собственные синтетические нажатия
    (вставку диктовки, Ctrl+Insert при чтении).

    Регистрируем сразу все клавиатуры, а нужную клавишу сверяем в цикле —
    тогда смена горячей клавиши не требует перерегистрации устройств.
    """
    cand_codes = {getattr(ecodes, n) for n, _ in KEY_CANDIDATES}
    devs = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except OSError:
            continue
        if "ydotoold" in d.name:
            continue
        keys = set(d.capabilities().get(ecodes.EV_KEY, []))
        if ecodes.KEY_A in keys or (cand_codes & keys):
            devs.append(d)
    return devs


def available_hotkeys(keyboards):
    """Какие клавиши-кандидаты реально присутствуют хотя бы на одной из
    клавиатур — только их показываем в меню выбора."""
    have = set()
    for d in keyboards:
        have |= set(d.capabilities().get(ecodes.EV_KEY, []))
    return [(n, lbl) for n, lbl in KEY_CANDIDATES if getattr(ecodes, n) in have]


# --- менеджер моделей ----------------------------------------------------

class ModelManager:
    """Каталог моделей: что доступно, что скачано, сколько весит.

    Хранит выбор пользователя в конфиге, умеет обновлять список и точные
    размеры с HuggingFace, удалять скачанные и подключать локальные папки.
    """

    def __init__(self, tray, jobs, config):
        self.tray, self.jobs, self.config = tray, jobs, config
        self.current = config.get("model") or os.environ.get("SHEPOT_MODEL", "large-v3")
        self.remote_names = []    # имена, найденные на HuggingFace
        self.remote_sizes = {}    # точные размеры скачивания с HuggingFace

    def known_names(self):
        """Все имена моделей: из faster-whisper + найденные на сайте."""
        try:
            from faster_whisper.utils import available_models
            names = list(available_models())
        except Exception:
            names = list(APPROX_MB)
        for n in self.remote_names:
            if n not in names:
                names.append(n)
        return names

    def local_models(self):
        return self.config.get("local_models", {})

    def path_or_name(self, name):
        """Что передавать в WhisperModel: путь локальной папки или имя/репо."""
        return self.local_models().get(name, name)

    def catalog(self):
        """Список записей для меню: имя, скачана ли, размер, можно ли удалить."""
        entries = []
        for name in self.known_names():
            dl = is_downloaded(name)
            if dl:
                size = human_size(dir_size(model_cache_dir(name)))
            elif name in self.remote_sizes:
                size = human_size(self.remote_sizes[name])
            elif name in APPROX_MB:
                size = "~" + human_size(APPROX_MB[name] * (1 << 20))
            else:
                size = "?"
            entries.append({"name": name, "downloaded": dl, "size": size,
                            "local": False,
                            "removable": dl and name != self.current})
        for name, path in self.local_models().items():
            ok = os.path.isdir(path)
            entries.append({"name": name, "downloaded": ok,
                            "size": human_size(dir_size(path)) if ok else "нет папки",
                            "local": True, "removable": False})
        return entries

    def push(self):
        """Перестроить подменю моделей в трее (можно звать из любого потока)."""
        entries, cur = self.catalog(), self.current
        GLib.idle_add(self.tray.set_model_catalog, entries, cur, self)

    def select(self, name):
        """Пользователь выбрал модель в меню — переключаемся (с автоскачкой)."""
        if name != self.current:
            self.jobs.put(("switch", name))

    def set_current(self, name):
        """Переключение удалось — запоминаем выбор в конфиге."""
        self.current = name
        self.config["model"] = name
        save_config(self.config)
        self.push()

    def delete(self, name):
        """Удалить скачанную модель из кеша (в фоне: до нескольких ГБ)."""
        d = model_cache_dir(name)

        def go():
            shutil.rmtree(d, ignore_errors=True)
            self.tray.set_status(f"Модель {name} удалена")
            self.push()
        threading.Thread(target=go, daemon=True).start()

    def add_local(self, path):
        """Подключить уже скачанную модель из произвольной папки."""
        if not os.path.isfile(os.path.join(path, "model.bin")):
            self.tray.set_status("В папке нет model.bin — это не модель CT2")
            return
        name = os.path.basename(os.path.normpath(path))
        self.config.setdefault("local_models", {})[name] = path
        save_config(self.config)
        self.push()
        self.tray.set_status(f"Добавлена локальная модель {name}")

    def refresh_remote(self):
        """Подтянуть с HuggingFace актуальный список моделей и точные размеры."""
        def go():
            self.tray.set_status("Обновление списка моделей…")
            try:
                url = ("https://huggingface.co/api/models"
                       "?author=Systran&search=faster-whisper&limit=100")
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = json.load(r)
                names = [m["id"].split("faster-whisper-")[-1]
                         for m in data if "faster-whisper-" in m["id"]]
                self.remote_names = names
                # точные размеры — только там, где нет ни факта, ни таблицы
                for n in names:
                    if is_downloaded(n) or n in APPROX_MB or n in self.remote_sizes:
                        continue
                    try:
                        u = (f"https://huggingface.co/api/models/{repo_for(n)}"
                             "?files_metadata=true")
                        with urllib.request.urlopen(u, timeout=15) as r:
                            info = json.load(r)
                        self.remote_sizes[n] = sum(
                            s.get("size") or 0 for s in info.get("siblings", []))
                    except Exception:
                        pass
                self.tray.set_status(f"Список обновлён: {len(names)} моделей на сайте")
            except Exception as e:
                self.tray.set_status(f"Сеть недоступна: {e}")
            self.push()
        threading.Thread(target=go, daemon=True).start()


def download_model(name, tray):
    """Скачать модель с прогрессом в строке статуса трея."""
    from huggingface_hub import snapshot_download
    from tqdm import tqdm

    class TrayTqdm(tqdm):
        # прогресс показываем по крупным файлам (model.bin), мелкие не мигают
        def update(self, n=1):
            super().update(n)
            if self.total and self.total > 50 * (1 << 20):
                pct = 100 * self.n / self.total
                tray.set_status(f"Скачивание {name}: {pct:.0f}%")

    snapshot_download(repo_for(name), tqdm_class=TrayTqdm)


# --- трей ----------------------------------------------------------------

class Tray:
    """Иконка в панели GNOME. Все обновления UI — через GLib.idle_add."""

    def __init__(self, config=None):
        self.config = config if config is not None else {}
        self.enabled = True
        self.reader = None            # Reader из shepot_reader; ставится в main()
        self.tts_enabled = bool(self.config.get("tts_enabled", True))
        self._updating_menu = False
        self.available_keys = list(KEY_CANDIDATES)   # уточняется worker'ом
        self.jobs = None              # очередь транскрайбера; ставится worker'ом

        # устройство распознавания: конфиг → env → auto
        self.device_pref = self.config.get("device") or DEVICE
        if self.device_pref not in DEVICE_LABELS:
            self.device_pref = "auto"

        # горячие клавиши: конфиг → env → умолчание. worker читает эти поля
        # в каждой итерации, поэтому смена применяется без перезапуска.
        self.dictate_key_name = self.config.get("hotkey") or HOTKEY_NAME
        self.dictate_key = getattr(ecodes, self.dictate_key_name, HOTKEY)
        if shepot_reader:
            self.read_key_name = self.config.get("tts_key") or TTS_KEY_NAME
            self.read_key = getattr(ecodes, self.read_key_name, TTS_KEY)
        else:
            self.read_key_name, self.read_key = None, None
        self.ind = AppInd.Indicator.new(
            "shepot", ICON_LOAD, AppInd.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_title("SHEPOT")   # имя программы для панели/подсказок
        self.ind.set_status(AppInd.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self.status = Gtk.MenuItem(label="Загрузка модели…")
        self.status.set_sensitive(False)
        self.menu.append(self.status)

        self.last = Gtk.MenuItem(label="—")
        self.last.set_sensitive(False)
        self.menu.append(self.last)

        self.menu.append(Gtk.SeparatorMenuItem())

        # подменю выбора модели; наполняется через set_model_catalog()
        self.model_item = Gtk.MenuItem(label="Модель")
        self.model_menu = Gtk.Menu()
        wait = Gtk.MenuItem(label="Загрузка…")
        wait.set_sensitive(False)
        self.model_menu.append(wait)
        self.model_item.set_submenu(self.model_menu)
        self.menu.append(self.model_item)

        # подменю выбора устройства (Авто / GPU / CPU); наполняется set_device_menu()
        self.device_item = Gtk.MenuItem(label=f"Устройство: {DEVICE_LABELS[self.device_pref]}")
        self.device_item.set_submenu(Gtk.Menu())
        self.menu.append(self.device_item)

        # чтение вслух: голос, скорость, движок; наполняются в set_voice_catalog()
        if shepot_reader:
            self.voice_item = Gtk.MenuItem(label="Голос")
            self.voice_menu = Gtk.Menu()
            self.voice_item.set_submenu(self.voice_menu)
            self.menu.append(self.voice_item)
            self.rate_item = Gtk.MenuItem(label="Скорость")
            self.rate_item.set_submenu(Gtk.Menu())
            self.menu.append(self.rate_item)
            self.engine_item = Gtk.MenuItem(label="Движок")
            self.engine_item.set_submenu(Gtk.Menu())
            self.menu.append(self.engine_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        self.toggle = Gtk.CheckMenuItem(
            label=f"Слушать ({key_label(self.dictate_key_name)})")
        self.toggle.set_active(True)
        self.toggle.connect("toggled", self._on_toggle)
        self.menu.append(self.toggle)

        # подменю выбора клавиши диктовки; наполняется set_key_catalog()
        self.dictate_key_item = Gtk.MenuItem(label="Клавиша диктовки")
        self.dictate_key_item.set_submenu(Gtk.Menu())
        self.menu.append(self.dictate_key_item)

        if shepot_reader:
            self.tts_toggle = Gtk.CheckMenuItem(
                label=f"Читать ({key_label(self.read_key_name)})")
            self.tts_toggle.set_active(self.tts_enabled)
            self.tts_toggle.connect("toggled", self._on_tts_toggle)
            self.menu.append(self.tts_toggle)
            self.read_key_item = Gtk.MenuItem(label="Клавиша чтения")
            self.read_key_item.set_submenu(Gtk.Menu())
            self.menu.append(self.read_key_item)
            self.stop_item = Gtk.MenuItem(label="Стоп чтение")
            self.stop_item.set_sensitive(False)
            self.stop_item.connect("activate", lambda *_: self.reader and
                                   threading.Thread(target=self.reader.stop,
                                                    daemon=True).start())
            self.menu.append(self.stop_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        q = Gtk.MenuItem(label="Выход")
        q.connect("activate", lambda *_: self.quit())
        self.menu.append(q)

        self.menu.show_all()
        self.ind.set_menu(self.menu)

    # -- подменю моделей (вызывается только из GTK-потока через idle_add) --

    def set_model_catalog(self, entries, current, manager):
        self._updating_menu = True
        for child in self.model_menu.get_children():
            self.model_menu.remove(child)

        group = None
        for e in entries:
            mark = "скачана" if e["downloaded"] else "скачать"
            if e["local"]:
                mark = "локальная"
            item = Gtk.RadioMenuItem.new_with_label_from_widget(
                group, f'{e["name"]} — {e["size"]} ({mark})')
            group = group or item
            item.set_active(e["name"] == current)
            item.connect("toggled", self._on_model_pick, e["name"], manager)
            self.model_menu.append(item)

        self.model_menu.append(Gtk.SeparatorMenuItem())

        removable = [e for e in entries if e["removable"]]
        rm_item = Gtk.MenuItem(label="Удалить скачанную")
        rm_menu = Gtk.Menu()
        for e in removable:
            it = Gtk.MenuItem(label=f'{e["name"]} — {e["size"]}')
            it.connect("activate", self._on_model_delete, e["name"], manager)
            rm_menu.append(it)
        rm_item.set_submenu(rm_menu)
        rm_item.set_sensitive(bool(removable))
        self.model_menu.append(rm_item)

        add = Gtk.MenuItem(label="Добавить локальную модель…")
        add.connect("activate", self._on_model_add, manager)
        self.model_menu.append(add)

        folder = Gtk.MenuItem(label="Открыть папку моделей")
        folder.connect("activate", lambda *_: subprocess.Popen(["xdg-open", HF_CACHE]))
        self.model_menu.append(folder)

        refresh = Gtk.MenuItem(label="Обновить список (HuggingFace)")
        refresh.connect("activate", lambda *_: manager.refresh_remote())
        self.model_menu.append(refresh)

        self.model_item.set_label(f"Модель: {current}")
        self.model_menu.show_all()
        self._updating_menu = False
        return False   # для GLib.idle_add — не повторять

    def _on_model_pick(self, item, name, manager):
        # RadioMenuItem шлёт toggled и при снятии, и при перестройке меню
        if self._updating_menu or not item.get_active():
            return
        manager.select(name)

    # -- подменю устройства (только из GTK-потока через idle_add) --

    def set_device_menu(self, pref, actual, has_gpu):
        """Перестроить подменю «Устройство».

        pref — выбор пользователя (auto/cuda/cpu), actual — где модель
        реально работает (cuda/cpu/None), has_gpu — есть ли видеокарта.
        """
        self._updating_menu = True
        menu = Gtk.Menu()
        group = None
        for key, label in DEVICE_LABELS.items():
            if key == "cuda" and not has_gpu:
                label += " — не найден"
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or item
            item.set_active(key == pref)
            item.set_sensitive(key != "cuda" or has_gpu)
            item.connect("toggled", self._on_device_pick, key)
            menu.append(item)
        self.device_item.set_submenu(menu)
        # в режиме «Авто» или при откате (выбран GPU, работает CPU) — показать, где реально
        tail = ""
        if actual and (pref == "auto" or actual != pref):
            tail = f" → {'GPU' if actual == 'cuda' else 'CPU'}"
        self.device_item.set_label(f"Устройство: {DEVICE_LABELS[pref]}{tail}")
        menu.show_all()
        self._updating_menu = False
        return False   # для GLib.idle_add — не повторять

    def _on_device_pick(self, item, pref):
        if self._updating_menu or not item.get_active():
            return
        if self.jobs:
            self.jobs.put(("device", pref))   # перезагрузку модели делает транскрайбер

    def _on_model_delete(self, _item, name, manager):
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO,
                                text=f"Удалить модель {name} с диска?")
        dlg.format_secondary_text(model_cache_dir(name))
        ok = dlg.run() == Gtk.ResponseType.YES
        dlg.destroy()
        if ok:
            manager.delete(name)

    def _on_model_add(self, _item, manager):
        dlg = Gtk.FileChooserDialog(title="Папка с моделью CTranslate2 (model.bin)",
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("Отмена", Gtk.ResponseType.CANCEL,
                        "Добавить", Gtk.ResponseType.OK)
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.OK else None
        dlg.destroy()
        if path:
            manager.add_local(path)

    # -- меню чтения (только из GTK-потока через idle_add) --

    def set_voice_catalog(self, entries, current, rate, engine_kind):
        """Перестроить подменю «Голос», «Скорость», «Движок»."""
        self._updating_menu = True
        for child in self.voice_menu.get_children():
            self.voice_menu.remove(child)

        group = None
        for e in entries:
            mark = "скачан" if e["downloaded"] else "скачать"
            item = Gtk.RadioMenuItem.new_with_label_from_widget(
                group, f'{e["name"]} — {e["size"]} ({mark})')
            group = group or item
            item.set_active(e["name"] == current)
            item.connect("toggled", self._on_voice_pick, e["name"])
            self.voice_menu.append(item)

        self.voice_menu.append(Gtk.SeparatorMenuItem())
        removable = [e for e in entries if e["removable"]]
        rm_item = Gtk.MenuItem(label="Удалить скачанный")
        rm_menu = Gtk.Menu()
        for e in removable:
            it = Gtk.MenuItem(label=f'{e["name"]} — {e["size"]}')
            it.connect("activate", self._on_voice_delete, e["name"])
            rm_menu.append(it)
        rm_item.set_submenu(rm_menu)
        rm_item.set_sensitive(bool(removable))
        self.voice_menu.append(rm_item)

        folder = Gtk.MenuItem(label="Открыть папку голосов")
        folder.connect("activate", lambda *_: subprocess.Popen(
            ["xdg-open", shepot_reader.VOICES_DIR]))
        self.voice_menu.append(folder)
        refresh = Gtk.MenuItem(label="Обновить список (HuggingFace)")
        refresh.connect("activate", lambda *_: self.reader.refresh_voices())
        self.voice_menu.append(refresh)

        # «ru_RU-irina-medium» → «irina»
        parts = current.split("-")
        self.voice_item.set_label(f"Голос: {parts[1] if len(parts) == 3 else current}")
        self.voice_menu.show_all()

        # скорость
        rate_menu = Gtk.Menu()
        group = None
        for r in TTS_RATES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, f"{r:g}×")
            group = group or item
            item.set_active(abs(r - rate) < 0.01)
            item.connect("toggled", self._on_rate_pick, r)
            rate_menu.append(item)
        self.rate_item.set_submenu(rate_menu)
        self.rate_item.set_label(f"Скорость: {rate:g}×")
        rate_menu.show_all()

        # движок
        engine_menu = Gtk.Menu()
        group = None
        cur = "espeak" if engine_kind == "espeak" else "piper"
        for kind, label in (("piper", "Piper (нейросеть)"), ("espeak", "espeak-ng (робот)")):
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or item
            item.set_active(kind == cur)
            item.connect("toggled", self._on_engine_pick, kind)
            engine_menu.append(item)
        self.engine_item.set_submenu(engine_menu)
        self.engine_item.set_label("Движок: " + ("espeak-ng" if cur == "espeak" else "Piper"))
        engine_menu.show_all()

        self._updating_menu = False
        return False   # для GLib.idle_add — не повторять

    def _on_voice_pick(self, item, name):
        if self._updating_menu or not item.get_active():
            return
        self.reader.select_voice(name)

    def _on_voice_delete(self, _item, name):
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO,
                                text=f"Удалить голос {name} с диска?")
        dlg.format_secondary_text(shepot_reader.VOICES_DIR)
        ok = dlg.run() == Gtk.ResponseType.YES
        dlg.destroy()
        if ok:
            self.reader.delete_voice(name)

    def _on_rate_pick(self, item, rate):
        if self._updating_menu or not item.get_active():
            return
        self.reader.set_rate(rate)
        self.rate_item.set_label(f"Скорость: {rate:g}×")

    def _on_engine_pick(self, item, kind):
        if self._updating_menu or not item.get_active():
            return
        threading.Thread(target=self.reader.set_engine, args=(kind,), daemon=True).start()

    def _on_tts_toggle(self, item):
        self.tts_enabled = item.get_active()
        if self.reader:
            self.reader.config["tts_enabled"] = self.tts_enabled
            shepot_reader.save_config(self.reader.config)
            if not self.tts_enabled:
                threading.Thread(target=self.reader.stop, daemon=True).start()

    def set_reading(self, on):
        """Reader сообщает: чтение началось/кончилось (из фонового потока)."""
        self.icon(ICON_READ if on else (ICON_IDLE if self.enabled else ICON_OFF))
        GLib.idle_add(self.stop_item.set_sensitive, on)

    # -- меню выбора клавиш (только из GTK-потока) --

    def set_key_catalog(self, available):
        """Сохранить список доступных клавиш (от worker) и построить подменю."""
        self.available_keys = available or list(KEY_CANDIDATES)
        self._rebuild_key_menus()
        return False   # для GLib.idle_add — не повторять

    def _rebuild_key_menus(self):
        """Перестроить подменю «Клавиша диктовки»/«Клавиша чтения» и ярлыки."""
        self._updating_menu = True
        self._fill_key_menu(self.dictate_key_item, self.dictate_key_name,
                            self._on_dictate_key_pick)
        self.dictate_key_item.set_label(
            f"Клавиша диктовки: {key_label(self.dictate_key_name)}")
        self.toggle.set_label(f"Слушать ({key_label(self.dictate_key_name)})")
        if shepot_reader:
            self._fill_key_menu(self.read_key_item, self.read_key_name,
                                self._on_read_key_pick)
            self.read_key_item.set_label(
                f"Клавиша чтения: {key_label(self.read_key_name)}")
            self.tts_toggle.set_label(f"Читать ({key_label(self.read_key_name)})")
        self._updating_menu = False

    def _fill_key_menu(self, item, current, handler):
        """Наполнить одно подменю radio-пунктами доступных клавиш."""
        menu = Gtk.Menu()
        group = None
        for name, label in self.available_keys:
            radio = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or radio
            radio.set_active(name == current)
            radio.connect("toggled", handler, name)
            menu.append(radio)
        item.set_submenu(menu)
        menu.show_all()

    def _on_dictate_key_pick(self, radio, name):
        if self._updating_menu or not radio.get_active():
            return
        self.set_dictate_key(name)

    def _on_read_key_pick(self, radio, name):
        if self._updating_menu or not radio.get_active():
            return
        self.set_read_key(name)

    def set_dictate_key(self, name):
        """Назначить клавишу диктовки (worker подхватит сразу)."""
        if name == self.read_key_name:
            self.set_status(f"{key_label(name)} уже занята чтением")
            self._rebuild_key_menus()      # вернуть отметку на прежнюю
            return
        self.dictate_key_name = name
        self.dictate_key = getattr(ecodes, name)
        self.config["hotkey"] = name
        save_config(self.config)
        self._rebuild_key_menus()
        self.set_status(f"Клавиша диктовки: {key_label(name)}")

    def set_read_key(self, name):
        """Назначить клавишу чтения (worker подхватит сразу)."""
        if name == self.dictate_key_name:
            self.set_status(f"{key_label(name)} уже занята диктовкой")
            self._rebuild_key_menus()
            return
        self.read_key_name = name
        self.read_key = getattr(ecodes, name)
        self.config["tts_key"] = name
        save_config(self.config)
        self._rebuild_key_menus()
        self.set_status(f"Клавиша чтения: {key_label(name)}")

    # -- прочее UI --

    def _on_toggle(self, item):
        self.enabled = item.get_active()
        self.icon(ICON_IDLE if self.enabled else ICON_OFF)
        self.set_status("Готов" if self.enabled else "Выключено")

    def icon(self, name):
        GLib.idle_add(self.ind.set_icon_full, name, "shepot")

    def set_status(self, text):
        GLib.idle_add(self.status.set_label, text)

    def set_last(self, text):
        t = (text[:60] + "…") if len(text) > 60 else text
        GLib.idle_add(self.last.set_label, t or "—")

    def quit(self):
        Gtk.main_quit()
        os._exit(0)


# --- основной рабочий поток ----------------------------------------------

def worker(tray, config, reader=None):
    import ctranslate2                   # движок faster-whisper: проверка наличия GPU
    from faster_whisper import WhisperModel

    # Регистрируем все клавиатуры сразу, а конкретную клавишу сверяем в цикле
    # с tray.dictate_key / tray.read_key — тогда смена клавиши из меню
    # применяется без перерегистрации устройств и перезапуска.
    keyboards = find_all_keyboards()
    if not keyboards:
        tray.set_status("Нет доступа к /dev/input — перелогинься")
        tray.icon(ICON_OFF)
        return

    rec, typer = Recorder(), make_typer()
    print("сессия:", os.environ.get("XDG_SESSION_TYPE"), flush=True)

    sel = DefaultSelector()
    for d in keyboards:
        sel.register(d, EVENT_READ)

    # какие клавиши реально есть на клавиатурах — их покажет меню выбора
    GLib.idle_add(tray.set_key_catalog, available_hotkeys(keyboards))

    # Очередь заданий транскрайбера. Один поток владеет моделью, поэтому
    # порядок чанков сохранён, а смена модели не требует блокировок.
    #   ("chunk", session, audio)  — распознать кусок
    #   ("finish", session)        — сессия кончилась: склеить и вставить
    #   ("switch", name)           — сменить модель (скачает при необходимости)
    #   ("device", pref)           — сменить устройство (auto/cuda/cpu) и перезагрузить модель
    jobs = queue.Queue()
    manager = ModelManager(tray, jobs, config)
    state = {"model": None, "ready": False, "device": None}   # device — где реально работает модель
    tray.jobs = jobs
    has_gpu = ctranslate2.get_cuda_device_count() > 0   # видеокарта с CUDA есть?

    def open_model(src, name, device, compute):
        """Загрузить модель на устройство и прогнать секунду тишины (прогрев)."""
        tray.set_status(f"Загрузка {name} в {'GPU' if device == 'cuda' else 'CPU'}…")
        model = WhisperModel(src, device=device, compute_type=compute)
        segs, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), beam_size=1)
        list(segs)
        state["device"] = device
        print(f"модель {name} загружена: {device}/{compute}", flush=True)
        return model

    def load_model(name):
        """Скачать при необходимости и загрузить модель с прогревом.

        Автопереключение на CPU: если видеокарты нет — сразу грузим на CPU;
        если есть, но загрузка на неё упала (нет CUDA/cuDNN, не хватило
        VRAM) — пробуем ещё раз на CPU. На CPU всегда int8: float16 он
        не умеет, а int8 — самый быстрый вариант для процессора.
        """
        src = manager.path_or_name(name)   # путь локальной папки или имя/репо
        if not os.path.isdir(src) and not is_downloaded(name):
            tray.set_status(f"Скачивание {name}…")
            download_model(name, tray)
        if tray.device_pref == "cpu":    # CPU выбран вручную
            return open_model(src, name, "cpu", "int8")
        # auto или GPU: без видеокарты / при ошибке — откат на CPU, чтобы
        # диктовка работала всегда; в меню будет видно «GPU → CPU»
        if not has_gpu:
            print("видеокарта CUDA не найдена — работаю на CPU", flush=True)
            return open_model(src, name, "cpu", "int8")
        try:
            return open_model(src, name, "cuda", COMPUTE_TYPE)
        except Exception as e:
            print(f"не удалось загрузить {name} на GPU: {e} — пробую CPU", flush=True)
            gc.collect()                 # освободить то, что успело занять VRAM
            return open_model(src, name, "cpu", "int8")

    def do_switch(name):
        old_name = manager.current
        state["model"], state["ready"] = None, False
        gc.collect()                     # освободить VRAM старой модели
        try:
            state["model"] = load_model(name)
            state["ready"] = True
            manager.set_current(name)
            tray.icon(ICON_IDLE if tray.enabled else ICON_OFF)
            tray.set_status(f"Готов ({'GPU' if state['device'] == 'cuda' else 'CPU'})")
        except Exception as e:
            tray.set_status(f"Ошибка {name}: {e}")
            print("ошибка смены модели:", e, flush=True)
            if name != old_name:         # откат на прежнюю рабочую модель
                jobs.put(("switch", old_name))
            else:
                tray.icon(ICON_OFF)
        GLib.idle_add(tray.set_device_menu, tray.device_pref,
                      state["device"] if state["ready"] else None, has_gpu)

    def do_device(pref):
        """Сменить устройство и перезагрузить текущую модель; не вышло — вернуть прежнее."""
        old = tray.device_pref
        if pref == old:
            return
        tray.device_pref = pref
        do_switch(manager.current)
        if not state["ready"]:
            print(f"устройство {pref} не работает — возвращаю {old}", flush=True)
            tray.device_pref = old
            do_switch(manager.current)
            tray.set_status(f"{DEVICE_LABELS[pref]} недоступен — оставил {DEVICE_LABELS[old]}")
            return
        config["device"] = pref
        save_config(config)

    def transcribe_one(audio):
        segments, _ = state["model"].transcribe(
            audio, language=LANGUAGE or None, beam_size=1, vad_filter=True,
            condition_on_previous_text=False, initial_prompt=INIT_PROMPT or None)
        return " ".join(s.text.strip() for s in segments).strip()

    def transcriber():
        while True:
            job = jobs.get()
            kind = job[0]
            if kind == "device":
                do_device(job[1])
            elif kind == "switch":
                do_switch(job[1])
            elif kind == "chunk":
                _, session, audio = job
                if not session.recording:
                    tray.set_status("Распознавание…")
                text = transcribe_one(audio)
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
                    typer(text + " ")

    threading.Thread(target=transcriber, daemon=True).start()
    jobs.put(("switch", manager.current))   # первая загрузка — тем же путём
    manager.push()

    def pump(session, gen):
        """Пока идёт запись — отдавать готовые чанки транскрайберу;
        после отпускания клавиши — отдать хвост и маркер конца."""
        while session.recording:
            time.sleep(0.2)
            chunk = rec.take_chunk(gen)
            if chunk is not None:
                session.chunks += 1
                jobs.put(("chunk", session, chunk))
            if session.recording:
                m, s = divmod(int(time.time() - session.t0), 60)
                done = f", готово {len(session.texts)}" if session.texts else ""
                tray.set_status(f"Запись… {m}:{s:02d}{done}")
        tail = rec.drain(gen)
        if tail.size >= MIN_SECONDS * SAMPLE_RATE:
            jobs.put(("chunk", session, tail))
        elif session.chunks == 0:
            tray.set_status("Готов")   # случайное короткое нажатие
            return
        jobs.put(("finish", session))

    pressed, session, gen = False, None, 0
    tts_down, tts_solo = False, False   # клавиша чтения зажата / без других клавиш
    while True:
        for key_obj, _ in sel.select():
            try:
                events = list(key_obj.fileobj.read())
            except (OSError, BlockingIOError):
                continue
            for ev in events:
                if ev.type != ecodes.EV_KEY:
                    continue
                # Чтение вслух: срабатывает на ОТПУСКАНИЕ клавиши, и только если
                # пока она была зажата, не нажималось ничего другого — так
                # AltGr+8 или Alt+Tab не запускают чтение. Автоповтор (2) — мимо.
                # Клавиши берём из tray (меняются из меню на лету).
                if reader and ev.code == tray.read_key:
                    if ev.value == 1:
                        tts_down, tts_solo = True, True
                    elif ev.value == 0 and tts_down:
                        tts_down = False
                        if tts_solo and tray.tts_enabled:
                            threading.Thread(target=reader.toggle, daemon=True).start()
                    continue
                if tts_down and ev.value == 1:
                    tts_solo = False
                if ev.code != tray.dictate_key:
                    continue
                if not tray.enabled:
                    continue
                if ev.value == 1 and not pressed:
                    if not state["ready"]:
                        tray.set_status("Модель ещё не готова — подожди")
                        continue
                    if reader and reader.reading:   # колонки не должны попасть в микрофон
                        threading.Thread(target=reader.stop, daemon=True).start()
                    pressed = True
                    gen = rec.start()
                    session = Session()
                    tray.icon(ICON_REC)
                    tray.set_status("Запись…")
                    print("нажатие зафиксировано", flush=True)
                    threading.Thread(target=pump, args=(session, gen),
                                     daemon=True).start()
                elif ev.value == 0 and pressed:
                    pressed = False
                    session.recording = False
                    rec.pause()
                    tray.icon(ICON_IDLE)


def main():
    config = load_config()
    tray = Tray(config)
    reader = None
    if shepot_reader:
        try:
            # Reader создаётся в главном потоке до Gtk.main(): AT-SPI цепляется
            # к тому же GLib main loop, что и трей (как у Orca).
            reader = shepot_reader.Reader(config, ui=tray)
            tray.reader = reader

            def push_voices():
                GLib.idle_add(tray.set_voice_catalog, reader.voices.catalog(),
                              reader.voices.current, reader.rate, reader.engine_kind)
            reader.on_voices_changed = push_voices
            push_voices()
        except Exception as e:
            print("чтение недоступно:", e, flush=True)
            reader = None
    threading.Thread(target=worker, args=(tray, config, reader), daemon=True).start()
    Gtk.main()


if __name__ == "__main__":
    main()
