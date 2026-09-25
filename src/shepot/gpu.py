"""Подготовка библиотек CUDA (cuBLAS, cuDNN) до импорта ctranslate2.

cuBLAS и cuDNN ставятся pip-пакетами nvidia-cublas-cu12 / nvidia-cudnn-cu12
и лежат в site-packages/nvidia/*/bin (Windows) или nvidia/*/lib (Linux).
Сами по себе они не находятся:
- Windows — добавляем папки через os.add_dll_directory (и в PATH);
- Linux — путь должен быть в LD_LIBRARY_PATH ещё до старта процесса,
  его выставляет обёртка запуска (shepot-run.sh или AppRun в AppImage).
macOS — CUDA нет, ничего не делаем.
"""

import glob
import os
import site
import sys

from .paths import IS_WIN


def _roots():
    """Где искать папку nvidia/: внутри сборки PyInstaller или в site-packages."""
    if getattr(sys, "frozen", False):
        return [getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))]
    roots = list(site.getsitepackages())
    user = site.getusersitepackages()
    if isinstance(user, str):
        roots.append(user)
    return roots


def cuda_lib_dirs():
    """Все найденные папки с библиотеками CUDA из пакетов nvidia-*."""
    sub = "bin" if IS_WIN else "lib"
    dirs = []
    for root in _roots():
        dirs += glob.glob(os.path.join(root, "nvidia", "*", sub))
    return dirs


def prepare_cuda_libs():
    """Сделать DLL CUDA видимыми для ctranslate2 (только Windows). Возвращает найденные папки."""
    dirs = cuda_lib_dirs()
    if IS_WIN:
        for d in dirs:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
        if dirs:
            os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
    return dirs
