"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor(tag, props = {}, ...children) {
    this.tag=tag;this.children=[];this.handlers={};this.disabled=false;
    Object.assign(this,props);this.append(...children);
  }
  get value(){return this._value??(this.tag==="select"?this.children[0]?.value??"":"");}
  set value(value){this._value=value;}
  append(...children){this.children.push(...children.filter((child)=>child!=null));}
  replaceChildren(...children){this.children=[];this.append(...children);}
  addEventListener(name,handler){this.handlers[name]=handler;}
  async fire(name){if(this.handlers[name])await this.handlers[name]();}
  set innerHTML(_){throw new Error("Unsafe HTML insertion");}
}
function fixture(){
  const root=new Element("div",{id:"task-intake"});
  const engine=new Element("select",{value:"architecture_engine"});
  const source=new Element("select",{value:"demo-site"});
  const fields=Object.fromEntries(["b-title","b-goal","b-context","b-input","b-constraints","b-accept","b-complexity"].map((id)=>[id,new Element("input",{value:"existing manual draft"})]));
  const listeners={};const calls=[];let invalidations=0;
  const document={addEventListener(name,fn){(listeners[name]||=[]).push(fn);},dispatchEvent(event){for(const fn of listeners[event.type]||[])fn(event);}};
  function find(element,id){if(element?.id===id)return element;for(const child of element?.children||[]){const found=typeof child==="object"&&find(child,id);if(found)return found;}}
  const $=(id)=>id==="task-intake"?root:id==="b-engine"?engine:id==="b-run-project"?source:fields[id]||find(root,id);
  const context={$,document,studio:{engines:[{id:"architecture_engine",example_project:"demo-site"},{id:"research_engine",example_project:"notes-app"}]},built:null,
    el:(tag,props,...children)=>new Element(tag,props,...children),
    invalidateBuilder(){invalidations+=1;context.built=null;document.dispatchEvent({type:"agent:builderchange"});},
    buildPrompt:async()=>{calls.push({method:"POST",url:"/studio/build"});context.built={engine:engine.value};}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../../sandbox/agent/ui/task-intake.js"),"utf8"),context);
  return{$,calls,context,event:(type,event={})=>document.dispatchEvent({type,...event}),invalidations:()=>invalidations};
}
async function answer(f){
  for(const[id,value]of[["intake-task","Prepare an ADR for the existing cache"],["intake-context","The demo-site cache is in memory and must survive only within one process"],["intake-criteria","Compare the supplied alternatives\nState known limitations"]]){
    f.$(id).value=value;await f.$(id).fire("input");
  }
}
(async()=>{
  const f=fixture();
  assert.equal(f.calls.length,0,"initialization makes no request");
  assert.equal(f.$("b-goal").value,"existing manual draft");
  await f.$("intake-structure").fire("click");
  assert.equal(f.$("intake-apply").disabled,true);
  assert.equal(f.$("intake-questions").children[1].children.length,3,"empty goal/context/criterion are explicit questions");
  assert.equal(f.calls.length,0);
  await answer(f);
  await f.$("intake-structure").fire("click");
  assert.equal(f.$("intake-questions").children.length,0,"resolved questions disappear");
  assert.equal(f.$("intake-apply").disabled,false);
  assert.equal(f.$("b-goal").value,"existing manual draft","structuring does not overwrite the existing draft");
  assert.equal(f.calls.length,0,"structuring does not compile or invoke a model automatically");
  await f.$("intake-apply").fire("click");
  assert.equal(f.$("b-engine").value,"architecture_engine","the wizard never auto-selects an Engine");
  assert.equal(f.$("b-goal").value,"Prepare an ADR for the existing cache");
  assert.equal(f.$("b-input").value,"");
  assert.equal(f.$("b-constraints").value,"");
  assert.equal(f.$("b-accept").value,"Compare the supplied alternatives\nState known limitations","no invented acceptance criteria");
  assert.equal(f.$("b-complexity").value,"","unknown complexity remains unknown");
  assert.deepEqual(f.calls,[{method:"POST",url:"/studio/build"}],"only explicit compile is called; no run/save/activate");
  assert.equal(f.invalidations(),1);
  assert(f.$("intake-status").textContent.includes("Engine ще не виконувався"));
  const g=fixture();await answer(g);await g.$("intake-structure").fire("click");
  g.$("b-engine").value="research_engine";g.event("agent:builderchange");
  assert.equal(g.$("intake-apply").disabled,true,"changing selected Engine invalidates the staged wizard draft");
  assert.equal(g.$("intake-task").value,"Prepare an ADR for the existing cache","answers remain editable");
  await g.$("intake-structure").fire("click");
  g.$("b-run-project").value="notes-app";
  await g.$("intake-apply").fire("click");
  assert.equal(g.calls.length,0,"a changed source scope cannot be silently applied");
  assert.equal(g.$("b-goal").value,"existing manual draft");
  const h=fixture();await answer(h);await h.$("intake-structure").fire("click");
  let resolve;h.context.buildPrompt=()=>new Promise((done)=>{resolve=done;});
  const pending=h.$("intake-apply").fire("click");
  h.$("b-engine").value="research_engine";h.event("agent:builderchange");
  resolve();await pending;
  assert(h.$("intake-status").textContent.includes("Engine або чернетку змінено"),"late compiler completion does not claim the new Engine draft is ready");
  console.log("PASS task intake: local clarification, no invented requirements, preserved Engine/manual draft, explicit compile only, source/Engine invalidation, stale compilation");
})().catch((error)=>{console.error(error);process.exitCode=1;});
