#!/usr/bin/env bash
set -euo pipefail

VENV="$HOME/venvs/shepot"
BIN="$HOME/bin"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
mkdir -p "$BIN" "$APPS" "$AUTOSTART"

echo "==> 1/6 системные пакеты"
sudo apt-get update -qq || true
sudo apt-get install -y python3-venv python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
                        portaudio19-dev xdotool xclip
sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1 \
                        gnome-shell-extension-appindicator || true
sudo apt-get install -y wl-clipboard wtype || true

echo "==> 2/6 python-окружение"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q faster-whisper sounddevice numpy evdev
"$VENV/bin/pip" install -q nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
SITE="$("$VENV/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo "/usr/lib/python3/dist-packages" > "$SITE/system-gi.pth"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> 3/6 демон с иконкой в трее"
install -m 755 "$SCRIPT_DIR/shepot.py" "$BIN/shepot.py"
install -m 755 "$SCRIPT_DIR/shepot_reader.py" "$BIN/shepot_reader.py"

echo "==> 4/6 обёртка запуска"
install -m 755 "$SCRIPT_DIR/shepot-run.sh" "$BIN/shepot-run.sh"

echo "==> 5/6 ярлык и автозапуск"
cat > "$APPS/shepot.desktop" <<DESKEOF
[Desktop Entry]
Type=Application
Name=SHEPOT
Comment=Диктовка и чтение вслух (push-to-talk)
Exec=$BIN/shepot-run.sh
Icon=audio-input-microphone
Terminal=false
StartupNotify=false
Categories=Utility;AudioVideo;
DESKEOF
chmod +x "$APPS/shepot.desktop"
cp "$APPS/shepot.desktop" "$AUTOSTART/shepot.desktop"
update-desktop-database "$APPS" 2>/dev/null || true

echo "==> 6/6 права и расширение GNOME"
id -nG | tr ' ' '\n' | grep -qx input || sudo usermod -aG input "$USER"
gnome-extensions enable ubuntu-appindicators@ubuntu.com 2>/dev/null \
  || gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com 2>/dev/null || true

echo
echo "Готово."
echo "Перелогинься (Log Out / reboot), затем запусти «SHEPOT» из меню приложений."
echo "Дальше стартует сама при входе в систему."
