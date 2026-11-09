#!/usr/bin/env bash
# Installs SDR Toolkit for the current user, with everything its modes use:
#   - Ubuntu packages: the app's libraries, WSJT-X, direwolf, multimon-ng, rtl_433, readsb,
#     the RTL-SDR tools, GNU Radio, and what the builds below need (asks for your password once);
#   - gr-lora_sdr (LoRa), and mbelib + DSD-FME (DMR/P25), built from source into
#     ~/.local/share/sdr-toolkit (a few minutes);
#   - the app in ~/.local/share/sdr-toolkit/venv, and its app-menu entry.
#
#   From a checkout:  scripts/install.sh
#   Anywhere:         curl -fsSL https://raw.githubusercontent.com/ssjrocks/sdr-toolkit/master/scripts/install.sh | bash
#   Options:          --accept-mbelib   build DMR/P25 support without asking about mbelib's patent notice
#                     --no-dmr          leave out mbelib and DSD-FME
#                     --uninstall       remove the app and everything it built (decoded pictures stay)
set -euo pipefail

APP_ID=io.github.ssjrocks.SDRToolkit
REPO=ssjrocks/sdr-toolkit
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
ROOT="$DATA/sdr-toolkit"
VENV="$ROOT/venv"
DESKTOP="$DATA/applications/$APP_ID.desktop"

mbelib=ask
for a in "$@"; do
  case "$a" in
    --uninstall)
      rm -rf "$ROOT" "$HOME/.cache/sdr-toolkit"
      rm -f "$DESKTOP"
      update-desktop-database -q "$DATA/applications" 2>/dev/null || true
      echo "SDR Toolkit removed."
      exit 0 ;;
    --accept-mbelib) mbelib=yes ;;
    --no-dmr) mbelib=no ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

step() { printf '\n== %s\n' "$*"; }

# Install from this checkout when run from one, otherwise from the latest release.
src=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "$here/../pyproject.toml" ]] && src="$(cd "$here/.." && pwd)"
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
if [[ -n "$src" ]]; then
  setup="$src/src/sdr_toolkit/setup"
  target="$src"
else
  step "Downloading the latest release"
  read -r wheel_url sha < <(python3 - "$REPO" <<'EOF'
import json, sys, urllib.request
repo = sys.argv[1]
rel = json.load(urllib.request.urlopen(f"https://api.github.com/repos/{repo}/releases/latest"))
assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
info = json.load(urllib.request.urlopen(assets["sdr-toolkit.json"]))
print(assets[info["wheel"]], info["sha256"])
EOF
)
  wheel="$tmp/$(basename "$wheel_url")"
  curl -fsSL -o "$wheel" "$wheel_url"
  echo "$sha  $wheel" | sha256sum -c --quiet
  python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2], [n for n in zipfile.ZipFile(sys.argv[1]).namelist() if '/setup/' in n])" "$wheel" "$tmp/x"
  setup="$tmp/x/sdr_toolkit/setup"
  target="$wheel"
fi

step "Ubuntu packages (asks for your password)"
sudo bash "$setup/system.sh"

step "The app"
mkdir -p "$ROOT"
if command -v uv >/dev/null; then
  uv venv -q --clear --system-site-packages --python /usr/bin/python3 "$VENV"
  uv pip install -q --python "$VENV/bin/python" "$target"
else
  python3 -m venv --clear --system-site-packages "$VENV"
  "$VENV/bin/pip" install -q "$target"
fi
touch "$VENV/sdr-toolkit-venv"     # tells the in-app updater it may update this venv

if [[ "$mbelib" == ask ]]; then
  if [[ -r /dev/tty ]]; then
    cat > /dev/tty <<'EOF'

DMR / P25 voice needs mbelib, the AMBE/IMBE voice codec library. Its authors' notice:
  "This source code is provided for educational purposes only. [...] Executable objects compiled
   or derived from this package may be covered by one or more patents. Readers are strongly
   advised to check for any patent restrictions or licencing requirements before compiling or
   using this source code."
EOF
    read -r -p "Build DMR / P25 support with mbelib? [y/N] " answer < /dev/tty
    [[ "$answer" =~ ^[Yy] ]] && mbelib=yes || mbelib=no
  else
    mbelib=no
    echo "No terminal to ask about mbelib: leaving out DMR/P25 (the app can add it later)."
  fi
fi
step "Decoders built from source (a few minutes)"
flags=()
[[ "$mbelib" == yes ]] && flags+=(--accept-mbelib)
bash "$setup/build.sh" "${flags[@]}"

step "App-menu entry"
mkdir -p "$(dirname "$DESKTOP")"
cat > "$DESKTOP" <<DESK
[Desktop Entry]
Version=1.1
Type=Application
Name=SDR Toolkit
Comment=Decode Morse, RTTY, PSK, NAVTEX, FT8/FT4, SSTV, weather fax, APRS, pagers, ADS-B, AIS, LoRa, DMR and more
Exec=$VENV/bin/python -m sdr_toolkit
Icon=/usr/share/icons/Adwaita/scalable/devices/audio-headset.svg
Terminal=false
Categories=AudioVideo;Audio;HamRadio;
StartupWMClass=$APP_ID
DESK
update-desktop-database -q "$DATA/applications" 2>/dev/null || true
echo
echo "Installed. Open SDR Toolkit from the app menu."
