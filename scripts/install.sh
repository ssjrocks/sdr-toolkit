#!/usr/bin/env bash
# Installs SDR Toolkit for the current user from this checkout: a venv in
# ~/.local/share/sdr-toolkit that uses the system's numpy, scipy and PyQt6
# (python3-numpy, python3-scipy, python3-pyqt6), plus the app-menu entry.
#
#   scripts/install.sh              install / update
#   scripts/install.sh --uninstall  remove (keeps ~/.local/state/sdr-toolkit)
set -euo pipefail

APP_ID=io.github.ssjrocks.SDRToolkit
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
ROOT="$DATA/sdr-toolkit"
VENV="$ROOT/venv"
DESKTOP="$DATA/applications/$APP_ID.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$ROOT"
  rm -f "$DESKTOP"
  update-desktop-database -q "$DATA/applications" 2>/dev/null || true
  echo "SDR Toolkit removed."
  exit 0
fi

command -v pw-record >/dev/null || { echo "pw-record not found: sudo apt install pipewire-bin" >&2; exit 1; }
# Outside decoders some modes use; the app says which is missing when you pick the mode.
missing=""
for p in jt9:wsjtx direwolf:direwolf multimon-ng:multimon-ng rtl_433:rtl-433 readsb:readsb rtl_fm:rtl-sdr; do
  command -v "${p%%:*}" >/dev/null || missing="$missing ${p##*:}"
done
[ -n "$missing" ] && echo "For all modes also install:  sudo apt install$missing"
if systemctl is-enabled readsb >/dev/null 2>&1; then
  echo "Note: the readsb system service is enabled; it holds the RTL-SDR whenever it's plugged in."
  echo "      The toolkit runs readsb itself. Turn the service off with: sudo systemctl disable --now readsb"
fi
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v uv >/dev/null; then
  uv venv -q --clear --system-site-packages --python /usr/bin/python3 "$VENV"
  uv pip install -q --python "$VENV/bin/python" "$src"
else
  python3 -m venv --clear --system-site-packages "$VENV"
  "$VENV/bin/pip" install -q "$src"
fi

mkdir -p "$(dirname "$DESKTOP")"
cat > "$DESKTOP" <<DESK
[Desktop Entry]
Version=1.1
Type=Application
Name=SDR Toolkit
Comment=Decode Morse, RTTY, PSK, NAVTEX, FT8/FT4, SSTV, weather fax, APRS, pagers and tones from any audio
Exec=$VENV/bin/python -m sdr_toolkit
Icon=/usr/share/icons/Adwaita/scalable/devices/audio-headset.svg
Terminal=false
Categories=AudioVideo;Audio;HamRadio;
StartupWMClass=$APP_ID
DESK
update-desktop-database -q "$DATA/applications" 2>/dev/null || true
echo "Installed. Open SDR Toolkit from the app menu, or run: $VENV/bin/python -m sdr_toolkit"
