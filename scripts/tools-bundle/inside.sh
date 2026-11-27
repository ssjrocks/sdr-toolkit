#!/usr/bin/env bash
# Runs in the container (see build-tools-bundle.sh): installs and builds the decoders, then collects them.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Whatever happens, the files written to /out belong to the user who ran the build, not root.
trap 'chown -R "$HOST_UID:$HOST_GID" /out' EXIT
apt-get update -q
# The decoders Ubuntu packages, the libraries the source builds need, and the tools to collect them.
apt-get install -y -q --no-install-recommends \
  rtl-433 readsb direwolf wsjtx multimon-ng rtl-sdr libhackrf0 gnss-sdr gr-satellites gnuradio \
  wmbusmeters rtl-wmbus welle.io satdump odr-dabmod odr-dabmux odr-audioenc odr-padenc \
  git cmake build-essential pkg-config gnuradio-dev libvolk-dev pybind11-dev libspdlog-dev libgmp-dev \
  golang-go ca-certificates libliquid-dev libconfig++-dev libcjson-dev libxml2-dev libjansson-dev zlib1g-dev \
  libglib2.0-dev libsqlite3-dev libfftw3-dev patchelf python3 python3-numpy xz-utils file ffmpeg >/dev/null
echo "== packages installed"
# The built parts stay in /out/cache between runs: a rebuild only builds what changed.
export SDR_TOOLKIT_ROOT=/out/cache/parts SDR_TOOLKIT_SRC=/out/cache/src
mkdir -p /out/cache/parts
bash /setup/build.sh
echo "== builds done"
python3 /work/collect.py /out/cache/parts /out/stage
cd /out/stage && tar -cf - . | xz -T0 -6 > /out/sdr-toolkit-tools-linux-x86_64.tar.xz
echo "== bundle written"
