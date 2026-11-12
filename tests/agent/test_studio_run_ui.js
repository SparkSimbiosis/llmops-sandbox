"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const app = fs.readFileSync(path.join(__dirname, "../../sandbox/agent/ui/app.js"), "utf8");
class Element {
  constructor(tag, props = {}, ...children) {
    this.tag = tag; this.children = []; this.handlers = {}; this.disabled = false; this.checked = false;
    for (const [key, value] of Object.entries(props)) {
      if (key === "on") this.handlers = { ...value }; else this[key] = value;
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
function eventBus() {
  const handlers = {};
  return { addEventListener(name, fn) { (handlers[name] ||= []).push(fn); },
    dispatchEvent(event) { for (const fn of handlers[event.type] || []) fn(event); } };
}
const CustomEvent = class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } };
const flush = () => new Promise((resolve) => setImmediate(resolve));
function testProviderAndExamples() {
  const fields = Object.fromEntries(["provider", "chat-mode", "b-engine", "b-title", "b-goal", "b-context", "b-input", "b-constraints", "b-accept", "b-complexity", "b-save", "b-yaml", "b-result"].map((id) => [id, new Element("div")]));
  const doc = eventBus(); doc.querySelectorAll = () => [];
  const exampleEvents = []; doc.addEventListener("agent:builderexample", (event) => exampleEvents.push(event.detail));
  const events = []; doc.addEventListener("agent:providerchange", (event) => events.push(event.detail.mode));
  const context = { $: (id) => fields[id], document: doc, CustomEvent, providerMode: "real", provider: "mock",
    fillModelSelect() {}, renderBanner() {}, toast() {}, studio: { engines: [
      { id: "research_engine", example: { title: "Research", task_goal: "Compare approaches", constraints: ["Cite supplied data"], acceptance_criteria: ["Sources", "Risks"] } },
      { id: "code_engine", example: { title: "Code", task_goal: "Add login", constraints: ["Allowed paths"], acceptance_criteria: ["Minimal diff"] } },
    ] }, builderGeneration: 0, built: {}, el: (tag, props, ...children) => new Element(tag, props, ...children) };
  context.taskMode = () => context.provider === "real" && context.providerMode === "real" ? "real" : "mock";
  const providerStart = app.indexOf("function setProvider(");
  vm.runInNewContext(app.slice(providerStart, app.indexOf('\ndocument.querySelectorAll(".seg.prov button").forEach', providerStart)), context);
  context.setProvider("real");
  assert.equal(fields["chat-mode"].textContent, "наступний запит: real");
  assert.deepEqual(events, ["real"]);
  context.setProvider("mock");
  assert.equal(fields["chat-mode"].textContent, "наступний запит: mock");
  const invalidStart = app.indexOf("function invalidateBuilder(");
  vm.runInNewContext(app.slice(invalidStart, app.indexOf("\nconst lines =", invalidStart)), context);
  const fillStart = app.indexOf("function fillExample(");
  vm.runInNewContext(app.slice(fillStart, app.indexOf('\n$("b-build").addEventListener', fillStart)), context);
  fields["b-engine"].value = "research_engine"; context.fillExample();
  assert.equal(fields["b-goal"].value, "Compare approaches");
  assert.equal(fields["b-accept"].value, "Sources\nRisks");
  assert.equal(context.built, null);
  assert.equal(exampleEvents.at(-1).project, "notes-app");
  fields["b-engine"].value = "code_engine"; context.fillExample();
  assert.equal(fields["b-goal"].value, "Add login");
  assert.equal(fields["b-accept"].value, "Minimal diff");
  assert.equal(exampleEvents.at(-1).project, "demo-site");
  context.studio.engines[1].example_project = "llm-ops";
  context.fillExample();
  assert.equal(exampleEvents.at(-1).project, "llm-ops", "catalog source overrides fallback");
}
async function testBannerRace() {
  const banner = new Element("div");
  let mode = "real", resolve;
  const context = { $:()=>banner, bannerGeneration:0, taskMode:()=>mode, usd:(v)=>String(v),
    api:()=>new Promise((done)=>{resolve=done;}) };
  const start=app.indexOf("async function renderBanner(");
  vm.runInNewContext(app.slice(start, app.indexOf("async function loadProjects(", start)),context);
  const pending=context.renderBanner();
  mode="mock"; await context.renderBanner();
  resolve({status:200,data:{real_today_usd:.01,real_budget_usd:1}}); await pending;
  assert.equal(banner.hidden,true,"a late real-cost response cannot restore a banner after switching to mock");
}
async function fixture() {
  const root = new Element("div", { id: "b-runtime" });
  const engine = new Element("select", { value: "research_engine" });
  const doc = eventBus(); const calls = [];
  let mode = "mock", goal = "Compare notes storage";
  let prepareReply = async () => ({ status: 200, data: { preparation_id: "engine-packet", packet: { engine: "research_engine", role: {name:"researcher"}, contract: {id:"research"}, model:"fake-mini", messages_sha256:"abc123" } } });
  let runReply = async () => ({ status: 200, data: { run_id:"run-1", engine:"research_engine", role:"researcher", contract:"research", status:"completed", mode:"mock", calls:0,cache_hits:1,checks:[{gate:"sections_present",ok:true}], output:"Synthetic mock fixture", cost_usd:0 } });
  function find(element, id) { if (element?.id === id) return element; for (const child of element?.children || []) { const result = typeof child === "object" && find(child,id); if(result) return result; } }
  const $ = (id) => id === "b-runtime" ? root : id === "b-engine" ? engine : find(root,id);
  const context = { $, document:doc, CustomEvent, studio:{engines:[{id:"research_engine"}]}, draft:()=>({task_goal:goal}), taskMode:()=>mode,
    el:(tag, props,...children)=>new Element(tag,props,...children), kpi:(key,value)=>new Element("div",{text:key+":"+value}),
    short:(value)=>value?.slice(0,10), priced:(cost,selected)=>selected+":"+cost, Date, setTimeout:()=>1,clearTimeout:()=>{},
    refreshToday(){},toast(){}, api:async(method,url,body)=>{
      calls.push({method,url,body});
      if(url==="/harness/projects") return {status:200,data:{projects:[{id:"demo-site",available:true},{id:"notes-app",available:true}]}};
      if(url==="/studio/runs") return {status:200,data:{runs:[]}};
      if(url==="/studio/prepare") return prepareReply(body);
      if(url==="/studio/run") return runReply(body);
      throw new Error("Unexpected request "+url);
    } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../../sandbox/agent/ui/studio-run.js"),"utf8"),context);
  await flush();
  return {$, calls, event:(name,detail)=>doc.dispatchEvent({type:name,detail}), mode:(value)=>{mode=value;doc.dispatchEvent({type:"agent:providerchange"});},
    goal:(value)=>{goal=value;doc.dispatchEvent({type:"agent:builderchange"});}, setPrepare:(fn)=>{prepareReply=fn;},setRun:(fn)=>{runReply=fn;}};
}
(async()=>{
  testProviderAndExamples();
  await testBannerRace();
  const f=await fixture();
  assert(f.calls.every((call)=>call.method==="GET"),"initialization never invokes or prepares");
  assert.equal(f.$("b-run-execute").disabled,true);
  await f.$("b-run-prepare").fire("click");
  assert.equal(f.$("b-run-execute").disabled,false);
  assert.equal(f.calls.at(-1).body.engine,"research_engine");
  assert.equal(f.calls.at(-1).body.cache,true);
  f.event("agent:builderexample", {engine:"research_engine",project:"notes-app"});
  assert.equal(f.$("b-run-project").value,"notes-app","explicit example selects its matching source project");
  assert.equal(f.$("b-run-execute").disabled,true,"example/source changes invalidate the prepared packet");
  f.$("b-run-project").value="demo-site";
  f.event("agent:builderchange");
  assert.equal(f.$("b-run-project").value,"demo-site","changing only Engine/draft preserves the user's project selection");
  f.mode("real");
  assert.equal(f.$("b-run-mode").textContent,"наступний Engine: real");
  assert.equal(f.$("b-run-execute").disabled,true);
  f.$("b-run-cache").checked=false; await f.$("b-run-cache").fire("change");
  await f.$("b-run-prepare").fire("click");
  assert.equal(f.calls.at(-1).body.mode,"real");
  assert.equal(f.calls.at(-1).body.cache,false);
  await f.$("b-run-execute").fire("click");
  assert.equal(f.$("b-run-execute").disabled,true,"consumed packet cannot run twice");
  assert(f.$("b-run-result").children.length>0,"result includes selected role and contract");
  let resolve;
  f.setPrepare(()=>new Promise((done)=>{resolve=done;}));
  const pending=f.$("b-run-prepare").fire("click");
  f.goal("Changed while preparing");
  resolve({status:200,data:{preparation_id:"stale",packet:{}}}); await pending;
  assert.equal(f.$("b-run-execute").disabled,true,"stale async packet is discarded");
  f.setPrepare(async()=>({status:200,data:{preparation_id:"uncertain",packet:{}}}));
  await f.$("b-run-prepare").fire("click");
  f.setRun(async()=>{throw new Error("connection interrupted");});
  await f.$("b-run-execute").fire("click");
  assert.equal(f.$("b-run-execute").disabled,true,"uncertain send has no automatic retry");
  assert(f.$("b-run-status").textContent.includes("Автоповтору немає"));
  console.log("PASS studio UI: immediate shared mode, engine-specific examples, no auto calls, frozen preparation, invalidation, cache, selected role/contract, uncertain no retry");
})().catch((error)=>{console.error(error);process.exitCode=1;});
