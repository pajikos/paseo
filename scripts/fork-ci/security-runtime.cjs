// Use the read-only npm installation from the pinned scan image, never project modules.
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const fromNpm = createRequire(path.resolve(path.dirname(process.execPath), "../lib/node_modules/npm/package.json"));
const Arborist = fromNpm("@npmcli/arborist");
const { checkPlatform } = fromNpm("npm-install-checks");
const semver = fromNpm("semver");

function productionPaths(desktop) {
  const nodes = new Map();
  const pending = [desktop];
  while (pending.length) {
    const node = pending.pop();
    if (nodes.has(node.location)) continue;
    nodes.set(node.location, node);
    if (node.isLink) {
      pending.push(node.target);
      continue;
    }
    for (const edge of node.edgesOut.values()) {
      if (!["prod", "optional", "peer", "peerOptional"].includes(edge.type)) continue;
      if (!edge.to && edge.optional) continue;
      if (!edge.to) throw new Error("Missing required runtime dependency: " + edge.name);
      try {
        checkPlatform(edge.to.package, false, { os: "darwin", cpu: "arm64" });
      } catch (error) {
        if (error.code === "EBADPLATFORM" && edge.optional) continue;
        throw error;
      }
      pending.push(edge.to);
    }
  }
  return nodes;
}

function affectedVersions(findings, nodes) {
  const matches = [];
  for (const [name, finding] of Object.entries(findings)) {
    const versions = new Set(finding.nodes.filter((location) => nodes.has(location))
      .map((location) => nodes.get(location).version));
    for (const version of versions) {
      for (const advisory of finding.via) {
        // npm also lists parent packages that depend on a vulnerable package. Their aggregated
        // severity can come from dev edges. Attribute the original advisory to its affected component.
        if (typeof advisory === "string") continue;
        const options = { loose: true, includePrerelease: true };
        if (typeof advisory.range !== "string" || !semver.validRange(advisory.range, options)) {
          throw new Error("Invalid npm advisory range");
        }
        if (semver.satisfies(version, advisory.range, options)) {
          matches.push({ name, version, severity: advisory.severity, source: advisory.source,
            title: advisory.title, url: advisory.url, range: advisory.range });
        }
      }
    }
  }
  return matches;
}

async function main() {
  const root = await new Arborist({ path: process.cwd() }).loadVirtual();
  const desktop = root.inventory.get("packages/desktop");
  const runtime = productionPaths(desktop);
  if (!runtime.has("packages/server") || !runtime.has("packages/cli")) throw new Error("Missing CLI or daemon");
  // build-fetch.sh takes this exact devDependency's version and checksum for the shipped macOS binary.
  const electron = desktop.edgesOut.get("electron")?.to;
  if (electron?.location !== "node_modules/electron" || electron.version !== "44.2.0") {
    throw new Error("Electron lock entry does not match the packaged version");
  }
  const { vulnerabilities } = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const result = {
    platform: "darwin-arm64",
    runtimePaths: Array.from(runtime.keys()).sort(),
    runtimeAdvisories: affectedVersions(vulnerabilities, runtime),
    electron: { version: electron.version, lockPath: electron.location,
      advisories: affectedVersions(vulnerabilities, new Map([[electron.location, electron]])) },
  };
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
}

if (require.main === module) {
  main().catch((error) => { console.error(error); process.exitCode = 1; });
}
module.exports = { productionPaths, affectedVersions };
