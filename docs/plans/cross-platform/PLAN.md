# План: SHEPOT на Windows, macOS и Linux + сборка в GitHub Actions + релизы

Статус: **согласован** (решения — раздел 8), идёт реализация.
Дата: 2026-09-25.

## 1. Цель

- SHEPOT работает на **Windows 10/11**, **macOS 12+** (Intel и Apple Silicon)
  и **Linux** (как сейчас).
- Код лежит на GitHub, каждый push собирается в GitHub Actions на трёх ОС.
- Тег `vX.Y.Z` → автоматический GitHub Release с готовыми файлами для
  скачивания под каждую ОС.

## 2. Что сейчас завязано на Linux

| Что | Сейчас (Linux) | Windows | macOS |
|---|---|---|---|
| Глобальная клавиша | `evdev` (`/dev/input`) | `pynput` (хук клавиатуры) | `pynput` (нужно разрешение «Мониторинг ввода») |
| Иконка в трее | GTK3 + AppIndicator | `pystray` (win32) | `pystray` (AppKit, строка меню) |
| Вставка текста | `xdotool` / `wl-copy` + `ydotool` | буфер (`pyperclip`) + Ctrl+V через `pynput` | буфер + Cmd+V через `pynput` (нужно разрешение «Универсальный доступ») |
| Выделенный текст для чтения | AT-SPI | сохранить буфер → Ctrl+C → прочитать → вернуть буфер | то же, Cmd+C |
| Голос | Piper, запасной `espeak-ng` | Piper (onnxruntime) | Piper (onnxruntime) |
| Микрофон | `sounddevice` | `sounddevice` (PortAudio внутри wheel) | `sounddevice` (нужно разрешение «Микрофон») |
| Распознавание | faster-whisper, CUDA/CPU | CUDA/CPU | **только CPU** (у CTranslate2 нет Metal) |
| Пути | `~/.config/shepot`, `~/.cache/huggingface` | `%APPDATA%\SHEPOT` | `~/Library/Application Support/SHEPOT` |

## 3. Архитектура

Это одна настольная программа на Python, не микросервисы. Поэтому
правила про микросервисы (Spring, Redis, Kafka) здесь не применяются.

Ядро не зависит от ОС: запись, нарезка чанков, распознавание, менеджер
моделей, выбор устройства, Piper. Всё, что зависит от ОС, живёт в
«платформенном слое» с одинаковым интерфейсом:

```
SHEPOT/
├── src/shepot/
│   ├── __main__.py          точка входа: выбирает платформу
│   ├── core/                ядро (без ОС-зависимостей)
│   │   ├── recorder.py      Recorder, find_split
│   │   ├── transcriber.py   очередь jobs, load_model, авто CPU/GPU
│   │   ├── models.py        ModelManager, скачивание
│   │   ├── reader.py        чтение вслух (Piper), без AT-SPI
│   │   └── config.py        пути конфига/кеша по ОС (platformdirs)
│   ├── platform/
│   │   ├── base.py          интерфейсы: Hotkeys, Tray, Typer, SelectionGrabber
│   │   ├── linux.py         evdev + GTK/AppIndicator + xdotool/ydotool + AT-SPI (текущий код)
│   │   ├── windows.py       pynput + pystray + буфер/Ctrl+V
│   │   └── macos.py         pynput + pystray + буфер/Cmd+V + проверка разрешений
│   └── i18n.py              строки интерфейса (ru сейчас, en — на будущее)
├── packaging/
│   ├── shepot.spec          PyInstaller
│   ├── windows/shepot.iss   Inno Setup (установщик .exe)
│   ├── macos/               Info.plist (описания разрешений), иконка .icns
│   └── linux/               .desktop, иконка, AppImage-рецепт
├── .github/workflows/
│   ├── build.yml            push/PR: сборка + smoke-тест на 3 ОС
│   └── release.yml          тег v*: сборка + загрузка файлов в Release
├── install-shepot.sh        остаётся для Linux (режим с GPU из исходников)
├── pyproject.toml           зависимости по ОС (environment markers)
└── .gitignore
```

- Для Linux меню и поведение остаются как сейчас (GNOME требует AppIndicator,
  под Wayland клавиши ловит только evdev).
- На Windows и macOS меню собирается из того же описания, но
  рисуется через `pystray`. Подменю «Модель», «Устройство», «Голос»,
  «Скорость» и «Клавиши» такие же.
- Клавиша по умолчанию: правый Ctrl — диктовка, правый Shift — чтение
  (на Mac: правый Option / правый Cmd — см. вопросы).

## 4. Сборка и релизы

- **PyInstaller** (onedir) на каждой ОС в её собственном раннере GitHub Actions:
  `windows-latest`, `macos-14` (arm64), `macos-13` (x86_64), `ubuntu-22.04`.
- Модели и голоса **в сборку не входят**: они скачиваются при первом запуске
  с прогрессом в трее, как и сейчас. Иначе размер вырастет на 3 ГБ.
- Артефакты релиза:
  - Windows: `SHEPOT-Setup-X.Y.Z.exe` (Inno Setup) + `SHEPOT-X.Y.Z-windows.zip`;
  - macOS: `SHEPOT-X.Y.Z-macos-arm64.dmg`, `SHEPOT-X.Y.Z-macos-x64.dmg`;
  - Linux: `SHEPOT-X.Y.Z-linux-x86_64.AppImage` (+ `install-shepot.sh` для GPU).
- `build.yml` — на каждый push: сборка + smoke-тест (запуск
  `shepot --selftest`: импорты, загрузка модели `tiny` на CPU, распознавание
  секунды тишины, выход без GUI).
- `release.yml` — на тег `v*`: всё то же + `softprops/action-gh-release`
  прикладывает файлы к релизу.

## 5. Этапы

1. **Git + GitHub**: `git init`, `.gitignore`, первый коммит, remote, push.
2. **Рефакторинг в пакет** `src/shepot/` (ядро + linux-платформа), поведение
   на Linux не меняется. Проверка: диктовка, чтение, меню как раньше.
3. **Режим `--selftest`** + `build.yml` только под Linux, чтобы CI заработал.
4. **Windows-платформа** + сборка в CI + ручная проверка на Windows.
5. **macOS-платформа** + разрешения + сборка в CI.
6. **Упаковка**: установщик, dmg, AppImage; `release.yml`; первый релиз `v0.1.0`.
7. Документация: README (установка под каждую ОС), `PROJECT.md`, `CONTEXT-shepot.md`.

## 6. Критерии проверки

- [ ] Linux: после рефакторинга диктовка, чтение, меню, смена модели и устройства работают как раньше.
- [ ] CI зелёный на трёх ОС, smoke-тест проходит.
- [ ] Тег `v0.1.0` создаёт Release с файлами для трёх ОС.
- [ ] Windows: скачал, установил, удержал клавишу, текст вставился в Блокнот; чтение работает.
- [ ] macOS: запрос разрешений понятен, после них диктовка вставляет текст в TextEdit.
- [ ] Linux AppImage запускается на чистой Ubuntu.

## 7. Риски и открытые вопросы

- **Проверка на реальных Windows/Mac.** CI проверит только сборку и
  распознавание. Клавиши, трей и вставку CI проверить не может: нужен
  ручной прогон на настоящей машине.
- **macOS без подписи** ($99/год Apple Developer): при первом запуске
  Gatekeeper ругается, запускать через ПКМ → «Открыть». При каждой
  пересборке система заново спрашивает разрешения.
- **Windows без подписи**: SmartScreen выдаёт «Неизвестный издатель» → «Подробнее» → «Выполнить в любом случае».
- **CUDA в сборке Windows** даёт +~1 ГБ (cuBLAS + cuDNN). Решение — см. вопросы.
- **Linux AppImage** — GTK/AppIndicator берутся из системы, evdev требует
  группу `input`, как и сейчас.

## 8. Решения разработчика (2026-09-25)

- **GPU**: Windows — одна сборка с CUDA (cuBLAS + cuDNN внутри, ~1.3 ГБ),
  переключение Авто / GPU / CPU в трее, как на Linux. Linux AppImage — так же
  (CUDA-библиотеки внутри). macOS — одна сборка, только CPU.
  Ограничение GitHub: файл в релизе до 2 ГБ — укладываемся.
- **Чтение вслух** на Windows/Mac — да, через буфер (Ctrl+C / Cmd+C →
  Piper → вернуть буфер). «От курсора до конца» — только Linux.
- **Формат релиза** — установщики: Windows `Setup.exe` (Inno Setup, ярлык,
  автозапуск) + zip; macOS `.dmg` (arm64 и x64); Linux AppImage.
- **Ручная проверка** — у разработчика есть Windows и Mac: после этапов 4 и 5
  он прогоняет чек-лист из раздела 6.
- **GitHub** — ссылку на репозиторий даёт разработчик; до этого всё
  делается локально (git init + коммиты), push — после получения ссылки.
