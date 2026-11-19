#!/usr/bin/env bash
set -euo pipefail

[[ $# == 1 && ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]]
[[ $(uname -sm) == 'Darwin arm64' ]]
scripts=$(cd "$(dirname "$0")" && pwd -P)
artifacts=$(cd "$1" && pwd -P)
archive=Paseo-mac-arm64.tar.gz
digest=$(awk -v archive="$archive" '$2 == archive {print $1}' "$artifacts/SHA256SUMS")
[[ $digest =~ ^[0-9a-f]{64}$ ]]
(cd "$artifacts" && printf '%s  %s\n' "$digest" "$archive" | shasum -a 256 -c -)

scratch=$(mktemp -d /private/tmp/paseo-smoke.XXXXXXXX)
checker=$(mktemp -d /private/tmp/paseo-checker.XXXXXXXX)
trap 'rm -rf "$scratch" "$checker"' EXIT
mkdir -p "$scratch"/{home,daemon,profile,tmp,terminal}
tar -xzf "$artifacts/$archive" -C "$scratch"
app="$scratch/Paseo.app"
cli="$app/Contents/Resources/bin/paseo"
test -x "$cli"
pty_helper=$(find "$app/Contents/Resources" -type f -path '*/darwin-arm64/spawn-helper' -print -quit)
test -n "$pty_helper" && test -x "$pty_helper" || { echo "Missing executable arm64 spawn-helper"; exit 1; }

# Sign only the disposable copy. Electron needs V8 JIT entitlements on its helpers too.
if ! codesign --verify --deep --strict "$app" 2>/dev/null; then
  codesign --force --deep --sign - --timestamp=none \
    --entitlements "$scripts/../../packages/desktop/build/entitlements.mac.plist" "$app"
  codesign --verify --deep --strict --verbose=2 "$app"
  echo "Ad-hoc signed the smoke copy of archive $digest"
fi

# Only scratch is writable under the profile. The candidate cannot replace the checker.
cp "$(command -v node)" "$checker/node"
cp "$scripts/smoke.mjs" "$checker/check.mjs"
xcrun swiftc "$scripts/smoke-window.swift" -o "$checker/check-window"
cd "$scratch"
env -i HOME="$scratch/home" USERPROFILE="$scratch/home" CFFIXED_USER_HOME="$scratch/home" \
  PASEO_HOME="$scratch/daemon" PASEO_ELECTRON_USER_DATA_DIR="$scratch/profile" \
  TMPDIR="$scratch/tmp" USER="$(id -un)" LOGNAME="$(id -un)" SHELL=/bin/zsh \
  LANG=en_US.UTF-8 PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  /usr/bin/sandbox-exec -D "SCRATCH=$scratch" -D "RUNNER_HOME=$HOME" \
    -D "RUNNER_TMP=$RUNNER_TEMP" -f "$scripts/smoke.sb" \
    "$checker/node" "$checker/check.mjs" "$scratch"
