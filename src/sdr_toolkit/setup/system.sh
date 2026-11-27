#!/usr/bin/env bash
# Ubuntu packages SDR Toolkit uses: the app itself, the SDRs' device permissions, tshark, and what
# build.sh needs to compile DSD-FME. The decoders come in the app's tools bundle (its own folder), not
# from here. Run as root (the installer uses sudo, the app pkexec). Also turns off readsb's own service, which would hold the
# RTL-SDR whenever it's plugged in (the toolkit runs readsb itself).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "system.sh must run as root" >&2; exit 1; }

packages=(
  # the app
  pipewire-bin python3-venv python3-numpy python3-scipy python3-pyqt6 python3-pil python3-cryptography
  # the SDRs' device permissions (their udev rules); the decoders themselves come in the app's tools bundle
  librtlsdr0 libhackrf0
  # Wi-Fi monitor mode's frames (it needs the system's capture permissions)
  tshark
  # to build DSD-FME and mbelib (kept out of the bundle by mbelib's patent notice)
  git cmake build-essential pkg-config libpulse-dev libsndfile1-dev libfftw3-dev liblapack-dev libusb-1.0-0-dev
  librtlsdr-dev libncurses-dev libcodec2-dev
)
missing=()
for p in "${packages[@]}"; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
done
if (( ${#missing[@]} )); then
  echo "STEP Installing ${#missing[@]} Ubuntu packages: ${missing[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q "${missing[@]}"
else
  echo "STEP Ubuntu packages already installed"
fi
if systemctl is-enabled readsb >/dev/null 2>&1 || systemctl is-active readsb >/dev/null 2>&1; then
  echo "STEP Turning off the readsb service (the toolkit starts readsb itself)"
  systemctl disable --now readsb
fi
if systemctl is-enabled wmbusmeters >/dev/null 2>&1 || systemctl is-active wmbusmeters >/dev/null 2>&1; then
  echo "STEP Turning off the wmbusmeters service (the toolkit starts wmbusmeters itself)"
  systemctl disable --now wmbusmeters
fi
echo "STEP System setup done"
