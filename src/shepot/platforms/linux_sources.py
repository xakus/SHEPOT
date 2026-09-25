"""Linux: откуда брать текст для чтения вслух.

Каждый источник отвечает на вопрос «что читать?» и возвращает текст,
None («у меня текста нет — пробуй следующий») или NO_TEXT («текста нет
точно, дальше не пробовать» — терминал, поле пароля). Порядок задаёт
SHEPOT_TTS_SOURCE: auto = AT-SPI, затем клавиши+буфер.

- AtspiSource — шина доступности (как Orca): текст и курсор у виджета;
- KeysSource  — эмуляция клавиш (xdotool / ydotool) + буфер обмена;
- ClipboardSource — просто то, что уже лежит в буфере.
"""

import os
import shutil
import subprocess
import threading
import time
import uuid

from ..reader import NO_TEXT, TTS_MAX_CHARS

# скан-коды для ydotool (Wayland): KEY_LEFTCTRL=29 KEY_LEFTSHIFT=42 KEY_END=107
# KEY_INSERT=110 KEY_LEFT=105. Под X11 те же действия делает xdotool по именам.
KEYS_COPY       = os.environ.get("SHEPOT_TTS_KEYS_COPY", "29:1 110:1 110:0 29:0").split()
KEYS_SELECT_END = os.environ.get("SHEPOT_TTS_KEYS_SELECT_END",
                                 "29:1 42:1 107:1 107:0 42:0 29:0").split()
KEYS_LEFT       = os.environ.get("SHEPOT_TTS_KEYS_LEFT", "105:1 105:0").split()

ATSPI_TIMEOUT_MS = 800    # зависшее приложение не держит нас дольше этого
ATSPI_WALK_MAX   = 3000   # предел узлов при обходе активного окна
ATSPI_WALK_S     = 1.5    # предел времени обхода, сек
GRAB_TIMEOUT_S   = 2.5    # сколько ждём источник текста
CLIP_POLL_S      = 0.05   # шаг опроса буфера обмена


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
