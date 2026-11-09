#!/usr/bin/env bash
# Builds gr-lora_sdr (EPFL's LoRa modem for GNU Radio, github.com/tapparelj/gr-lora_sdr) for the
# LoRa mode, into ~/.local/share/sdr-toolkit/gr-lora_sdr. Needs: sudo apt install gnuradio-dev cmake
# libvolk-dev pybind11-dev. Takes a few minutes and about 2 GB of memory per compile job.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$here/build/ext/gr-lora_sdr"
prefix="$HOME/.local/share/sdr-toolkit/gr-lora_sdr"
if [[ ! -d "$src" ]]; then
  mkdir -p "$(dirname "$src")"
  git clone --depth 1 https://github.com/tapparelj/gr-lora_sdr.git "$src"
fi
cmake -S "$src" -B "$src/build" -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_BUILD_TYPE=Release
jobs=$(( $(nproc) / 3 )); (( jobs < 1 )) && jobs=1
make -C "$src/build" -j"$jobs"
make -C "$src/build" install
echo "gr-lora_sdr installed in $prefix"
