#!/usr/bin/env bash
# Executed inside the network-enabled, unprivileged fetch container.
set -euo pipefail

mkdir src target electron
for tree in src target; do tar xf /manifests.tar -C "$tree"; done

cd src
npm ci --ignore-scripts --no-audit --no-fund
version=$(node -p 'require("./node_modules/electron/package.json").version')
[[ $version == 44.11.5 ]]
archive="electron-v$version-darwin-arm64.zip"
checksum=$(node -p 'require("./node_modules/electron/checksums.json")[process.argv[1]]' "$archive")
[[ $checksum =~ ^[0-9a-f]{64}$ ]]
cd ../electron
curl --fail --location --proto '=https' --proto-redir '=https' --retry 4 \
  --output "$archive" "https://github.com/electron/electron/releases/download/v$version/$archive"
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check

cd ../target
npm ci --workspace=@getpaseo/desktop --omit=dev --os=darwin --cpu=arm64 \
  --ignore-scripts --no-audit --no-fund
# Discard the fetch cache before Metro and electron-builder allocate their working files.
npm cache clean --force
