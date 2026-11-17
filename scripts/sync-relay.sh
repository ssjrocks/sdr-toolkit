#!/usr/bin/env bash
# Copies SDR Tools Relay's device server (its HackRF and RTL-SDR backends, the DSP and the protocol) into
# src/sdr_toolkit/hackrf_local/, unchanged: the toolkit runs it on 127.0.0.1 for a HackRF plugged into this
# PC. Change them in the relay (~/dev/sdr-tools-relay), then run this again.
#
# Usage: scripts/sync-relay.sh [RELAY_CHECKOUT]
set -euo pipefail
cd "$(dirname "$0")/.."
src="${1:-$HOME/dev/sdr-tools-relay}/src/sdr_relay"
dst=src/sdr_toolkit/hackrf_local
mkdir -p "$dst"
for f in protocol.py server.py hackrf.py dsp.py rtlsdr.py; do
  cp "$src/$f" "$dst/$f"
done
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$src/__init__.py")
commit=$(git -C "$src" rev-parse --short HEAD)
cat > "$dst/__init__.py" <<PY
"""SDR Tools Relay's device server, copied unchanged by scripts/sync-relay.sh (relay $version, $commit): the
toolkit runs it on 127.0.0.1 to serve a HackRF plugged into this PC (see local_hackrf.py)."""

RELAY_VERSION = "$version"
PY
echo "copied relay $version ($commit) into $dst"
