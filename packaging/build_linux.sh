#!/usr/bin/env bash
# Сборка SHEPOT под Linux: PyInstaller → AppImage + tar.gz.
# Запускать из корня репозитория в venv, где стоит ".[gpu,gtk,dev]".
# Результат: release/SHEPOT-<версия>-linux-x86_64.AppImage и .tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(python -c 'import sys; sys.path.insert(0, "src"); import shepot; print(shepot.__version__)')"
NAME="SHEPOT-${VERSION}-linux-x86_64"
mkdir -p release

echo "==> иконки"
python -m shepot.icons packaging/icons/generated

echo "==> PyInstaller"
pyinstaller packaging/shepot.spec --noconfirm --clean

echo "==> tar.gz"
cp packaging/linux/AppRun dist/SHEPOT/shepot.sh
chmod +x dist/SHEPOT/shepot.sh
tar -C dist -czf "release/${NAME}.tar.gz" SHEPOT

echo "==> AppImage"
APPDIR=build/AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib"
cp -a dist/SHEPOT "$APPDIR/usr/lib/shepot"
rm -f "$APPDIR/usr/lib/shepot/shepot.sh"
install -m 755 packaging/linux/AppRun "$APPDIR/AppRun"
cp packaging/linux/shepot.desktop "$APPDIR/shepot.desktop"
cp packaging/icons/generated/shepot.png "$APPDIR/shepot.png"
TOOL=build/appimagetool
if [ ! -x "$TOOL" ]; then
    curl -fsSL -o "$TOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$TOOL"
fi
# без FUSE на машине сборки: инструмент распаковывает сам себя
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "$TOOL" --comp zstd "$APPDIR" "release/${NAME}.AppImage"

ls -lh release/
