const assert = require("node:assert/strict");
const { test } = require("node:test");
const { affectedVersions, productionPaths } = require("./security-runtime.cjs");

function node(location, version = "1.0.0", pkg = {}) {
  return { location, version, package: pkg, edgesOut: new Map() };
}

test("follows workspace targets and runtime edges, excluding tools and incompatible optionals", () => {
  const desktop = node("packages/desktop");
  const server = node("packages/server");
  const link = { ...node("node_modules/server"), isLink: true, target: server };
  desktop.edgesOut.set("server", { to: link, type: "prod" });
  desktop.edgesOut.set("dev-tool", { to: node("node_modules/dev-tool"), type: "dev" });
  server.edgesOut.set("peer", { to: node("node_modules/peer"), type: "peer" });
  server.edgesOut.set("helper-linux", { type: "optional", optional: true,
    to: node("node_modules/helper-linux", "1.0.0", { os: ["linux"] }) });
  assert.deepEqual([...productionPaths(desktop).keys()].sort(),
    ["node_modules/peer", "node_modules/server", "packages/desktop", "packages/server"]);
});

test("matches the original advisory against each affected in-scope path and version", () => {
  const runtime = node("packages/server/node_modules/dep", "1.0.0");
  const dependency = { nodes: ["node_modules/dep", runtime.location], via: [
    "parent-meta", { source: 1, severity: "low", range: "<2.0.0" },
    { source: 2, severity: "high", range: ">=2.0.0" }] };
  const tooling = { nodes: ["node_modules/dev-tool"], via: [{ source: 3, range: "*" }] };
  const matches = affectedVersions({ dep: dependency, tool: tooling }, new Map([[runtime.location, runtime]]));
  assert.deepEqual(matches.map(({ source, severity, version }) => [source, severity, version]),
    [[1, "low", "1.0.0"]]);
});
