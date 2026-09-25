#!/usr/bin/env bash
# Сборка SHEPOT под macOS: PyInstaller → SHEPOT.app → .dmg (только CPU).
# Запускать из корня репозитория в venv, где стоит ".[dev]".
# Результат: release/SHEPOT-<версия>-macos-<arm64|x64>.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(python -c 'import sys; sys.path.insert(0, "src"); import shepot; print(shepot.__version__)')"
ARCH="$(uname -m)"
[ "$ARCH" = "x86_64" ] && ARCH="x64"
NAME="SHEPOT-${VERSION}-macos-${ARCH}"
mkdir -p release

echo "==> иконки"
python -m shepot.icons packaging/icons/generated

echo "==> PyInstaller"
pyinstaller packaging/shepot.spec --noconfirm --clean

echo "==> подпись ad-hoc (без Apple Developer ID; на Apple Silicon без подписи не запустится)"
codesign --force --deep --sign - dist/SHEPOT.app

echo "==> dmg"
STAGE=build/dmg
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R dist/SHEPOT.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # перетащить SHEPOT в «Программы»
hdiutil create -volname "SHEPOT" -srcfolder "$STAGE" -ov -format UDZO "release/${NAME}.dmg"

ls -lh release/
