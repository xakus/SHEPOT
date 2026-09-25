"""Windows и macOS: pynput (клавиши и нажатия), pystray (трей), pyperclip (буфер).

- Горячие клавиши — глобальный хук клавиатуры pynput. На macOS нужно
  разрешение «Универсальный доступ» (Accessibility), иначе события не приходят.
- Вставка текста — буфер обмена + Ctrl+V (Cmd+V на Mac). Клавиша V
  нажимается по виртуальному коду, а не по символу: при русской раскладке
  символа «v» на клавиатуре нет, и Ctrl+«v» не сработал бы.
- Текст для чтения — буфер + Ctrl+C (Cmd+C): выделенный текст. Режима
  «от курсора до конца документа» здесь нет (он есть только на Linux).
- Меню трея перестраивается целиком при каждом изменении (pystray
  не умеет менять отдельный пункт). На macOS всё, что трогает AppKit,
  выполняется в главном потоке через AppHelper.callAfter.
"""

import os
import threading
import time
import uuid

from ..keys import KEY_CANDIDATES, OTHER, key_label, labeled
from ..paths import HF_CACHE, IS_MAC, IS_WIN, VOICES_DIR, open_folder
from ..settings import CLIP_ONLY, DEVICE_LABELS, TTS_RATES
from ..ui_base import ICON_STATES, TrayBase

if IS_MAC:
    # pbcopy/pbpaste без UTF-8 локали портят кириллицу (если pyperclip не нашёл AppKit)
    os.environ.setdefault("LANG", "en_US.UTF-8")

# виртуальные коды клавиш V и C (не зависят от раскладки)
VK_V, VK_C = (9, 8) if IS_MAC else (0x56, 0x43)
LLKHF_INJECTED = 0x10    # Windows: флаг синтетического нажатия (от нас же самих)
CLIP_POLL_S = 0.05       # шаг опроса буфера обмена

# имя клавиши (keys.py) → атрибут pynput.keyboard.Key; нет атрибута на ОС — нет клавиши
_PYNPUT_KEYS = {
    "KEY_RIGHTCTRL":  ["ctrl_r"],
    "KEY_RIGHTALT":   ["alt_r", "alt_gr"],
    "KEY_RIGHTSHIFT": ["shift_r"],
    "KEY_RIGHTMETA":  ["cmd_r"],
    "KEY_COMPOSE":    ["menu"],
    "KEY_PAUSE":      ["pause"],
    "KEY_SCROLLLOCK": ["scroll_lock"],
    "KEY_CAPSLOCK":   [] if IS_MAC else ["caps_lock"],   # на Mac Caps шлёт только переключения
    "KEY_INSERT":     ["insert"],
}


def _act(fn, *args):
    """Действие пункта меню без аргументов: pystray принимает функции с 0–2 параметрами."""
    return lambda: fn(*args)


# --- клавиши и буфер ---------------------------------------------------------

class Keys:
    """Нажатие комбинаций и буфер обмена (общие для вставки и чтения)."""

    def __init__(self):
        from pynput import keyboard
        self.kb = keyboard.Controller()
        self.mod = keyboard.Key.cmd if IS_MAC else keyboard.Key.ctrl
        self.KeyCode = keyboard.KeyCode

    def combo(self, vk):
        """Ctrl/Cmd + клавиша по виртуальному коду."""
        k = self.KeyCode.from_vk(vk)
        with self.kb.pressed(self.mod):
            self.kb.press(k)
            self.kb.release(k)

    @staticmethod
    def clip_get():
        import pyperclip
        try:
            return pyperclip.paste()
        except Exception:
            return None

    @staticmethod
    def clip_set(text):
        import pyperclip
        pyperclip.copy(text)


def make_typer():
    """Вставка: текст в буфер обмена, затем Ctrl+V (Cmd+V).

    Текст остаётся в буфере — если вставка не сработала, его можно вставить руками.
    """
    keys = Keys()

    def paste(text):
        keys.clip_set(text)
        if CLIP_ONLY:
            return
        time.sleep(0.05)                 # дать буферу обновиться
        keys.combo(VK_V)
        print(f"ВСТАВКА: {len(text)} символов", flush=True)
    return paste


class CopySource:
    """Выделенный текст через Ctrl+C (Cmd+C) с возвратом прежнего буфера.

    1. запомнить текст буфера, положить «соль» (уникальную строку);
    2. Ctrl+C — если буфер сменился, это выделение;
    3. вернуть в буфер то, что там было (только текст: картинка из буфера
       после чтения пропадёт — ограничение pyperclip).
    """
    name = "copy"

    def __init__(self):
        self.keys = Keys()

    def grab(self):
        old = self.keys.clip_get()
        salt = f"⁣shepot-tts-{uuid.uuid4().hex}"
        self.keys.clip_set(salt)
        try:
            self.keys.combo(VK_C)
            deadline = time.time() + 0.8
            while time.time() < deadline:
                cur = self.keys.clip_get()
                if cur and cur != salt and cur.strip():
                    return cur
                time.sleep(CLIP_POLL_S)
            return None
        finally:
            self.keys.clip_set(old or "")


class ClipboardSource:
    """Режим SHEPOT_TTS_SOURCE=clipboard: читать то, что пользователь скопировал сам."""
    name = "clipboard"

    def grab(self):
        return Keys.clip_get()


def make_sources(mode, own_loop=False):
    """Источники текста: по умолчанию — выделение через Ctrl+C."""
    if mode == "clipboard":
        return [ClipboardSource()]
    return [CopySource()]


# --- горячие клавиши --------------------------------------------------------

def _mac_trusted(prompt):
    """macOS: есть ли разрешение «Универсальный доступ»; prompt — показать системный запрос."""
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))
    except Exception as e:
        print("проверка разрешений macOS не удалась:", e, flush=True)
        return True


_listener = None   # держим ссылку, иначе слушатель соберёт сборщик мусора


def start_hotkeys(tray, on_key):
    """Глобальный хук клавиатуры pynput → on_key(имя, значение)."""
    global _listener
    from pynput import keyboard

    if IS_MAC and not _mac_trusted(prompt=True):
        tray.set_status("Нужно разрешение: Настройки → Конфиденциальность → "
                        "Универсальный доступ → SHEPOT, затем перезапусти")
        tray.icon("off")
        return False

    names = {}
    for name, attrs in _PYNPUT_KEYS.items():
        for a in attrs:
            k = getattr(keyboard.Key, a, None)
            if k is not None:
                names[k] = name
    have = [n for n in KEY_CANDIDATES if n in names.values()]
    tray.call_soon(tray.set_key_catalog, labeled(have))

    down = set()   # зажатые клавиши-кандидаты: повторное нажатие = автоповтор

    def press(key):
        name = names.get(key, OTHER)
        if name != OTHER and name in down:
            on_key(name, 2)
            return
        down.add(name)
        on_key(name, 1)

    def release(key):
        name = names.get(key, OTHER)
        down.discard(name)
        on_key(name, 0)

    kwargs = {}
    if IS_WIN:
        # собственные синтетические нажатия (Ctrl+V, Ctrl+C) не считать нажатиями пользователя
        def win_filter(msg, data):
            return not (data.flags & LLKHF_INJECTED)
        kwargs["win32_event_filter"] = win_filter
    _listener = keyboard.Listener(on_press=press, on_release=release, **kwargs)
    _listener.start()
    return True


# --- трей --------------------------------------------------------------------

class PystrayTray(TrayBase):
    """Иконка в трее Windows / строке меню macOS. Меню строится из текущего состояния."""

    def __init__(self, config, has_reader):
        import pystray
        from ..icons import draw_icon
        super().__init__(config, has_reader)
        self.pystray = pystray
        self.status_text = "Загрузка модели…"
        self.last_text = "—"
        self.models = ([], None, None)            # (записи, текущая, ModelManager)
        self.device = (self.device_pref, None, False)   # (выбор, где реально, есть ли GPU)
        self.voices = ([], "", 1.0)               # (записи, текущий голос, скорость)
        self.reading = False
        self.images = {s: draw_icon(s, 64) for s in ICON_STATES}
        self.ic = pystray.Icon("shepot", self.images["load"], "SHEPOT", menu=self._build())
        self._menu_dirty = False      # Windows: меню устарело, пересобрать перед показом
        self._lazy_menu = IS_WIN and self._install_win_lazy_menu()

    # -- потоки --

    def call_soon(self, fn, *args):
        if IS_MAC:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(fn, *args)
        else:
            fn(*args)

    def _install_win_lazy_menu(self):
        """Windows: pystray удаляет и создаёт меню прямо в вызывающем потоке —
        если в этот момент меню открыто (обновился процент скачивания), это
        гонка. Поэтому меню пересобирается только в потоке pystray, перед
        показом по правому клику. Обработчик сообщений — внутренний API
        pystray; не получилось — работаем по-старому."""
        try:
            from pystray._util import win32
            handlers = self.ic._message_handlers
            orig = handlers[win32.WM_NOTIFY]

            def on_notify(wparam, lparam):
                if lparam == win32.WM_RBUTTONUP and self._menu_dirty:
                    self._menu_dirty = False
                    self.ic.menu = self._build()     # сеттер сам пересоздаёт меню
                return orig(wparam, lparam)
            handlers[win32.WM_NOTIFY] = on_notify
            return True
        except Exception as e:
            print("ленивое меню pystray недоступно:", e, flush=True)
            return False

    def _refresh(self):
        """Перестроить меню и подсказку (из любого потока)."""
        self.call_soon(self._apply)

    def _apply(self):
        # подсказка при наведении; в Windows не длиннее 127 символов (szTip)
        self.ic.title = f"SHEPOT — {self.status_text}"[:120]
        if self._lazy_menu:
            self._menu_dirty = True
            return
        try:
            self.ic.menu = self._build()                 # сеттер сам вызывает update_menu
        except Exception:
            pass   # иконка ещё не показана — меню применится при показе

    # -- базовые операции --

    def set_status(self, text):
        self.status_text = text
        self._refresh()

    def set_last(self, text):
        self.last_text = self.short_last(text)
        self._refresh()

    def icon(self, state):
        img = self.images[state]
        self.call_soon(setattr, self.ic, "icon", img)

    def set_reading(self, on):
        self.reading = on
        self.icon("read" if on else self.idle_icon())
        self._refresh()

    def set_model_catalog(self, entries, current, manager):
        self.models = (entries, current, manager)
        self._apply()

    def set_device_menu(self, pref, actual, has_gpu):
        self.device = (pref, actual, has_gpu)
        self._apply()

    def set_voice_catalog(self, entries, current, rate, engine_kind):
        self.voices = (entries, current, rate)
        self._apply()

    def rebuild_key_menus(self):
        self._apply()

    def run(self, on_ready):
        def setup(icon):
            icon.visible = True
            on_ready()
        self.ic.run(setup=setup)

    def quit(self):
        try:
            self.ic.stop()
        finally:
            os._exit(0)

    # -- действия меню --

    def _toggle_enabled(self):
        self.set_enabled(not self.enabled)
        self._refresh()

    def _toggle_tts(self):
        self.set_tts_enabled(not self.tts_enabled)
        self._refresh()

    def _stop_reading(self):
        if self.reader:
            threading.Thread(target=self.reader.stop, daemon=True).start()

    def _set_rate(self, r):
        if self.reader:
            self.reader.set_rate(r)
        self.voices = (self.voices[0], self.voices[1], r)
        self._refresh()

    # -- построение меню --

    def _build(self):
        ps = self.pystray
        Item, Menu, SEP = ps.MenuItem, ps.Menu, ps.Menu.SEPARATOR
        items = [Item(self.status_text, None, enabled=False),
                 Item(self.last_text, None, enabled=False), SEP,
                 self._model_menu(), self._device_menu()]
        if self.has_reader:
            items += self._voice_menus()
        items += [SEP,
                  Item(f"Слушать ({key_label(self.dictate_key_name)})", _act(self._toggle_enabled),
                       checked=lambda _i: self.enabled),
                  self._key_menu("Клавиша диктовки", self.dictate_key_name, self.set_dictate_key)]
        if self.has_reader:
            items += [Item(f"Читать ({key_label(self.read_key_name)})", _act(self._toggle_tts),
                           checked=lambda _i: self.tts_enabled),
                      self._key_menu("Клавиша чтения", self.read_key_name, self.set_read_key),
                      Item("Стоп чтение", _act(self._stop_reading), enabled=self.reading)]
        items += [SEP, Item("Выход", _act(self.quit))]
        return Menu(*items)

    def _model_menu(self):
        ps = self.pystray
        Item, Menu, SEP = ps.MenuItem, ps.Menu, ps.Menu.SEPARATOR
        entries, current, manager = self.models
        if manager is None:
            return Item("Модель", Menu(Item("Загрузка…", None, enabled=False)))
        sub = []
        for e in entries:
            mark = "локальная" if e["local"] else ("скачана" if e["downloaded"] else "скачать")
            sub.append(Item(f'{e["name"]} — {e["size"]} ({mark})',
                            _act(manager.select, e["name"]),
                            checked=lambda _i, n=e["name"]: n == current, radio=True))
        removable = [e for e in entries if e["removable"]]
        # удаление — через подменю «Подтвердить»: случайный клик не сотрёт 3 ГБ
        rm = [Item(f'{e["name"]} — {e["size"]}',
                   Menu(Item("Подтвердить удаление", _act(manager.delete, e["name"]))))
              for e in removable]
        sub += [SEP,
                Item("Удалить скачанную", Menu(*rm) if rm else None, enabled=bool(rm)),
                Item("Открыть папку моделей", _act(open_folder, HF_CACHE)),
                Item("Обновить список (HuggingFace)", _act(manager.refresh_remote))]
        return Item(f"Модель: {current}", Menu(*sub))

    def _device_menu(self):
        ps = self.pystray
        pref, actual, has_gpu = self.device
        sub = []
        for key, label in DEVICE_LABELS.items():
            if key == "cuda" and not has_gpu:
                label += " — не найден"
            sub.append(ps.MenuItem(label, _act(self.pick_device, key),
                                   checked=lambda _i, k=key: k == self.device[0], radio=True,
                                   enabled=key != "cuda" or has_gpu))
        return ps.MenuItem(self.device_title(pref, actual), ps.Menu(*sub))

    def _voice_menus(self):
        ps = self.pystray
        Item, Menu, SEP = ps.MenuItem, ps.Menu, ps.Menu.SEPARATOR
        entries, current, rate = self.voices

        def on_reader(method, *args):
            """Действие с Reader: берём его в момент клика (трей создаётся раньше Reader)."""
            return _act(lambda: self.reader and getattr(self.reader, method)(*args))

        sub = [Item(f'{e["name"]} — {e["size"]} ({"скачан" if e["downloaded"] else "скачать"})',
                    on_reader("select_voice", e["name"]),
                    checked=lambda _i, n=e["name"]: n == current, radio=True)
               for e in entries]
        rm = [Item(f'{e["name"]} — {e["size"]}',
                   Menu(Item("Подтвердить удаление", on_reader("delete_voice", e["name"]))))
              for e in entries if e["removable"]]
        sub += [SEP,
                Item("Удалить скачанный", Menu(*rm) if rm else None, enabled=bool(rm)),
                Item("Открыть папку голосов", _act(open_folder, VOICES_DIR)),
                Item("Обновить список (HuggingFace)", on_reader("refresh_voices"))]
        rates = [Item(f"{r:g}×", _act(self._set_rate, r),
                      checked=lambda _i, r=r: abs(r - self.voices[2]) < 0.01, radio=True)
                 for r in TTS_RATES]
        return [Item(f"Голос: {self.voice_short(current)}", Menu(*sub)),
                Item(f"Скорость: {rate:g}×", Menu(*rates))]

    def _key_menu(self, title, current, setter):
        ps = self.pystray
        sub = [ps.MenuItem(label, _act(setter, name),
                           checked=lambda _i, n=name: n == current, radio=True)
               for name, label in self.available_keys]
        if not sub:
            sub = [ps.MenuItem("—", None, enabled=False)]
        return ps.MenuItem(f"{title}: {key_label(current)}", ps.Menu(*sub))


def create_tray(config, has_reader):
    return PystrayTray(config, has_reader)
