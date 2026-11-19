#!/usr/bin/env bash
# Executed inside a fresh Linux ARM64 container with --network none.
set -euo pipefail

cd /home/node/src
export PATH="/home/node/src/node_modules/.bin:$PATH"
export npm_config_offline=true EXPO_OFFLINE=1 NODE_OPTIONS=--max-old-space-size=4608
node scripts/postinstall-patches.mjs
# electron-log resolves Electron's path during import. These tests use a mocked runtime.
ELECTRON_OVERRIDE_DIST_PATH=/tmp vitest run --maxWorkers=1 --reporter=dot \
  packages/desktop/src/features/{auto-updater,app-update-service,app-update-rollout}.test.ts
npm run build:app-deps:clean
(cd packages/app && PASEO_WEB_PLATFORM=electron expo export --platform web --max-workers 3)
npm run build:server:clean
npm run build:main --workspace=@getpaseo/desktop
node packages/cli/dist/index.js --version

# Keep native build tools in the Linux tree and package the Darwin production dependencies.
tar cf - packages/{app,cli,client,desktop,highlight,plugin,protocol,relay,server}/dist \
  | tar xf - -C /home/node/target
cd /home/node/target
node scripts/postinstall-patches.mjs
cd packages/desktop
export NODE_PATH=/home/node/src/node_modules CSC_IDENTITY_AUTO_DISCOVERY=false
node <<'NODE'
const fs = require("node:fs");
const yaml = require("yaml");
const config = yaml.parse(fs.readFileSync("electron-builder.yml", "utf8"));
config.publish = null;
config.mac.identity = null;
config.mac.notarize = false;
config.electronVersion = "44.11.5";
config.electronDist = "/home/node/electron";
fs.writeFileSync("trial-builder.json", JSON.stringify(config));
NODE
electron-builder --config trial-builder.json --mac dir --arm64 --publish never
cd release/mac-arm64
node /home/node/src/scripts/fork-ci/build-check.cjs
python3 /home/node/src/scripts/fork-ci/build-check.py
tar czf /home/node/Paseo-mac-arm64.tar.gz Paseo.app
