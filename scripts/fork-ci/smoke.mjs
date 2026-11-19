// Uses the runner's Node and built-in CDP transport, without installing packages.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { randomInt, randomUUID } from "node:crypto";
import dgram from "node:dgram";
import { once } from "node:events";
import { existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const root = process.argv[2];
const daemonHome = process.env.PASEO_HOME;
const contents = path.join(root, "Paseo.app/Contents");
const shim = path.join(contents, "Resources/bin/paseo");
const appLog = path.join(root, "desktop.log");

async function preflight() {
  const server = net.createServer((socket) => socket.end("loopback works"));
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  try {
    const local = net.connect(server.address().port, "127.0.0.1");
    local.setEncoding("utf8");
    let reply = "";
    for await (const data of local) reply += data;
    assert.equal(reply, "loopback works");
  } finally {
    server.close();
  }

  // TEST-NET-1 is for documentation. Only an explicit policy denial passes.
  const tcp = net.connect(443, "192.0.2.1");
  tcp.setTimeout(3000, () => tcp.destroy(new Error("external TCP timed out")));
  try {
    await assert.rejects(once(tcp, "connect"), { code: "EPERM" });
  } finally {
    tcp.destroy();
  }
  const udp = dgram.createSocket("udp4");
  try {
    // Bind explicitly to loopback so an implicit wildcard bind cannot pass this check.
    udp.bind(0, "127.0.0.1");
    await once(udp, "listening");
    await assert.rejects(new Promise((resolve, reject) => {
      udp.send("probe", 443, "192.0.2.1", (error) => error ? reject(error) : resolve());
    }), { code: "EPERM" });
  } finally {
    udp.close();
  }
  console.log("Loopback TCP exchange succeeded. External TCP and UDP returned EPERM.");
}

async function availablePort() {
  const server = net.createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

// Query the renderer via Chrome DevTools Protocol, matching the packaged app URL
// and desktop preload bridge used in packages/desktop/e2e/packaged-app-smoke.js.
async function evaluateRenderer(port, evaluate, args, timeout = 5000) {
  const response = await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(2000) });
  const target = (await response.json()).find((page) => page.type === "page" && page.url.startsWith("paseo://app/"));
  assert.ok(target, "waiting for paseo://app/ renderer");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  try {
    const signal = AbortSignal.timeout(timeout);
    await once(ws, "open", { signal });
    const expression = `(${evaluate})(${JSON.stringify(args)})`;
    ws.send(JSON.stringify({ id: 1, method: "Runtime.evaluate", params: { expression, awaitPromise: true, returnByValue: true } }));
    for (;;) {
      const [event] = await once(ws, "message", { signal });
      const message = JSON.parse(event.data);
      if (message.id !== 1) continue;
      assert.ok(!message.error && !message.result.exceptionDetails,
        message.error?.message ?? message.result?.exceptionDetails?.exception?.description ?? "renderer evaluation failed");
      return message.result.result.value;
    }
  } finally {
    ws.close();
  }
}

async function rendererStatus() {
  const root = document.querySelector("#root");
  const text = document.body?.innerText || "";
  const bridge = window.paseoDesktop;
  const ready = root?.childElementCount > 0 && /Welcome to Paseo|Skip setup/i.test(text)
    && ["invoke", "events", "window", "dialog", "browser"].every(key => key in (bridge || {}));
  return { ready, text: text.slice(0, 250),
    daemon: ready ? await bridge.invoke("desktop_daemon_status") : null };
}

function cli(...args) {
  return execFileSync(shim, args, { encoding: "utf8", timeout: 20000, stdio: ["ignore", "pipe", "inherit"] });
}

async function openChat({ workspaceId, serverId, agentId, projectName, workspaceName, question, reply }) {
  const deadline = Date.now() + 60000;
  async function visible(selector, text) {
    for (;;) {
      const element = [...document.querySelectorAll(selector)].find(node =>
        node.checkVisibility({ visibilityProperty: true, opacityProperty: true }) &&
        node.getBoundingClientRect().height > 0 &&
        (!text || node.innerText.includes(text)));
      if (element) return element;
      if (Date.now() >= deadline) {
        throw new Error(`Waiting for ${selector} ${text ?? ""} at ${location.pathname}: ${document.body.innerText.slice(0, 650)}`);
      }
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  }
  const testId = id => `[data-testid="${CSS.escape(id)}"]`;
  (await visible('[data-testid="onboarding-modal"] :is(button,[role="button"])', "Skip setup")).click();
  (await visible(testId(`sidebar-workspace-row-${serverId}:${workspaceId}`))).click();
  await visible(testId("workspace-header-title"), workspaceName);
  await visible(testId("workspace-header-subtitle"), projectName);
  const tab = testId(`workspace-tab-agent_${agentId}`);
  (await visible(tab)).click();
  await visible(tab + '[aria-selected="true"]');
  const chat = testId("agent-chat-scroll") + " ";
  await visible(chat + testId("user-message"), question);
  await visible(chat + testId("assistant-message"), reply);
}

async function checkProjectChat(cdpPort, serverId) {
  const cwd = path.join(root, "paseo-smoke-project");
  mkdirSync(cwd);
  const workspace = JSON.parse(cli("workspace", "create", "--isolation", "local", "--path", cwd,
    "--title", "Mac smoke workspace", "--json"));
  assert.equal(workspace.cwd, cwd);

  // Exercise the production transcript importer with a disposable local history.
  const sessionId = randomUUID();
  const question = "Which project is open?";
  const reply = `The smoke project is open (${sessionId}).`;
  const title = "Imported smoke conversation";
  const projectDir = path.join(process.env.HOME, ".claude", "projects", cwd.replace(/[^a-zA-Z0-9]/g, "-"));
  mkdirSync(projectDir, { recursive: true });
  const history = [
    { type: "user", uuid: randomUUID(), message: { role: "user", content: question } },
    { type: "assistant", uuid: randomUUID(), message: { role: "assistant", content: reply } },
    { type: "custom-title", customTitle: title },
  ];
  writeFileSync(path.join(projectDir, sessionId + ".jsonl"),
    history.map(entry => JSON.stringify({ ...entry, sessionId, cwd })).join("\n") + "\n");
  const agent = JSON.parse(cli("agent", "import", sessionId, "--provider", "claude", "--cwd", cwd, "--json"));
  assert.equal(agent.title, title);
  await evaluateRenderer(cdpPort, openChat, {
    workspaceId: workspace.workspaceId, serverId, agentId: agent.agentId,
    projectName: workspace.project, workspaceName: workspace.name, question, reply,
  }, 70000);
  console.log(`Opened project "${workspace.project}" and workspace "${workspace.name}". Selected "${title}" and read both fixture messages.`);
}

async function checkTerminal() {
  const { id } = JSON.parse(cli("terminal", "create", "--cwd", path.join(root, "terminal"), "--name", "fork-smoke", "--json"));
  assert.ok(typeof id === "string" && id.length > 0, "terminal create returned no id");
  try {
    const addend = randomInt(10000, 90000);
    const expected = `paseo-pty-${addend + 731}`;
    // The expected output does not occur in the input, even when the PTY echoes it.
    cli("terminal", "send-keys", id, `printf 'paseo-pty-%d\\n' $((${addend} + 731))`, "Enter", "--json");
    for (let attempt = 0; attempt < 20; attempt++) {
      const { lines } = JSON.parse(cli("terminal", "capture", id, "--scrollback", "--json"));
      if (lines.some((line) => line.trim() === expected)) {
        console.log(`Bundled CLI and PTY returned the computed line "${expected}".`);
        return;
      }
      await delay(500);
    }
    throw new Error("Terminal never printed the computed result");
  } finally {
    cli("terminal", "kill", id, "--json");
  }
}

await preflight();
const daemonPort = await availablePort();
let cdpPort = await availablePort();
while (cdpPort === daemonPort) cdpPort = await availablePort();
const listen = `127.0.0.1:${daemonPort}`;
process.env.PASEO_LISTEN = listen;
process.env.PASEO_ELECTRON_FLAGS = `--remote-debugging-address=127.0.0.1 --remote-debugging-port=${cdpPort}`;
writeFileSync(path.join(daemonHome, "config.json"), JSON.stringify({
  version: 1, daemon: { listen, relay: { enabled: false }, mcp: { enabled: false, injectIntoAgents: false } },
  // History import reads JSONL locally. Prevent any model invocation or provider login.
  agents: { providers: { claude: { command: ["/usr/bin/false"] } } },
}));
const logFd = openSync(appLog, "a");
const child = spawn(path.join(contents, "MacOS/Paseo"), [], { detached: true, stdio: ["ignore", logFd, logFd] });
try {
  let last;
  const deadline = Date.now() + 100000;
  for (;;) {
    assert.ok(child.exitCode === null && child.signalCode === null,
      `Desktop exited (code ${child.exitCode}, signal ${child.signalCode})`);
    try {
      last = await evaluateRenderer(cdpPort, rendererStatus);
      const daemon = last.daemon;
      if (last.ready && daemon?.status === "running" && daemon.desktopManaged === true &&
          Number.isInteger(daemon.pid) && daemon.serverId && daemon.listen === listen && daemon.home === daemonHome) {
        const window = execFileSync(path.join(path.dirname(process.execPath), "check-window"), [String(child.pid)], { encoding: "utf8", timeout: 5000 });
        console.log(window.trim());
        break;
      }
    } catch (error) {
      last = String(error);
    }
    if (Date.now() >= deadline) throw new Error(`Timed out waiting for renderer and desktop-managed daemon: ${JSON.stringify(last)}`);
    await delay(500);
  }
  console.log(`Paseo rendered its setup screen and started its daemon on ${listen}.`);
  const response = await fetch(`http://${listen}/api/health`, { signal: AbortSignal.timeout(3000) });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).status, "ok");
  await checkTerminal();
  await checkProjectChat(cdpPort, last.daemon.serverId);
} catch (error) {
  for (const log of [appLog, path.join(process.env.PASEO_ELECTRON_USER_DATA_DIR, "logs/main.log"), path.join(daemonHome, "daemon.log")]) {
    if (existsSync(log)) console.error(`${path.basename(log)} tail:\n${readFileSync(log, "utf8").slice(-6000)}`);
  }
  throw error;
} finally {
  try {
    if (existsSync(path.join(daemonHome, "paseo.pid"))) cli("daemon", "stop", "--force");
  } finally {
    if (child.pid && child.exitCode === null && child.signalCode === null) {
      try {
        process.kill(-child.pid, "SIGTERM");
        try {
          await once(child, "exit", { signal: AbortSignal.timeout(5000) });
        } catch (error) {
          if (error.name !== "AbortError") throw error;
          process.kill(-child.pid, "SIGKILL");
        }
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
    }
  }
}
