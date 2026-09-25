# -*- mode: python ; coding: utf-8 -*-
"""Рецепт PyInstaller для SHEPOT — один на Linux, Windows и macOS.

Сборка (из корня репозитория, в venv с установленным пакетом):
    python -m shepot.icons packaging/icons/generated
    pyinstaller packaging/shepot.spec --noconfirm

Результат: dist/SHEPOT/ (Linux, Windows) или dist/SHEPOT.app (macOS).
Переменные:
    SHEPOT_BUILD_NO_CUDA=1  — не класть cuBLAS/cuDNN (сборка без GPU, меньше на ~1 ГБ).
"""

import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICONS = os.path.join(ROOT, "packaging", "icons", "generated")
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
WITH_CUDA = not IS_MAC and not os.environ.get("SHEPOT_BUILD_NO_CUDA")

sys.path.insert(0, os.path.join(ROOT, "src"))
from shepot import __version__  # noqa: E402

datas, binaries, hiddenimports = [], [], []

# пакеты с данными и нативными библиотеками: модели VAD, espeak-ng-data, .so/.dll
for pkg in ["faster_whisper", "ctranslate2", "onnxruntime", "piper", "tokenizers", "av",
            "sounddevice", "_sounddevice_data", "huggingface_hub"]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:   # пакета нет на этой ОС (например _sounddevice_data на Linux)
        print(f"collect_all({pkg}) пропущен: {e}")

hiddenimports += collect_submodules("shepot")

if IS_LINUX:
    hiddenimports += ["gi", "gi.repository.Gtk", "gi.repository.GLib",
                      "gi.repository.AyatanaAppIndicator3", "gi.repository.Atspi", "evdev"]
    # PortAudio с машины сборки (apt install libportaudio2): у пользователя его может не быть
    for p in ["/usr/lib/x86_64-linux-gnu/libportaudio.so.2", "/usr/lib/libportaudio.so.2"]:
        if os.path.exists(p):
            binaries.append((p, "."))
            break
else:
    for pkg in ["pynput", "pystray", "pyperclip"]:
        hiddenimports += collect_submodules(pkg)
    if IS_MAC:
        hiddenimports += ["PyObjCTools.AppHelper", "ApplicationServices", "AppKit", "Quartz"]

# cuBLAS + cuDNN из pip-пакетов nvidia-* — в ту же структуру nvidia/<имя>/<bin|lib>,
# где их ищет shepot.gpu (Windows) и AppRun (Linux)
if WITH_CUDA:
    import site
    sub = "bin" if IS_WIN else "lib"
    for sp in site.getsitepackages():
        for d in glob.glob(os.path.join(sp, "nvidia", "*", sub)):
            name = os.path.basename(os.path.dirname(d))
            for f in glob.glob(os.path.join(d, "*")):
                if f.endswith((".dll", ".so")) or ".so." in f:
                    binaries.append((f, f"nvidia/{name}/{sub}"))

a = Analysis(
    [os.path.join(ROOT, "src", "shepot", "__main__.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[os.path.join(ROOT, "packaging", "hooks")],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = None
if IS_WIN:
    icon = os.path.join(ICONS, "shepot.ico")
elif IS_MAC:
    icon = os.path.join(ICONS, "shepot.icns")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SHEPOT",
    console=False,          # без окна консоли; вывод — в shepot-console.log
    upx=False,
    icon=icon,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SHEPOT", upx=False)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="SHEPOT.app",
        icon=icon,
        bundle_identifier="io.github.xakus.shepot",
        version=__version__,
        info_plist={
            "CFBundleName": "SHEPOT",
            "CFBundleDisplayName": "SHEPOT",
            "CFBundleShortVersionString": __version__,
            "LSUIElement": True,          # только значок в строке меню, без Dock
            "LSMinimumSystemVersion": "12.0",
            "NSMicrophoneUsageDescription":
                "SHEPOT записывает речь с микрофона, пока зажата клавиша диктовки, "
                "и превращает её в текст прямо на этом компьютере.",
            "NSAppleEventsUsageDescription":
                "SHEPOT вставляет распознанный текст в активное окно.",
        },
    )
