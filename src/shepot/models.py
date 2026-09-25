"""Менеджер моделей Whisper: каталог, размеры, скачивание, удаление.

Модели лежат в кеше HuggingFace (там же, куда их качает faster-whisper).
В сборку программы модели не входят — скачиваются при первом выборе.
"""

import json
import os
import shutil
import threading
import urllib.request

from .config import save_config
from .paths import HF_CACHE
from .settings import DEFAULT_MODEL

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
    """Скачана ли модель целиком (в снапшоте есть model.bin)."""
    d = model_cache_dir(name)
    if not os.path.isdir(d):
        return False
    for _root, _, files in os.walk(os.path.join(d, "snapshots")):
        if "model.bin" in files:
            return True
    return False


class ModelManager:
    """Каталог моделей: что доступно, что скачано, сколько весит.

    Хранит выбор пользователя в конфиге, умеет обновлять список и точные
    размеры с HuggingFace, удалять скачанные и подключать локальные папки.
    """

    def __init__(self, tray, jobs, config):
        self.tray, self.jobs, self.config = tray, jobs, config
        self.current = config.get("model") or DEFAULT_MODEL
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
        self.tray.call_soon(self.tray.set_model_catalog, entries, cur, self)

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
