# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

**SHEPOT** (от «шёпот» + Whisper) — push-to-talk диктовка (faster-whisper) и чтение
вслух (Piper) для **Linux, Windows и macOS**. Фоновая программа с иконкой в трее:
по удержанию клавиши пишет с микрофона, по отпусканию распознаёт речь и вставляет
текст в позицию курсора; по одиночному нажатию клавиши чтения читает выделенный текст.

**Сначала читай `CONTEXT-shepot.md`** — там полное состояние проекта:
что работает, что сломано, известные проблемы и ближайшие шаги. При значимых
изменениях обновляй этот документ.

Документ проекта — `docs/PROJECT.md` (что сделано + что планируется).
Планы новых функций — в `docs/plans/<функция>/PLAN.md`; текущий:
`docs/plans/cross-platform/PLAN.md` — Windows/macOS + сборка в GitHub Actions + релизы.
Перед реализацией — читать план, после каждого этапа — отмечать чекбоксы проверки.

Репозиторий: `git@github.com:xakus/SHEPOT.git`, ветка `main`.

## Где живёт рабочий код (Linux, машина разработчика)

Источник правды — пакет `src/shepot/`. Рабочая установка — venv `~/venvs/shepot/`
(пакет ставится туда через pip) + обёртка `~/bin/shepot-run.sh`
(выставляет `LD_LIBRARY_PATH` для cuBLAS/cuDNN и делает `python3 -m shepot`).
После правки кода задеплой и перезапусти:

```bash
~/venvs/shepot/bin/pip install -q --no-deps . && install -m 755 shepot-run.sh ~/bin/
pkill -f "python3 -m shepot$"; ~/bin/shepot-run.sh
```

## Команды

```bash
pkill -f "python3 -m shepot$"   # убить демон (обязательно перед отладкой — вторая
                                # копия отбирает микрофон и получает пустой поток).
                                # Не `pkill -f shepot` — убьёт и собственный shell.
~/bin/shepot-run.sh             # запуск с выводом в терминал
bash install-shepot.sh          # полная установка на Linux (apt + venv + пакет[gpu] + ярлык)
python -m pytest -q             # юнит-тесты (PYTHONPATH=src, нужен pytest)
python -m shepot --selftest     # проверка без GUI: импорты, PortAudio, Piper, Whisper tiny
python -m shepot.reader --say "текст"   # проверка чтения из терминала
SHEPOT_PLATFORM=desktop python -m shepot  # трей Windows/macOS (pystray) на Linux — для отладки
bash packaging/build_linux.sh   # сборка AppImage + tar.gz (SHEPOT_BUILD_NO_CUDA=1 — без CUDA)
```

Меню трея на Linux можно проверять без мыши через D-Bus: процесс отдаёт
`com.canonical.dbusmenu` по пути `/org/ayatana/NotificationItem/shepot/Menu`
(`GetLayout` — прочитать, `Event <id> clicked` — нажать).

## Архитектура

Пакет `src/shepot/` = ядро (не зависит от ОС) + платформенный слой.

- `app.py` — точка входа: создаёт трей платформы, `Reader`, `Dictation`;
  `--selftest` (`selftest.py`) гоняется в CI на собранной программе.
- `dictation.py` — **ядро диктовки**. `on_key(имя, значение)` получает события
  клавиш от платформы (имена как в evdev: `KEY_RIGHTCTRL`…, прочие — `OTHER`).
  Один поток-транскрайбер владеет моделью и разбирает очередь `jobs`:
  `chunk` / `finish` / `switch` (модель) / `device` (Авто/GPU/CPU). `load_model()`
  без GPU или при ошибке CUDA откатывается на CPU (`int8`).
- `audio.py` — `Recorder` (`sd.InputStream` открыт постоянно; длинная запись
  режется на чанки ~30 с по паузам, `find_split`), `Session`, лог чанков.
- `models.py` — `ModelManager`: каталог моделей, размеры, скачивание, удаление.
- `reader.py` — чтение вслух: `VoiceManager`, `PiperEngine`/`EspeakEngine`,
  конвейер синтез → очередь → плеер. Источники текста ему даёт платформа.
- `ui_base.py` — `TrayBase`: общее состояние трея (включено, клавиши, устройство)
  и правила. Отрисовка — у платформ. `call_soon()` — выполнить в потоке UI.
- `platforms/linux.py` — `GtkTray` (AppIndicator, `GLib.idle_add`), evdev,
  `make_typer()` (X11 → xdotool; Wayland → wl-copy + ydotool Ctrl+V).
  `platforms/linux_sources.py` — AT-SPI и клавиши+буфер для чтения.
- `platforms/desktop.py` — Windows и macOS: `PystrayTray` (меню перестраивается
  целиком; на Mac всё UI — через `AppHelper.callAfter`), pynput (хук клавиш,
  Ctrl/Cmd+V по виртуальному коду — не зависит от раскладки), pyperclip.
- `paths.py` — пути по ОС (Linux — прежние `~/.config/shepot` и т.д.),
  `gpu.py` — DLL cuBLAS/cuDNN на Windows (`os.add_dll_directory`).

Сборка: `packaging/shepot.spec` (PyInstaller, один на все ОС),
`packaging/build_{linux.sh,macos.sh,windows.ps1}`, `.github/workflows/build.yml`
(push → тесты + сборка 4 целей + selftest; тег `vX.Y.Z` → GitHub Release).
Версия — `src/shepot/__init__.py`, тег должен совпадать.

Конфигурация — переменные окружения `SHEPOT_*` (значения по умолчанию) +
`config.json` (выбор из меню, важнее) — таблица в `CONTEXT-shepot.md`, раздел 3.

## Жёсткие ограничения железа (не менять бездумно)

GPU — GTX 1080 Ti (Pascal, sm_61):

- `SHEPOT_COMPUTE` только `int8` — `float16` в CTranslate2 не работает на CC < 7.0.
- CUDA 12.x + cuDNN 9, драйвер ветки 580 — новее нельзя, Pascal отвалится.
- Откат при поломке cuDNN: `pip install --force-reinstall ctranslate2==4.4.0 "nvidia-cudnn-cu12==8.*"`.

## Главная нерешённая проблема

Вставка текста под **GNOME Wayland** не работает (xdotool/wtype/ydotool — все
мимо, детали в разделе 6 контекст-файла). Рабочее решение — сессия Xorg, тогда
`make_typer()` сам выбирает `xdotool`. Запасной путь — `SHEPOT_CLIPBOARD_ONLY=1`
(только копирование в буфер).
