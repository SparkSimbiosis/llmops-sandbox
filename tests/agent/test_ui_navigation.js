"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("sandbox/agent/ui/app.js", "utf8");
const slice = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
for (const [port, expected] of [["8180", "4300"], ["8280", "4300"], ["8080", "4200"]]) {
  const link = {};
  const start = app.indexOf('$("console-link").href =');
  const end = app.indexOf(";", start) + 1;
  vm.runInNewContext(app.slice(start, end), { $: () => link, location: { port, protocol: "http:", hostname: "localhost" } });
  assert.equal(link.href, "http://localhost:" + expected);
}
(async () => {
  const status = { hidden: true, textContent: "", replaceChildren(...children) { this.children = children; } };
  const calls = [], events = [];
  const fields = { "config-load-status": status, "tab-packs": {}, "tab-chat": {} };
  const context = { $: id => fields[id], document: { querySelectorAll: () => [], dispatchEvent: e => events.push(e) },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    loadConfig: () => calls.push("config"), loadChatTab: () => calls.push("chat") };
  vm.createContext(context);
  vm.runInContext(slice("function showTab(name)", '\ndocument.querySelectorAll(".tab[data-tab]").forEach'), context);
  context.showTab("packs"); assert.equal(status.hidden, false); assert.deepEqual(calls, ["config"]);
  context.showTab("chat"); assert.equal(status.hidden, true); assert.deepEqual(calls, ["config", "chat"]);
  context.showTab("unknown"); assert.equal(events.length, 2);
  context.cfg = null; context.configLoading = null; context.errorCard = message => ({ textContent: message });
  context.api = async () => ({ status: 503 });
  vm.runInContext(slice("async function loadConfig()", "\nfunction openConfigFile("), context);
  await context.loadConfig(); assert.match(status.children[0].textContent, /HTTP 503/);
  context.api = async () => { throw new Error("offline"); };
  await context.loadConfig(); assert.match(status.children[0].textContent, /сервісом/);
  console.log("PASS navigation: visible config errors, network failure, loader routing, unknown page guard");
})();
