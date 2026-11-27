#!/usr/bin/env bash
# Builds the decoders Ubuntu doesn't package, into ~/.local/share/sdr-toolkit, as the user:
#   gr-lora_sdr (EPFL's LoRa modem for GNU Radio, github.com/tapparelj/gr-lora_sdr)
#   mbelib (AMBE/IMBE voice codecs, github.com/lwvmobile/mbelib) and DSD-FME (github.com/lwvmobile/dsd-fme)
#   rs1729's radiosonde decoders and dft_detect (github.com/rs1729/RS)
#   rtlamr, utility meters' ERT / SCM / IDM / R900 messages (github.com/bemasher/rtlamr)
#   libacars, dumpvdl2 and dumphfdl (github.com/szpajder), acarsdec (github.com/f00b4r0/acarsdec): aircraft messages
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
rs="$root/radiosonde"
sondes="rs41mod rs92mod dfm09mod m10m20mod lms6Xmod imet54mod meisei100mod mp3h1mod mts01mod"
if (( rebuild )) || [[ ! -x "$rs/bin/rs41mod" || ! -x "$rs/bin/dft_detect" ]]; then
  echo "STEP Building the radiosonde decoders, a minute"
  fetch RS https://github.com/rs1729/RS.git
  # shellcheck disable=SC2086
  nice -n 10 make -C "$src/RS/demod/mod" -j"$jobs" $sondes >/dev/null
  nice -n 10 gcc -O2 "$src/RS/scan/dft_detect.c" -lm -o "$src/RS/scan/dft_detect"
  mkdir -p "$rs/bin"
  for p in $sondes; do install -m 755 "$src/RS/demod/mod/$p" "$rs/bin/"; done
  install -m 755 "$src/RS/scan/dft_detect" "$rs/bin/"
  echo "STEP Radiosonde decoders installed"
else
  echo "STEP Radiosonde decoders already built"
fi
amr="$root/rtlamr"
if (( rebuild )) || [[ ! -x "$amr/bin/rtlamr" ]]; then
  echo "STEP Building rtlamr (utility meters), a minute"
  mkdir -p "$amr/bin"
  GOBIN="$amr/bin" GOPATH="$src/go" GOFLAGS=-modcacherw nice -n 10 go install github.com/bemasher/rtlamr@latest
  echo "STEP rtlamr installed"
else
  echo "STEP rtlamr already built"
fi
aero="$root/aero"
if (( rebuild )) || [[ ! -x "$aero/bin/dumpvdl2" || ! -x "$aero/bin/dumphfdl" || ! -x "$aero/bin/acarsdec" ]]; then
  echo "STEP Building the aircraft message decoders (ACARS, VDL Mode 2, HFDL), a few minutes"
  # Installed under $aero, finding libacars there by their built-in library path (no system folders).
  aero_cmake=(-DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$aero" -DCMAKE_PREFIX_PATH="$aero"
              -DCMAKE_INSTALL_RPATH="$aero/lib" -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON)
  export PKG_CONFIG_PATH="$aero/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  fetch libacars https://github.com/szpajder/libacars.git
  cmake -S "$src/libacars" -B "$src/libacars/build" "${aero_cmake[@]}" >/dev/null
  build "$src/libacars/build" >/dev/null
  fetch dumpvdl2 https://github.com/szpajder/dumpvdl2.git
  cmake -S "$src/dumpvdl2" -B "$src/dumpvdl2/build" "${aero_cmake[@]}" -DRTLSDR=FALSE -DMIRISDR=FALSE \
        -DSDRPLAY=FALSE -DSDRPLAY3=FALSE -DSOAPYSDR=FALSE -DZMQ=FALSE -DPROTOBUF=FALSE >/dev/null
  build "$src/dumpvdl2/build" >/dev/null
  fetch dumphfdl https://github.com/szpajder/dumphfdl.git
  cmake -S "$src/dumphfdl" -B "$src/dumphfdl/build" "${aero_cmake[@]}" -DSOAPYSDR=FALSE -DZMQ=FALSE \
        -DRDKAFKA=FALSE >/dev/null
  build "$src/dumphfdl/build" >/dev/null
  fetch acarsdec https://github.com/f00b4r0/acarsdec.git
  cmake -S "$src/acarsdec" -B "$src/acarsdec/build" "${aero_cmake[@]}" -DMQTT=OFF -DALSA=OFF -DRTLSDR=OFF \
        -DAIRSPY=OFF -DSDRPLAY=OFF -DSOAPYSDR=OFF >/dev/null
  build "$src/acarsdec/build" >/dev/null
  echo "STEP Aircraft message decoders installed"
else
  echo "STEP Aircraft message decoders already built"
fi
echo "STEP Builds done"
