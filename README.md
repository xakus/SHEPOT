# SHEPOT

**SHEPOT** (от «шёпот» + Whisper) — диктовка и чтение вслух для **Windows, macOS и Linux**.
Всё работает локально, без интернета и без облака.

- **Диктовка:** держишь клавишу → говоришь → отпускаешь → текст появляется там, где стоит курсор.
  Распознавание — [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (модели Whisper).
- **Чтение вслух:** выделил текст → нажал клавишу чтения → текст читается голосом
  [Piper](https://github.com/OHF-Voice/piper1-gpl). Повторное нажатие — стоп.
- Иконка в трее (строке меню на Mac): статус, выбор модели, устройства (GPU/CPU), голоса,
  скорости и клавиш.

## Скачать

Готовые сборки — на странице [Releases](https://github.com/xakus/SHEPOT/releases/latest):

| ОС | Файл | Видеокарта |
|---|---|---|
| Windows 10/11 (x64) | `SHEPOT-Setup-X.Y.Z.exe` — установщик, или `…-windows-x64.zip` — без установки | NVIDIA CUDA — автоматически, переключается в меню |
| macOS 12+ Apple Silicon (M1–M4) | `SHEPOT-X.Y.Z-macos-arm64.dmg` | только CPU |
| macOS 12+ Intel | `SHEPOT-X.Y.Z-macos-x64.dmg` | только CPU |
| Linux x86_64 (Ubuntu 22.04+) | `SHEPOT-X.Y.Z-linux-x86_64.AppImage` или `.tar.gz` | NVIDIA CUDA — автоматически |

Модели распознавания (75 МБ – 3 ГБ) и голоса (~60 МБ) в установщик не входят —
скачиваются при первом запуске с прогрессом в трее.

## Клавиши по умолчанию

| | Windows | macOS | Linux |
|---|---|---|---|
| Диктовка (держать) | правый Ctrl | правый Option | правый Ctrl |
| Чтение (нажать и отпустить) | правый Shift | правый Cmd | правый Alt |

Меняются в меню трея: «Клавиша диктовки», «Клавиша чтения».

## Установка

### Windows
1. Запусти `SHEPOT-Setup-X.Y.Z.exe`. Если SmartScreen пишет «Неизвестный издатель», нажми
   «Подробнее» → «Выполнить в любом случае». Программа не подписана платным сертификатом.
2. Установщик не просит прав администратора. По желанию добавит SHEPOT в автозапуск.
3. Иконка появится в трее (возле часов; может прятаться под стрелкой ^).

### macOS
1. Открой `.dmg` и перетащи SHEPOT в «Программы».
2. Первый запуск: правый клик по SHEPOT → «Открыть» → «Открыть». Программа не подписана
   Apple Developer ID, поэтому обычный двойной клик Gatekeeper заблокирует.
3. Дай разрешения в «Системные настройки → Конфиденциальность и безопасность»:
   - **Универсальный доступ** — нужен для горячих клавиш и вставки текста;
   - **Микрофон** — запросится при первой диктовке.

   После выдачи «Универсального доступа» перезапусти SHEPOT.

### Linux
**AppImage:**
```bash
chmod +x SHEPOT-*-linux-x86_64.AppImage
sudo usermod -aG input $USER     # клавиши читаются из /dev/input; потом перелогинься
./SHEPOT-*-linux-x86_64.AppImage
```
Если AppImage не запускается из-за FUSE: `sudo apt install libfuse2` или
`./SHEPOT-*.AppImage --appimage-extract-and-run`.
Под GNOME для иконки нужно расширение AppIndicator (в Ubuntu включено по умолчанию).
Для вставки текста под X11 нужен `xdotool`, под Wayland — `wl-clipboard` + `ydotool`
(см. `CONTEXT-shepot.md`, раздел про Wayland).

**Из исходников (с GPU, для разработки):** `bash install-shepot.sh`.

## Видеокарта

Меню «Устройство»:
- **Авто** — GPU NVIDIA, если он есть и модель на нём загрузилась, иначе CPU;
- **GPU (CUDA)** — видеокарта (если её нет или не хватило памяти — откат на CPU);
- **CPU** — всегда процессор.

На процессоре `large-v3` медленная. Лучше взять `large-v3-turbo` (почти такое же качество)
или `small`.

## Где что лежит

| | Windows | macOS | Linux |
|---|---|---|---|
| Настройки | `%APPDATA%\SHEPOT\config.json` | `~/Library/Application Support/SHEPOT/config.json` | `~/.config/shepot/config.json` |
| Голоса | `%APPDATA%\SHEPOT\voices` | `…/SHEPOT/voices` | `~/.local/share/shepot/voices` |
| Лог диктовки | `%APPDATA%\SHEPOT\shepot-log.txt` | `…/SHEPOT/shepot-log.txt` | `~/shepot-log.txt` |
| Модели | `~/.cache/huggingface/hub` (на всех ОС) | | |

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[gpu,dev]"                        # macOS: ".[dev]"
python -m pytest -q                                # юнит-тесты
python -m shepot --selftest                        # проверка без GUI
python -m shepot                                   # запуск
```

Структура:
```
src/shepot/
├── app.py            точка входа, --selftest
├── dictation.py      ядро диктовки: модель, очередь, клавиши
├── reader.py         чтение вслух: голоса Piper, синтез, воспроизведение
├── audio.py models.py config.py paths.py settings.py keys.py gpu.py icons.py
├── ui_base.py        общая логика трея
└── platforms/
    ├── linux.py      evdev + GTK/AppIndicator + xdotool/ydotool
    ├── linux_sources.py  текст для чтения: AT-SPI, клавиши+буфер
    └── desktop.py    Windows и macOS: pynput + pystray + pyperclip
packaging/            PyInstaller, Inno Setup, AppImage, dmg
.github/workflows/    сборка на 3 ОС и релизы
```

**Релиз:** поменять `__version__` в `src/shepot/__init__.py`, закоммитить, затем
`git tag vX.Y.Z && git push origin vX.Y.Z`. GitHub Actions соберёт все ОС и выложит Release.

## Лицензия

Piper (`piper-tts`) распространяется под GPL-3.0, и сборки его включают,
поэтому SHEPOT распространяется под GPL-3.0-or-later.
