"use strict";
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const source=fs.readFileSync("sandbox/agent/ui/scenario-workspace.js","utf8").replace(/\r\n/g,"\n");
const start=source.indexOf("  const pendingRequests = new Map();"), end=source.indexOf('  document.addEventListener("agent:requestoutcome", event => {\n    const d = event.detail, name',start);
const listeners={}, nodes=[];let now=1000,tick;
function el(tag,props,...children){const node={tag,...props,textContent:props?.text||"",children,classList:{toggle(){}},append(...items){this.children.push(...items);}};nodes.push(node);return node;}
const context={el,requestNames:{"/studio/run":"Engine","/studio/prepare":"Пакет"},Date:{now:()=>now},setInterval:fn=>{tick=fn;},document:{querySelector:()=>({after(){}}),addEventListener:(type,fn)=>{listeners[type]=fn;}}};vm.createContext(context);vm.runInContext(source.slice(start,end),context);
const fire=(type,detail)=>listeners[type]({detail});const label=nodes.find(n=>n.tag==="strong"),time=nodes.find(n=>n.class==="live-time"),detail=nodes.find(n=>n.class==="live-detail");
fire("agent:requeststart",{requestId:1,startedAt:1000,path:"/studio/run",mode:"real"});now=4000;tick();assert.match(label.textContent,/Виконується/);assert.match(time.textContent,/3 с/);assert.match(detail.textContent,/REAL/);
fire("agent:requeststart",{requestId:2,startedAt:4000,path:"/studio/prepare",mode:"mock"});
fire("agent:requestoutcome",{requestId:2,startedAt:4000,path:"/studio/prepare",mode:"mock",http:200});assert.match(label.textContent,/Виконується: Engine/);assert.match(detail.textContent,/REAL/,"other completed request does not hide active run");
now=6000;fire("agent:requestoutcome",{requestId:1,startedAt:1000,path:"/studio/run",mode:"real",http:null});assert.match(detail.textContent,/Результат невідомий/);assert.match(time.textContent,/5 с/);
now=9000;tick();assert.match(time.textContent,/5 с/,"finished timer stays frozen");
console.log("PASS live status: elapsed time, parallel requests, frozen mode, unknown outcome, stopped timer");