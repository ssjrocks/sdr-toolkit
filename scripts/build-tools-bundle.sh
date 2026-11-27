#!/usr/bin/env bash
# Builds SDR Toolkit's tools bundle: every outside decoder the app runs, with the libraries it needs, in one
# folder that works from anywhere (no system packages). Built inside an Ubuntu container of the same release
# as the target, so nothing is installed on this PC. Output: build/tools-bundle/sdr-toolkit-tools-linux-x86_64.tar.xz
#
# Usage: scripts/build-tools-bundle.sh [UBUNTU_TAG]      (default: this PC's release, e.g. 26.04)
set -euo pipefail
cd "$(dirname "$0")/.."
tag="${1:-$(. /etc/os-release; echo "$VERSION_ID")}"
out="$PWD/build/tools-bundle"
mkdir -p "$out" "$out/cache"
docker run --rm --network host --memory=16g --memory-swap=16g --cpus=12 \
  -v "$PWD/scripts/tools-bundle:/work:ro" -v "$PWD/src/sdr_toolkit/setup:/setup:ro" -v "$out:/out" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  "ubuntu:$tag" bash /work/inside.sh
ls -la "$out"/*.tar.xz
