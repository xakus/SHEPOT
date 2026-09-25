"""Linux: evdev (клавиши), GTK + AppIndicator (трей), xdotool/ydotool (вставка).

Клавиши ловятся через /dev/input (нужна группа input), поэтому работают
и под X11, и под Wayland. Иконка — AppIndicator в верхней панели GNOME.
Все обновления UI из фоновых потоков — только через GLib.idle_add.
"""

import os
import shutil
import subprocess
import threading
import time
from selectors import EVENT_READ, DefaultSelector

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppInd  # noqa: E402
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppInd  # noqa: E402

from ..keys import KEY_CANDIDATES, OTHER, key_label, labeled  # noqa: E402
from ..models import model_cache_dir  # noqa: E402
from ..paths import HF_CACHE, VOICES_DIR, open_folder  # noqa: E402
from ..settings import CLIP_ONLY, DEVICE_LABELS, TTS_RATES  # noqa: E402
from ..ui_base import TrayBase  # noqa: E402

# иконки из темы GNOME для состояний трея
ICONS = {
    "load": "content-loading-symbolic",
    "idle": "audio-input-microphone-symbolic",
    "rec":  "media-record-symbolic",
    "off":  "microphone-sensitivity-muted-symbolic",
    "read": "audio-speakers-symbolic",
}


# --- трей ----------------------------------------------------------------

class GtkTray(TrayBase):
    """Иконка в панели GNOME. Все обновления UI — через GLib.idle_add."""

    def __init__(self, config, has_reader):
        super().__init__(config, has_reader)
        self._updating_menu = False   # перестройка меню шлёт toggled — игнорируем
        self.available_keys = labeled(KEY_CANDIDATES)   # уточняется по клавиатурам

        self.ind = AppInd.Indicator.new(
            "shepot", ICONS["load"], AppInd.IndicatorCategory.APPLICATION_STATUS)
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
        if has_reader:
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
        self.toggle.connect("toggled", lambda item: self.set_enabled(item.get_active()))
        self.menu.append(self.toggle)

        # подменю выбора клавиши диктовки; наполняется rebuild_key_menus()
        self.dictate_key_item = Gtk.MenuItem(label="Клавиша диктовки")
        self.dictate_key_item.set_submenu(Gtk.Menu())
        self.menu.append(self.dictate_key_item)

        if has_reader:
            self.tts_toggle = Gtk.CheckMenuItem(
                label=f"Читать ({key_label(self.read_key_name)})")
            self.tts_toggle.set_active(self.tts_enabled)
            self.tts_toggle.connect("toggled",
                                    lambda item: self.set_tts_enabled(item.get_active()))
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

    # -- базовые операции (из любого потока) --

    def call_soon(self, fn, *args):
        def wrapper():
            fn(*args)
            return False   # idle_add: не повторять
        GLib.idle_add(wrapper)

    def icon(self, state):
        GLib.idle_add(self.ind.set_icon_full, ICONS[state], "shepot")

    def set_status(self, text):
        GLib.idle_add(self.status.set_label, text)

    def set_last(self, text):
        GLib.idle_add(self.last.set_label, self.short_last(text))

    def set_reading(self, on):
        """Reader сообщает: чтение началось/кончилось (из фонового потока)."""
        self.icon("read" if on else self.idle_icon())
        GLib.idle_add(self.stop_item.set_sensitive, on)

    def run(self, on_ready):
        GLib.idle_add(lambda: on_ready() and False)
        Gtk.main()

    def quit(self):
        Gtk.main_quit()
        os._exit(0)

    # -- подменю моделей (только из GTK-потока) --

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
        folder.connect("activate", lambda *_: open_folder(HF_CACHE))
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

    def _on_model_delete(self, _item, name, manager):
        if self._confirm(f"Удалить модель {name} с диска?", model_cache_dir(name)):
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

    @staticmethod
    def _confirm(text, detail):
        """Диалог «Да/Нет»; True — пользователь согласился."""
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO, text=text)
        dlg.format_secondary_text(detail)
        ok = dlg.run() == Gtk.ResponseType.YES
        dlg.destroy()
        return ok

    # -- подменю устройства (только из GTK-потока) --

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
        self.device_item.set_label(self.device_title(pref, actual))
        menu.show_all()
        self._updating_menu = False
        return False   # для GLib.idle_add — не повторять

    def _on_device_pick(self, item, pref):
        if self._updating_menu or not item.get_active():
            return
        self.pick_device(pref)

    # -- меню чтения (только из GTK-потока) --

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
        folder.connect("activate", lambda *_: open_folder(VOICES_DIR))
        self.voice_menu.append(folder)
        refresh = Gtk.MenuItem(label="Обновить список (HuggingFace)")
        refresh.connect("activate", lambda *_: self.reader.refresh_voices())
        self.voice_menu.append(refresh)

        self.voice_item.set_label(f"Голос: {self.voice_short(current)}")
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
        if self._confirm(f"Удалить голос {name} с диска?", VOICES_DIR):
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

    # -- меню выбора клавиш (только из GTK-потока) --

    def rebuild_key_menus(self):
        """Перестроить подменю «Клавиша диктовки»/«Клавиша чтения» и ярлыки."""
        self._updating_menu = True
        self._fill_key_menu(self.dictate_key_item, self.dictate_key_name,
                            self._on_dictate_key_pick)
        self.dictate_key_item.set_label(
            f"Клавиша диктовки: {key_label(self.dictate_key_name)}")
        self.toggle.set_label(f"Слушать ({key_label(self.dictate_key_name)})")
        if self.has_reader:
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


def create_tray(config, has_reader):
    return GtkTray(config, has_reader)


# --- вставка текста --------------------------------------------------------

def make_typer():
    """Выбрать способ вставки текста в позицию курсора по окружению."""
    wayland = os.environ.get("XDG_SESSION_TYPE", "") == "wayland"
    print("сессия:", os.environ.get("XDG_SESSION_TYPE"), flush=True)

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


# --- источники текста для чтения ------------------------------------------

def make_sources(mode, own_loop=False):
    """Источники текста в порядке опроса (см. linux_sources.py)."""
    from .linux_sources import make_sources as _make
    return _make(mode, own_loop)


# --- клавиатура ------------------------------------------------------------

def find_all_keyboards():
    """Все физические клавиатуры: устройства с буквой A или одной из
    клавиш-кандидатов (KEY_CANDIDATES). Виртуальное устройство ydotool
    исключаем — иначе демон ловил бы собственные синтетические нажатия
    (вставку диктовки, Ctrl+Insert при чтении).

    Регистрируем сразу все клавиатуры, а нужную клавишу сверяем в цикле —
    тогда смена горячей клавиши не требует перерегистрации устройств.
    """
    from evdev import InputDevice, ecodes, list_devices
    cand_codes = {getattr(ecodes, n) for n in KEY_CANDIDATES}
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
    """Какие клавиши-кандидаты реально есть хотя бы на одной из клавиатур —
    только их показываем в меню выбора."""
    from evdev import ecodes
    have = set()
    for d in keyboards:
        have |= set(d.capabilities().get(ecodes.EV_KEY, []))
    return labeled([n for n in KEY_CANDIDATES if getattr(ecodes, n) in have])


def start_hotkeys(tray, on_key):
    """Слушать все клавиатуры в фоновом потоке и отдавать события в on_key."""
    from evdev import ecodes

    keyboards = find_all_keyboards()
    if not keyboards:
        tray.set_status("Нет доступа к /dev/input — перелогинься")
        tray.icon("off")
        return False
    tray.call_soon(tray.set_key_catalog, available_hotkeys(keyboards))
    names = {getattr(ecodes, n): n for n in KEY_CANDIDATES}   # код evdev → имя

    def loop():
        sel = DefaultSelector()
        for d in keyboards:
            sel.register(d, EVENT_READ)
        while True:
            for key_obj, _ in sel.select():
                try:
                    events = list(key_obj.fileobj.read())
                except (OSError, BlockingIOError):
                    continue
                for ev in events:
                    if ev.type == ecodes.EV_KEY:
                        on_key(names.get(ev.code, OTHER), ev.value)

    threading.Thread(target=loop, daemon=True).start()
    return True
