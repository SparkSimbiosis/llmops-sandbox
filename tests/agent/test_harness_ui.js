// Offline behavior checks for the prepare/confirm/run UI. No browser, network or model calls.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
class Element {
  constructor(tag, props = {}, ...children) {
    this.tag = tag; this.children = []; this.handlers = {}; this.disabled = false; this.checked = false;
    for (const [key, value] of Object.entries(props)) {
      if (key === "on") this.handlers = { ...value };
      else this[key] = value;
    }
    this.append(...children);
  }
  get options() { return this.children; }
  get value() { return this._value ?? (this.tag === "select" ? this.children[0]?.value ?? "" : ""); }
  set value(value) { this._value = value; }
  append(...children) { this.children.push(...children.filter((x) => x != null)); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  async fire(name) { if (this.handlers[name]) await this.handlers[name](); }
  set innerHTML(_) { throw new Error("Unsafe HTML insertion"); }
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
async function fixture({ realAvailable = true } = {}) {
  const root = new Element("div", { id: "harness-root" });
  const calls = [];
  const handlers = {};
  let selectedProvider = "mock";
  const document = { addEventListener: (name, handler) => { handlers[name] = handler; }, dispatchEvent: event => { handlers[event.type]?.(event); } };
  const response = { version: 1, fingerprint: "abc123", profile: { name: "quick" }, permissions: { actual_repository_apply: false } };
  let prepareReply = async () => ({ status: 200, data: { preparation_id: "frozen-1", expires_in_seconds: 1800,
    passport: response, stages: [{ role: "researcher", kind: "model" }], packet: { goal: "<script>untrusted</script>" } } });
  let runReply = async () => ({ status: 200, data: { status: "completed", passport: response, stages: [], result: { cost_usd: 0 } } });
  const context = {
    $, document, CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } }, taskMode: () => selectedProvider, setProvider: (value) => {
      selectedProvider = value === "real" && realAvailable ? "real" : "mock";
      handlers["agent:providerchange"]?.({ detail: { mode: selectedProvider } });
    }, el: (tag, props, ...children) => new Element(tag, props, ...children),
    kpi: (key, value) => new Element("div", { text: key + ": " + value }), short: (value) => value?.slice(0, 10),
    showTab: () => {}, refreshToday: () => {}, priced: (cost, mode) => mode + ": " + cost, URLSearchParams, Date, setTimeout: () => 1, clearTimeout: () => {},
    api: async (method, url, body) => {
      calls.push({ method, url, body });
      if (url === "/harness/projects") return { status: 200, data: { projects: [{ id: "notes", title: "Notes", kind: "research", available: true }] } };
      if (url === "/models") return { status: 200, data: { models: [{ side: "real", available: realAvailable }] } };
      if (method === "GET") return { status: 200, data: response };
      if (url === "/harness/prepare") return prepareReply(body);
      if (url === "/harness/run") return runReply(body);
      throw new Error("Unexpected request");
    },
  };
  function find(element, id) {
    if (element?.id === id) return element;
    for (const child of element?.children || []) { const result = typeof child === "object" && find(child, id); if (result) return result; }
  }
  function $(id) { return id === "harness-root" ? root : find(root, id); }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../sandbox/agent/ui/harness.js"), "utf8"), context);
  await flush();
  return { $, calls, emit: (type, detail) => handlers[type]?.({ detail }), emitMode: (mode) => { selectedProvider = mode; handlers["agent:providerchange"]?.({detail:{mode}}); }, setPrepare: (fn) => { prepareReply = fn; }, setRun: (fn) => { runReply = fn; } };
}
(async () => {
  const f = await fixture();
  assert(f.calls.every((call) => call.method === "GET"), "initialization must not invoke a model or prepare");
  assert.equal(f.$("harness-mode").value, "mock");
  assert.equal(f.$("harness-run").disabled, true);
  f.$("harness-goal").value = "Inspect notes";
  await f.$("harness-goal").fire("input");
  await f.$("harness-prepare").fire("click");
  assert.equal(f.$("harness-run").disabled, false);
  f.$("harness-profile").value = "thorough";
  await f.$("harness-profile").fire("change");
  assert.equal(f.$("harness-run").disabled, true, "settings invalidate frozen preparation");
  f.$("harness-risk").value = "critical";
  await f.$("harness-risk").fire("change");
  await f.$("harness-prepare").fire("click");
  assert.equal(f.$("harness-run").disabled, true, "critical action needs fresh explicit confirmation");
  f.$("harness-confirm").checked = true;
  await f.$("harness-confirm").fire("change");
  assert.equal(f.$("harness-run").disabled, false);
  f.setRun(async () => { throw new Error("uncertain network outcome"); });
  await f.$("harness-run").fire("click");
  assert.equal(f.$("harness-run").disabled, true, "uncertain operation cannot be blindly retried");
  assert.equal(f.calls.filter((call) => call.url === "/harness/run").length, 1);
  assert.equal(f.calls.find((call) => call.url === "/harness/run").body.confirmed, true);
  let resolvePrepare;
  f.setPrepare(() => new Promise((resolve) => { resolvePrepare = resolve; }));
  const pending = f.$("harness-prepare").fire("click");
  f.$("harness-goal").value = "Changed while preparing";
  await f.$("harness-goal").fire("input");
  resolvePrepare({ status: 200, data: { preparation_id: "stale", expires_in_seconds: 1800 } });
  await pending;
  assert.equal(f.$("harness-run").disabled, true, "stale async preparation must be discarded");
  const g = await fixture();
  g.$("harness-goal").value = "Inspect frozen packet";
  await g.$("harness-goal").fire("input");
  await g.$("harness-prepare").fire("click");
  g.setRun(async () => ({ status: 200, data: { status: "answered", result: { cost_usd: 0, answer: "Visible answer <script>data only</script>" } } }));
  await g.$("harness-run").fire("click");
  assert(g.$("harness-result").children.some(child => child.tag === "div" && child.text === "Visible answer <script>data only</script>"), "answer is visible outside technical details as plain text");
  assert(g.$("harness-result").children.length > 0, "successful result is rendered");
  assert.equal(g.calls.find((call) => call.url === "/harness/run").body.confirmed, false);
  assert.equal(g.calls.find((call) => call.url === "/harness/prepare").body.mode, "mock");
  const disabledReal = await fixture({ realAvailable: false });
  assert.equal(disabledReal.$("harness-mode").options[1].disabled, true);
  disabledReal.$("harness-mode").value = "real";
  disabledReal.$("harness-goal").value = "Explicit real request";
  await disabledReal.$("harness-mode").fire("change");
  assert.equal(disabledReal.$("harness-mode").value, "mock", "unavailable real mode falls back to shared mock mode");
  await g.$("harness-prepare").fire("click");
  g.emitMode("real");
  assert.equal(g.$("harness-mode").value, "real", "header provider event updates the passport mode selector immediately");
  assert.equal(g.$("harness-run").disabled, true, "header mode changes invalidate prepared packet");
  assert.equal(g.$("harness-cache").checked, true);
  g.$("harness-cache").checked = false;
  await g.$("harness-cache").fire("change");
  await g.$("harness-prepare").fire("click");
  assert.equal(g.calls.filter((call) => call.url === "/harness/prepare").at(-1).body.cache, false);
  const texts = (node) => [node.text || "", ...(node.children || []).filter((child) => typeof child === "object").map(texts)].join(" ");
  const h = await fixture();
  assert.match(texts(h.$("harness-route")), /routing\.yaml/, "model choice rules are visible before preparation");
  assert.match(texts(h.$("harness-context")), /білого списку/, "context selection rules are visible before preparation");
  h.setPrepare(async () => ({ status: 200, data: { preparation_id: "frozen-2", expires_in_seconds: 1800, passport: { limits: { context_budget: 1000, max_tokens: 600 } }, stages: [],
    packet: { agent: "explorer", route: { chosen_model_class: "fast", resolved_model: "fake-mini", reason: "службовий крок" } },
    context: { terms: ["circ", "brea"], files_total: 41, chunks_total: 300, candidates: 12, used_tokens: 950, budget_tokens: 1000, dropped_total: 1,
      items: [{ path: "INCIDENT_RUNBOOK.md", start: 66, end: 73, heading: "Circuit breaker", score: 9.5, hits: ["circ"], pinned: false }],
      dropped: [{ path: "README.md", start: 1, end: 40, heading: null, score: 1, hits: ["brea"], pinned: false }] } } }));
  h.$("harness-goal").value = "Як працює circuit breaker?";
  await h.$("harness-goal").fire("input");
  await h.$("harness-prepare").fire("click");
  assert.match(texts(h.$("harness-route")), /клас fast → fake-mini\. Правило: службовий крок/);
  assert.match(texts(h.$("harness-context")), /41 файлів у білому списку → 300 фрагментів → 12 зі збігами → 1 у пакеті \(950 з 1000 токенів\)/);
  assert.match(texts(h.$("harness-context")), /INCIDENT_RUNBOOK\.md:66-73 · Circuit breaker/);
  assert.match(h.$("harness-profile-hint").textContent, /Контекст до 1000 токенів, відповідь до 600/);
  assert(!("complexity" in h.calls.find((call) => call.url === "/harness/prepare").body), "no-op complexity axis is not sent");
  await h.$("harness-goal").fire("input");
  assert.match(texts(h.$("harness-context")), /Після «Підготувати пакет»/, "invalidation returns to the rules");
  // A change for a git project: the scenario sets the kind, the result shows gates, the canonical diff and the approval.
  const c = await fixture();
  c.$("harness-goal").value = "Додай примітку в runbook";
  await c.$("harness-goal").fire("input");
  await c.$("harness-prepare").fire("click");
  assert.equal(c.calls.filter((call) => call.url === "/harness/prepare").at(-1).body.kind, "read", "questions are the default kind");
  c.emit("agent:harnesskind", { kind: "change" });
  assert.equal(c.$("harness-run").disabled, true, "kind change invalidates the prepared packet");
  assert.match(texts(c.$("harness-context")), /\.patch/, "change rules explain the .patch flow before preparation");
  assert.match(texts(c.$("harness-route")), /Planner тут немає/);
  await c.$("harness-prepare").fire("click");
  assert.equal(c.calls.filter((call) => call.url === "/harness/prepare").at(-1).body.kind, "change");
  c.setRun(async () => ({ status: 200, data: { status: "proposed", kind: "change", stages: [], result: { cost_usd: 0, message: "Запропоновано зміни",
    gates: [{ gate: "parse_ok", ok: true, recounted: true }, { gate: "secrets", ok: true }],
    patch: { diff: "--- a/INCIDENT_RUNBOOK.md\n+++ b/INCIDENT_RUNBOOK.md\n@@ -1,1 +1,2 @@\n x\n+y\n" },
    approval: { approval_id: "0123456789abcdef", status: "pending" } } } }));
  await c.$("harness-run").fire("click");
  const shown = texts(c.$("harness-result"));
  assert.match(shown, /Запропоновано зміни/);
  assert.match(shown, /✓ parse_ok · лічильники @@ перераховано/);
  assert.match(shown, /Відкрити заявку 0123456789 → Approve \/ Reject/);
  assert(c.$("harness-result").children.some((child) => child.tag === "pre" && /\+y/.test(child.text)), "diff is plain text");
  c.emit("agent:harnesskind", { kind: "trap" });
  await c.$("harness-prepare").fire("click");
  assert.equal(c.calls.filter((call) => call.url === "/harness/prepare").at(-1).body.kind, "read", "the trap is a question");
  console.log("PASS harness UI: default mock, no auto calls, invalidation, critical confirmation, uncertain outcome, stale response, result rendering, real availability, rule explanations, change kind with diff and approval");
})().catch((error) => { console.error(error); process.exitCode = 1; });
