# SHEPOT — контекст проекта для продолжения работы

Документ для передачи в Claude Code. Описывает текущее состояние push-to-talk
диктовки на локальном Whisper, что работает, что сломано и что делать дальше.

> 2026-09-25: проект переименован из `ptt-whisper` в **SHEPOT**. Файлы, пути
> (`~/bin/shepot*`, `~/venvs/shepot`, `~/.config/shepot`, `~/.local/share/shepot`,
> `~/shepot-log.txt`) и переменные окружения (`PTT_*` → `SHEPOT_*`) — новые.
>
> 2026-09-25: **кроссплатформенность.** Код перенесён в пакет `src/shepot/`
> (ядро + платформы: Linux — evdev/GTK, Windows и macOS — pynput/pystray),
> репозиторий `github.com/xakus/SHEPOT`, сборка PyInstaller в GitHub Actions
> на 4 цели (Windows x64 с CUDA, Linux x86_64 с CUDA, macOS arm64/x64 — CPU),
> релизы по тегу `vX.Y.Z`. План — `docs/plans/cross-platform/PLAN.md`.
> На Linux-машине разработчика демон теперь `python3 -m shepot` из venv
> (`~/bin/shepot.py` больше не используется).

---

## 1. Задача

Демон под Linux, который:
- постоянно висит в фоне, держит модель Whisper в VRAM;
- по удержанию правого Ctrl пишет с микрофона;
- по отпусканию распознаёт речь и вставляет текст туда, где стоит курсор;
- показывает иконку в верхней панели GNOME (не в доке), оттуда можно выключить;
- запускается двойным кликом и стартует при входе в систему.

Язык диктовки — русский.

---

## 2. Железо и окружение

| Параметр | Значение |
|---|---|
| GPU | NVIDIA GeForce GTX 1080 Ti, 11 GB, Pascal (sm_61) |
| ОС | Ubuntu, GNOME |
| Сессия | **Wayland** (`XDG_SESSION_TYPE=wayland`) — источник основных проблем |
| Python | 3.14 (системный), venv в `~/venvs/shepot` |
| Пользователь | `xakus`, состоит в группе `input` (gid 994) |

### Критичные ограничения Pascal
- `compute_type="float16"` **не поддерживается** в CTranslate2 на CC < 7.0 → используем `int8`.
- Драйверная ветка 580 — последняя с поддержкой Pascal в Linux. На 590 карта отвалится.
- CUDA 13 не поддерживает Pascal. Нужно оставаться на CUDA 12.x + cuDNN 9.
- Если cuDNN 9 сломается: откат `pip install --force-reinstall ctranslate2==4.4.0 "nvidia-cudnn-cu12==8.*"`.

---

## 3. Где что лежит

```
~/Documents/projects/SHEPOT/src/shepot/    исходники пакета  ← источник правды
~/venvs/shepot/                              venv: пакет shepot + faster-whisper, piper, nvidia-*
~/bin/shepot-run.sh                          обёртка: активирует venv, ставит LD_LIBRARY_PATH, python3 -m shepot
~/bin/ptt-whisper.py                      ранняя консольная версия без трея (рабочая)
install-shepot.sh (в проекте)                установщик для Linux из исходников
~/.local/share/applications/shepot.desktop   ярлык в меню приложений
~/.config/autostart/shepot.desktop   автозапуск — УДАЛЁН на время отладки
~/.config/systemd/user/ydotoold.service   юнит для ydotoold (в пакете Ubuntu его нет)
~/shepot-log.txt                             лог распознанного текста (добавлен последним патчем)
~/.cache/huggingface/hub/                 веса large-v3, ~3 GB
```

### shepot-run.sh
```bash
#!/bin/bash
source "$HOME/venvs/shepot/bin/activate"
SITE=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
export LD_LIBRARY_PATH="$(ls -d $SITE/nvidia/*/lib 2>/dev/null | paste -sd:)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec python3 -m shepot "$@"
```

### Переменные окружения
| Переменная | По умолчанию | Назначение |
|---|---|---|
| `SHEPOT_KEY` | `KEY_RIGHTCTRL` (Mac: `KEY_RIGHTALT` = правый Option) | клавиша push-to-talk; меню трея важнее |
| `SHEPOT_TTS_KEY` | Linux `KEY_RIGHTALT`, Windows `KEY_RIGHTSHIFT`, Mac `KEY_RIGHTMETA` (правый Cmd) | клавиша чтения; меню трея важнее |
| `SHEPOT_PLATFORM` | — | `desktop` — трей Windows/macOS (pystray) на Linux, для отладки |
| `SHEPOT_MODEL` | `large-v3` | размер модели |
| `SHEPOT_LANG` | `ru` | язык, пусто = автоопределение |
| `SHEPOT_COMPUTE` | `int8` | тип вычислений (для Pascal только int8) |
| `SHEPOT_DEVICE` | `auto` | `auto` / `cuda` / `cpu`; выбор в меню трея «Устройство» (хранится в `config.json`) важнее. `auto` и `cuda` без GPU или при ошибке загрузки откатываются на CPU (`int8`) |
| `SHEPOT_PROMPT` | `""` | initial_prompt — термины и имена собственные |
| `SHEPOT_PASTE` | `29:1 47:1 47:0 29:0` | скан-коды Ctrl+V для ydotool |
| `SHEPOT_CLIPBOARD_ONLY` | — | если задана, только копирование без вставки |
| `SHEPOT_CHUNK` | `30` | макс. длина чанка при стриминговом распознавании, сек |
| `SHEPOT_LOG` | Linux `~/shepot-log.txt`, иначе в папке данных | лог распознанных чанков (страховка от потери) |
| `SHEPOT_MODEL` | `large-v3` | модель по умолчанию; выбор из меню трея имеет приоритет |

### Менеджер моделей в трее (добавлен 2026-08-22)

Подменю «Модель: <имя>» в меню трея:
- список всех моделей faster-whisper (`available_models()` + найденные на
  HuggingFace по «Обновить список»); у каждой — размер (факт на диске для
  скачанных, размер скачивания для остальных) и статус скачана/скачать;
- выбор нескачанной модели автоматически качает её (прогресс в % в строке
  статуса) и загружает в GPU; выбор сохраняется в
  `~/.config/shepot/config.json` и переживает перезапуск;
- «Удалить скачанную» (активную нельзя), «Добавить локальную модель…»
  (папка с `model.bin`), «Открыть папку моделей» (`~/.cache/huggingface/hub`).

Архитектурно: поток-транскрайбер один владеет моделью; смена модели — это
задание `("switch", name)` в той же очереди `jobs`, что и чанки, поэтому
блокировки не нужны, а первая загрузка при старте идёт тем же путём.
При ошибке загрузки — автооткат на прежнюю модель.

---

## 4. Архитектура (исторически — shepot.py; актуальная раскладка пакета — в CLAUDE.md)

```
Gtk.main() в главном потоке  →  AppIndicator (иконка в панели)
       │
       └─ worker() в фоновом потоке:
              WhisperModel(large-v3, cuda, int8)   ← прогрев пустым аудио
              find_keyboards(KEY_RIGHTCTRL)        ← evdev, /dev/input/event*
              Recorder                             ← sounddevice, поток открыт постоянно
              selectors loop:
                  key down → rec.start(), иконка "запись"
                  key up   → rec.stop() → отдельный поток:
                                 model.transcribe(...)
                                 запись в ~/shepot-log.txt
                                 typer(text)
```

Ключевые классы и функции:
- `Recorder` — постоянно открытый `sd.InputStream`, кадры копятся только между `start()`/`stop()`.
- `make_typer()` — выбирает способ ввода текста. **Здесь основная проблема, см. раздел 6.**
- `find_keyboards(key)` — перебирает `/dev/input/event*`, ищет устройства с нужной клавишей.
- `Tray` — AppIndicator, меню: статус, последняя фраза, чекбокс «Слушать», выход.

Параметры распознавания:
```python
model.transcribe(audio, language="ru", beam_size=1, vad_filter=True,
                 condition_on_previous_text=False, initial_prompt=...)
```

---

## 5. Что уже работает

- CUDA + faster-whisper на 1080 Ti с `int8`. Проверено: **20.5 с аудио → 1.2 с обработки**.
- Качество распознавания русского на `large-v3` хорошее.
- Захват глобальных нажатий через evdev работает и под Wayland.
- Иконка в верхней панели GNOME отображается, меню работает, окна в доке нет.
- Микрофон пишет (после того, как разобрались с устройством ввода).

---

## 6. ГЛАВНАЯ НЕРЕШЁННАЯ ПРОБЛЕМА: ввод текста под GNOME Wayland

Перепробовано, все три пути не работают:

| Способ | Результат |
|---|---|
| `xdotool type` | под Wayland не работает в принципе |
| `wtype` | `Compositor does not support the virtual keyboard protocol` — GNOME не реализует нужный протокол |
| `ydotool key` (Ctrl+V через буфер) | демон `ydotoold` работает, `/dev/uinput` = `root:input 0660`, но нажатия не доходят до композитора; `ydotool type` с кириллицей выдавал только `.   .` (он шлёт скан-коды, Unicode не умеет) |

Буфер обмена сам по себе исправен: `wl-copy "тест" && wl-paste` возвращает `тест`.

**UPD 2026-08-22: ydotool Ctrl+V у пользователя работает**, но был race:
`wl-copy` асинхронный и завладевает буфером только через 150–250 мс (замерено),
а Ctrl+V нажимался через фиксированные 0.15 с → часто вставлялось СТАРОЕ
содержимое буфера (кусок предыдущей диктовки) — выглядело как «текст
укорачивается». Фикс в `make_typer()`: после `wl-copy` ждём подтверждения
через `wl-paste` (сверка содержимого, таймаут 3 с) и только потом Ctrl+V.
Каждая вставка логируется в stdout демона строкой `ВСТАВКА: N символов`.

### Что осталось проверить по ydotool
```bash
sleep 5; ydotool key --key-delay 50 29:1 47:1 47:0 29:0     # Ctrl+V с задержкой
sleep 5; ydotool key --key-delay 50 42:1 110:1 110:0 42:0   # Shift+Insert
sleep 5; ydotool key --key-delay 50 30:1 30:0               # просто буква "a"
```
Если даже третья не печатает — GNOME Wayland блокирует ydotool, и путь тупиковый.

### Рекомендованное решение
**Переключиться на сессию Xorg.** Log Out → шестерёнка на экране входа → **Ubuntu on Xorg**.
Тогда `XDG_SESSION_TYPE=x11`, и `make_typer()` сам выберет `xdotool`, который печатает
Unicode напрямую в позицию курсора. Код для этой ветки уже написан и не требует правок.

### Запасной путь
`SHEPOT_CLIPBOARD_ONLY=1` — текст только копируется в буфер, вставка вручную по Ctrl+V.
Работает всегда, но требует лишнего действия.

---

## 7. Второстепенные проблемы

**Длинная диктовка теряется — РЕШЕНО (2026-08-22).** Запись теперь режется на
чанки по ~30 с (разрез — в самом тихом 100-мс окне между `CHUNK_MIN` и `CHUNK_MAX`,
чтобы не рубить слово) и распознаётся параллельно с записью в потоке-транскрайбере
(очередь `jobs`, один поток — порядок чанков сохранён). При отпускании клавиши
дораспознаётся только хвост, затем весь текст склеивается и вставляется одним
куском. Каждый распознанный чанк сразу дописывается в `~/shepot-log.txt` (путь —
`SHEPOT_LOG`) — страховка от потери. Логика резки покрыта тестом (65 с синтетики
собираются из чанков и хвоста без потери сэмплов).

**Имена собственные.** «SHAD» распознаётся как «Shut Up». Лечится `SHEPOT_PROMPT`:
```bash
SHEPOT_PROMPT="Проект SHAD, GÜVƏN, Flutter, Vue.js, Spring Boot, Docker Swarm, PostgreSQL, ClickHouse."
```

**Bluetooth-гарнитура.** Профили A2DP (хороший звук, микрофон выключен) и HSP/HFP
(микрофон есть, звук телефонного качества) взаимоисключающи — ограничение самого Bluetooth.
Для диктовки лучше отдельный USB или встроенный микрофон. Выбор устройства можно
прибить через `sd.InputStream(device=N)`; номера смотреть в `sd.query_devices()`.

**Несколько копий демона.** Автозапуск + ручной запуск конкурируют за микрофон,
вторая копия получает пустой поток. Перед отладкой всегда: `pkill -f "python3 -m shepot$"`.

**Репозиторий k6** ломает `apt-get update` (`NO_PUBKEY C780D0BDB1A69C86`).
Не связано с проектом, но валит установщик из-за `set -e`.

---

## 8. Ближайшие шаги

1. Переключиться на Xorg, проверить `xdotool` — это закрывает главную проблему.
2. Вернуть автозапуск: `cp ~/.local/share/applications/shepot.desktop ~/.config/autostart/`.
3. Добавить `SHEPOT_MIC` для явного выбора устройства ввода.
4. ~~Переписать на потоковое распознавание чанками~~ — СДЕЛАНО 2026-08-22 (см. раздел 7).
5. Прописать `SHEPOT_PROMPT` с рабочими терминами в `.desktop` через `Exec=env SHEPOT_PROMPT=... ...`.
6. **Чтение текста вслух (TTS)** по правому Alt — план согласован 2026-09-14,
   см. `docs/plans/tts-reading/PLAN.md` (Piper на CPU, AT-SPI → клавиши+буфер,
   новый модуль `shepot_reader.py`, диктовка не меняется). — СДЕЛАНО, теперь `src/shepot/reader.py`.
7. **Windows / macOS + GitHub Actions + релизы** — 2026-09-25, см.
   `docs/plans/cross-platform/PLAN.md`. Код и CI готовы; нужна ручная проверка
   на настоящих Windows и Mac (клавиши, трей, вставка, чтение, разрешения macOS).

`install-shepot.sh` ставит пакет из папки проекта в venv (`pip install ".[gpu]"`)
и копирует `shepot-run.sh` в `~/bin`. Источник правды — файлы проекта.

---

## 9. Полезные команды

```bash
pkill -f "python3 -m shepot$"            # убить все копии (не `pkill -f shepot` — заденет shell)
pgrep -af "python3 -m shepot"            # проверить, что не осталось
~/bin/shepot-run.sh                           # запуск с выводом в терминал
echo $XDG_SESSION_TYPE                     # x11 или wayland
systemctl --user status ydotoold           # состояние демона ydotool
nvidia-smi                                 # проверить, что модель села на GPU (~2 GB)
source ~/venvs/shepot/bin/activate && python3 -c "import sounddevice as sd; print(sd.query_devices())"
```
