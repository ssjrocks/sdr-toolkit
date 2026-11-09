#!/usr/bin/env bash
# Builds mbelib (AMBE/IMBE voice codecs, github.com/lwvmobile/mbelib) and DSD-FME (DMR, P25,
# NXDN, D-Star, YSF... github.com/lwvmobile/dsd-fme) for the DMR/P25 mode, into
# ~/.local/share/sdr-toolkit/dsd-fme. Nothing is installed system-wide.
#
# mbelib's patent notice: "Executable objects compiled or derived from this package may be
# covered by one or more patents." Read https://github.com/lwvmobile/mbelib#readme first.
#
# Needs: sudo apt install cmake build-essential libpulse-dev libsndfile1-dev libfftw3-dev \
#        liblapack-dev libusb-1.0-0-dev librtlsdr-dev libncurses-dev libcodec2-dev
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ext="$here/build/ext"
prefix="$HOME/.local/share/sdr-toolkit/dsd-fme"
jobs=$(( $(nproc) / 3 )); (( jobs < 1 )) && jobs=1
mkdir -p "$ext"

if [[ ! -d "$ext/mbelib" ]]; then
  git clone https://github.com/lwvmobile/mbelib "$ext/mbelib"
  git -C "$ext/mbelib" checkout ambe_tones
fi
cmake -S "$ext/mbelib" -B "$ext/mbelib/build" -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_BUILD_TYPE=Release
make -C "$ext/mbelib/build" -j"$jobs"
make -C "$ext/mbelib/build" install

if [[ ! -d "$ext/dsd-fme" ]]; then
  git clone --depth 1 https://github.com/lwvmobile/dsd-fme "$ext/dsd-fme"
fi
cmake -S "$ext/dsd-fme" -B "$ext/dsd-fme/build" -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH="$prefix" -DCMAKE_INSTALL_RPATH="$prefix/lib" -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON
make -C "$ext/dsd-fme/build" -j"$jobs"
make -C "$ext/dsd-fme/build" install
echo "DSD-FME installed: $prefix/bin/dsd-fme"
