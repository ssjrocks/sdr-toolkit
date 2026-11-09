#!/usr/bin/env bash
# Builds the decoders Ubuntu doesn't package, into ~/.local/share/sdr-toolkit, as the user:
#   gr-lora_sdr (EPFL's LoRa modem for GNU Radio, github.com/tapparelj/gr-lora_sdr)
#   mbelib (AMBE/IMBE voice codecs, github.com/lwvmobile/mbelib) and DSD-FME (github.com/lwvmobile/dsd-fme)
# Parts already built are skipped; --rebuild builds them again.
#
# mbelib carries a patent notice: "Executable objects compiled or derived from this package may be
# covered by one or more patents." It's built only with --accept-mbelib (the installer and the app
# ask first).
set -euo pipefail
rebuild=0 mbelib=0
for a in "$@"; do
  case "$a" in
    --rebuild) rebuild=1 ;;
    --accept-mbelib) mbelib=1 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done
root="$HOME/.local/share/sdr-toolkit"
src="${SDR_TOOLKIT_SRC:-$HOME/.cache/sdr-toolkit/src}"
jobs=$(( $(nproc) / 3 )); (( jobs < 1 )) && jobs=1
mkdir -p "$src"
# Compiles run at low priority so the desktop stays responsive.
build() { nice -n 10 make -C "$1" -j"$jobs"; make -C "$1" install; }
fetch() {   # fetch DIR URL [BRANCH]
  if [[ ! -d "$src/$1/.git" ]]; then
    rm -rf "${src:?}/$1"
    git clone -q ${3:+--branch "$3"} --depth 1 "$2" "$src/$1"
  fi
}

lora="$root/gr-lora_sdr"
if (( rebuild )) || ! compgen -G "$lora/lib*/python3*/*-packages/gnuradio/lora_sdr" >/dev/null; then
  echo "STEP Building gr-lora_sdr (LoRa), a few minutes"
  fetch gr-lora_sdr https://github.com/tapparelj/gr-lora_sdr.git
  cmake -S "$src/gr-lora_sdr" -B "$src/gr-lora_sdr/build" -DCMAKE_INSTALL_PREFIX="$lora" \
        -DCMAKE_BUILD_TYPE=Release >/dev/null
  build "$src/gr-lora_sdr/build" >/dev/null
  echo "STEP gr-lora_sdr installed"
else
  echo "STEP gr-lora_sdr already built"
fi

dsd="$root/dsd-fme"
if (( rebuild )) || [[ ! -x "$dsd/bin/dsd-fme" ]]; then
  if (( ! mbelib )); then
    echo "STEP Skipping DSD-FME (DMR/P25): mbelib's patent notice wasn't accepted"
  else
    echo "STEP Building mbelib and DSD-FME (DMR/P25), a few minutes"
    fetch mbelib https://github.com/lwvmobile/mbelib ambe_tones
    cmake -S "$src/mbelib" -B "$src/mbelib/build" -DCMAKE_INSTALL_PREFIX="$dsd" -DCMAKE_BUILD_TYPE=Release >/dev/null
    build "$src/mbelib/build" >/dev/null
    fetch dsd-fme https://github.com/lwvmobile/dsd-fme
    cmake -S "$src/dsd-fme" -B "$src/dsd-fme/build" -DCMAKE_INSTALL_PREFIX="$dsd" -DCMAKE_BUILD_TYPE=Release \
          -DCMAKE_PREFIX_PATH="$dsd" -DCMAKE_INSTALL_RPATH="$dsd/lib" -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON >/dev/null
    build "$src/dsd-fme/build" >/dev/null
    echo "STEP DSD-FME installed"
  fi
else
  echo "STEP DSD-FME already built"
fi
echo "STEP Builds done"
