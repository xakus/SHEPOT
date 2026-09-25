"""Точка входа SHEPOT: собрать трей, чтение и диктовку для текущей ОС.

    shepot                 обычный запуск (иконка в трее)
    shepot --selftest      проверка сборки без GUI (для CI)
    shepot --version
"""

import argparse
import io
import os
import sys
import threading

from . import __version__
from .config import load_config
from .paths import CONSOLE_LOG_PATH, DATA_DIR


class _UiProxy:
    """Интерфейс для Reader, пока трей ещё не создан: потом всё идёт в трей."""

    def __init__(self):
        self.tray = None

    def set_status(self, text):
        if self.tray:
            self.tray.set_status(text)
        else:
            print("СТАТУС:", text, flush=True)

    def set_reading(self, on):
        if self.tray:
            self.tray.set_reading(on)


def _fix_stdio():
    """Сборка без консоли (Windows/macOS): stdout/stderr = None, и любой print
    падает. Перенаправляем их в файл. В консоли Windows кодировка может быть
    cp866 — кириллица не должна ронять программу."""
    if sys.stdout is None or sys.stderr is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        log = open(CONSOLE_LOG_PATH, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 — живёт до выхода
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass


def _make_reader(plat, config, ui):
    """Создать Reader с источниками текста платформы; нет модуля/ошибка — None.

    Создаётся в главном потоке до запуска цикла UI: на Linux AT-SPI
    цепляется к тому же GLib main loop, что и трей (как у Orca).
    """
    try:
        from .reader import TTS_SOURCE, Reader
        sources = plat.make_sources(TTS_SOURCE)
        return Reader(config, ui=ui, sources=sources)
    except Exception as e:
        print("чтение недоступно:", e, flush=True)
        return None


def run_gui():
    """Обычный запуск: трей + диктовка + чтение."""
    from .dictation import Dictation
    from .platforms import current

    plat = current()
    config = load_config()
    ui = _UiProxy()
    reader = _make_reader(plat, config, ui)
    tray = plat.create_tray(config, has_reader=reader is not None)
    ui.tray = tray
    if reader:
        tray.reader = reader

        def push_voices():
            tray.call_soon(tray.set_voice_catalog, reader.voices.catalog(),
                           reader.voices.current, reader.rate, reader.engine_kind)
        reader.on_voices_changed = push_voices
        push_voices()

    dictation = Dictation(tray, config, reader, plat.make_typer())

    def on_ready():
        """Трей показан: клавиатура и модель — в фоне (модель грузится секунды)."""
        def boot():
            if not plat.start_hotkeys(tray, dictation.on_key):
                return
            try:
                dictation.start()
            except Exception as e:
                print("ошибка запуска диктовки:", e, flush=True)
                tray.set_status(f"Ошибка запуска: {e}")
                tray.icon("off")
        threading.Thread(target=boot, daemon=True).start()

    tray.run(on_ready)


def main(argv=None):
    _fix_stdio()
    ap = argparse.ArgumentParser(prog="shepot", description="SHEPOT — диктовка и чтение вслух")
    ap.add_argument("--selftest", action="store_true",
                    help="проверить сборку без GUI (импорты, модель tiny на CPU, резка текста)")
    ap.add_argument("--version", action="version", version=f"SHEPOT {__version__}")
    args = ap.parse_args(argv)
    if args.selftest:
        from .selftest import run
        sys.exit(run())
    run_gui()


if __name__ == "__main__":
    main()
