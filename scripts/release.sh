#!/usr/bin/env bash
# Makes a draft GitHub release with the wheel and sdr-toolkit.json (version,
# wheel name, SHA-256) for the in-app updater and scripts/install.sh. Pushing the
# tag starts .github/workflows/windows.yml, which adds the Windows installer and
# sdr-toolkit-windows.json and then publishes the release, so no updater sees a
# release without its files. (The workflow can also make the release by itself
# from just the tag: then release-notes/vVERSION.md is its notes.)
#
# Usage: scripts/release.sh NOTES.md
# Bump version in pyproject.toml and commit first.
set -euo pipefail
cd "$(dirname "$0")/.."
notes="${1:?usage: scripts/release.sh NOTES.md}"
[[ -f "$notes" ]] || { echo "no notes file: $notes" >&2; exit 2; }
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
if git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  echo "tag v$version already exists" >&2; exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "commit your changes first" >&2; exit 1
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
uv build -q --wheel --out-dir "$tmp"
wheel=$(cd "$tmp" && ls sdr_toolkit-"$version"-*.whl)
sha=$(sha256sum "$tmp/$wheel" | cut -d' ' -f1)
printf '{"version": "%s", "wheel": "%s", "sha256": "%s"}\n' "$version" "$wheel" "$sha" > "$tmp/sdr-toolkit.json"
git tag -a "v$version" -m "SDR Toolkit $version"
git push origin HEAD "v$version"
gh release create "v$version" "$tmp/$wheel" "$tmp/sdr-toolkit.json" \
  --title "SDR Toolkit $version" --notes-file "$notes" --verify-tag --draft
echo "drafted SDR Toolkit $version ($wheel, sha256 $sha); the Windows build publishes it"
