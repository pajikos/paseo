const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const asar = require("@electron/asar");

const resources = path.resolve("Paseo.app/Contents/Resources");
const archive = path.join(resources, "app.asar");
const entry = (name) => asar.extractFile(archive, name);
for (const name of [
  "package.json",
  "node_modules/@getpaseo/cli/package.json",
  "node_modules/@getpaseo/server/package.json",
]) {
  assert.equal(JSON.parse(entry(name)).version, "0.16.2", name);
}
assert.match(entry("dist/features/auto-updater.js").toString(), /isPackaged:\s*\(\)\s*=>\s*false/);
assert.ok(!fs.existsSync(path.join(resources, "app-update.yml")));
fs.accessSync(path.join(resources, "app-dist/index.html"));
assert.ok(fs.statSync(path.join(resources, "builtin-plugins")).isDirectory());
fs.accessSync(path.join(resources, "bin/paseo"), fs.constants.X_OK);

const files = asar.listPackage(archive);
for (const [name, pattern] of [
  ["PTY", /\/node-pty\/prebuilds\/darwin-arm64\/pty\.node$/],
  ["PTY helper", /\/node-pty\/prebuilds\/darwin-arm64\/spawn-helper$/],
  ["esbuild", /\/@esbuild\/darwin-arm64\/bin\/esbuild$/],
  ["speech", /\/sherpa-onnx-darwin-arm64\/.*\.(node|dylib)$/],
]) {
  const file = files.find((item) => pattern.test(item));
  assert.ok(file, name + " Darwin ARM64 payload missing");
  const relative = file.slice(1);
  const bytes = entry(relative);
  assert.equal(bytes.readUInt32LE(0), 0xfeedfacf, name + " Mach-O");
  assert.equal(bytes.readUInt32LE(4), 0x0100000c, name + " ARM64");
  if (name === "PTY helper") {
    fs.accessSync(path.join(archive + ".unpacked", relative), fs.constants.X_OK);
  }
  console.log("Verified", name, file);
}
