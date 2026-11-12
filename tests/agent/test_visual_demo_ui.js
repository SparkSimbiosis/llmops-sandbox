"use strict";
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const source=fs.readFileSync("sandbox/agent/ui/scenario-workspace.js","utf8");const fragment=source.slice(source.indexOf("// VISUAL_DEMO_START"));
const pure={window:{}};vm.runInNewContext(fragment,pure);
assert.deepEqual(Object.keys(pure.window.AgentDemoStory.recipes),["read","change","trap"]);
assert.match(pure.window.AgentDemoStory.frames("read")[0].artifact,/llm-ops/);assert.match(pure.window.AgentDemoStory.frames("trap")[5].artifact,/reply_leak[\s\S]*blocked_by_output_gate/);
for(const kind of Object.keys(pure.window.AgentDemoStory.recipes))for(const mode of ["mock","real"])for(const failure of [false,true]){
 const frames=pure.window.AgentDemoStory.frames(kind,"<script>task</script>",mode,failure);
 assert.equal(frames.length,7);assert(frames[2].artifact.includes("<script>task</script>"));assert(frames[3].artifact.includes(mode.toUpperCase()));
 assert.match(frames[6].artifact,/Мережевих викликів моделі: 0/);assert.match(frames[6].artifact,/Записів виконання в БД: 0/);
 if(failure)assert.match(frames[5].artifact,/rejected/);
}
const nodes={};let apiCalls=0,applyCalls=0,providerChanges=0,opened;
class Element{
 constructor(tag,props={},...children){this.tag=tag;this.children=children;this.handlers={};this.style={};this.hidden=false;this.disabled=false;this.textContent=props.text||"";Object.assign(this,props);if(props.id)nodes[props.id]=this;this.classList={toggle(){}};}
 get value(){return this._value??this.children.find(n=>n?.value)?.value??"";} set value(v){this._value=v;}
 addEventListener(type,fn){this.handlers[type]=fn;} async fire(type){if(!this.disabled)return this.handlers[type]?.();}
 replaceChildren(...children){this.children=children;}prepend(...children){this.children.unshift(...children);}append(...children){this.children.push(...children);}
 dispatchEvent(){}scrollIntoView(){}focus(){}getBoundingClientRect(){return{height:69};}set innerHTML(_){throw new Error("unsafe HTML");}
}
const lessonHost=new Element("details",{id:"demo-lesson"});nodes["execution-status"]=new Element("section");nodes["harness-goal"]=new Element("textarea");
const context={window:{AgentDemoCatalog:{catalog:async()=>["harness-ops","harness-ops-change","harness-demo","harness-trap"].map(id=>({id,target:"harness"})),apply:async()=>{applyCalls++;}}},document:{},el:(...args)=>new Element(...args),$:id=>nodes[id],requestAnimationFrame:fn=>fn(),taskMode:()=>"real",setProvider:()=>{providerChanges++;},api:()=>{apiCalls++;throw new Error("hidden API");},showTab:target=>{opened=target;},toast(){},Event:class{constructor(type){this.type=type;}}};
vm.runInNewContext(fragment,context);
(async()=>{
 assert.equal(lessonHost.children.at(-1)?.id,"visual-demo","lesson lives inside the collapsed Demo block");assert.equal(applyCalls,0);
 nodes["story-mode"].value="real";await nodes["story-mode"].fire("change");
 for(let i=0;i<6;i++)await nodes["story-next"].fire("click");
 assert.equal(nodes["story-next"].disabled,true);assert.equal(nodes["story-transfer"].hidden,false);assert.equal(apiCalls,0);assert.equal(applyCalls,0);assert.equal(providerChanges,0);
 nodes["story-task"].value="My explicit task";await nodes["story-task"].fire("input");for(let i=0;i<6;i++)await nodes["story-next"].fire("click");
 await nodes["story-transfer"].fire("click");assert.equal(applyCalls,1);assert.equal(opened,"harness");assert.equal(nodes["harness-goal"].value,"My explicit task");assert.equal(providerChanges,0);assert.equal(apiCalls,0);
 console.log("PASS visual demo: 12 lesson variants, seven stages, no model/API on navigation, no provider switching, explicit transfer only");
})();