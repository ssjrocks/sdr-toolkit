#!/usr/bin/env bash
# Publishes the tools bundle (built by build-tools-bundle.sh) as a GitHub pre-release of its own, tagged
# tools-<its build time>, and writes src/sdr_toolkit/tools_bundle.json, which tells the app (and install.sh)
# which bundle to fetch. A pre-release: the app's updater follows the latest full release, never this one.
#
# Usage: scripts/release-tools.sh
set -euo pipefail
cd "$(dirname "$0")/.."
tarball=build/tools-bundle/sdr-toolkit-tools-linux-x86_64.tar.xz
version=$(tar -xJOf "$tarball" ./VERSION | tr -d '\n')
tag="tools-$(echo "$version" | tr -d ':-' | tr 'T' '-' | cut -c1-13)"
sha=$(sha256sum "$tarball" | cut -d' ' -f1)
size=$(stat -c %s "$tarball")
repo=ssjrocks/sdr-toolkit
if ! gh release view "$tag" -R "$repo" >/dev/null 2>&1; then
  gh release create "$tag" "$tarball" -R "$repo" --prerelease --title "Tools bundle $version" \
    --notes "The outside decoders SDR Toolkit runs on Linux (x86-64, Ubuntu $(. /etc/os-release; echo "$VERSION_ID")), with their libraries, built in a container by scripts/build-tools-bundle.sh. The app and install.sh fetch it by themselves. sha256 $sha"
fi
url="https://github.com/$repo/releases/download/$tag/$(basename "$tarball")"
python3 - "$version" "$url" "$sha" "$size" <<'PY'
import json, sys
v, url, sha, size = sys.argv[1:]
json.dump({"version": v, "url": url, "sha256": sha, "size": int(size)},
          open("src/sdr_toolkit/tools_bundle.json", "w"), indent=2)
PY
echo "published $tag: $url ($size bytes)"
