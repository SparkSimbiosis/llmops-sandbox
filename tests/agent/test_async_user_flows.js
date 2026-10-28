"use strict";
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const source=fs.readFileSync("sandbox/agent/ui/app.js","utf8").replace(/\r\n/g,"\n");
function between(start,end){return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));}
const fields={},requests=[],notifications=[],renders=[];let project="notes-app";
function field(id){return fields[id] ||= {value:"",disabled:false,checked:true,children:[],append(...xs){this.children.push(...xs);},replaceChildren(...xs){this.children=xs;},classList:{remove(){}}};}
function el(tag,props,...children){return {tag,props,children};}
const context={$:field,el,taskMode:()=>"real",toast:x=>notifications.push(x),confirm:()=>true,refreshToday(){},refreshApprovalCount(){},loadChats(){},loadWsRuns(){},chatRender:turns=>renders.push([...turns]),wsProject:()=>project,wsPinned:[],statusPill:()=>null,priced:()=>"cost",kpi:()=>null,short:x=>x,api:(method,path,body)=>new Promise(resolve=>requests.push({method,path,body,resolve})),renderWsAnswer:()=>renders.push("workspace"),renderWsPack(){},renderWsPackage(){}};
vm.createContext(context);vm.runInContext('let chatId=null,chatTurns=[],chatRevision=0,chatSending=false,chatHistoryPending=0,wsViewRevision=0;',context);
vm.runInContext(between('function syncChatSend()', 'async function chatSend(text) {'),context);
vm.runInContext(between('async function chatSend(text) {','async function openChat(id) {'),context);
vm.runInContext(between('async function openChat(id) {','async function loadChats() {'),context);
vm.runInContext(between('async function wsAsk() {','function renderWsAnswer('),context);
context.wsBody=()=>({question:"question",mode:"real",cache:false});
(async()=>{
 field("chat-input").value="first";
 const first=context.chatSend();await context.chatSend();assert.equal(requests.length,1,"repeat shortcut cannot send twice");
 vm.runInContext('chatRevision++;chatId=null;chatTurns=[];',context);field("chat-input").value="new draft";
 requests[0].resolve({status:200,data:{chat_id:"old",user_text:"first"}});await first;
 assert.equal(vm.runInContext("chatId",context),null,"late reply cannot switch new conversation");assert.equal(vm.runInContext("chatTurns.length",context),0);assert.equal(field("chat-input").value,"new draft");
 const a=context.openChat("a"),b=context.openChat("b");const count=requests.length;await context.chatSend("while history loads");assert.equal(requests.length,count,"cannot send into old conversation while history loads");requests[2].resolve({status:200,data:{chat_id:"b",turns:[{user_text:"b"}]}});await b;requests[1].resolve({status:200,data:{chat_id:"a",turns:[]}});await a;assert.equal(vm.runInContext("chatId",context),"b","stale history read cannot overwrite latest conversation");
 const before=requests.length;const work=context.wsAsk();await context.wsAsk();assert.equal(requests.length,before+1,"workspace shortcut cannot send twice");
 project="llm-ops";vm.runInContext("wsViewRevision++",context);requests.at(-1).resolve({status:200,data:{}});await work;assert(!renders.includes("workspace"),"stale project answer does not overwrite current view");
 const opened=[];context.wsShowFile=(...args)=>opened.push(args);
 vm.runInContext(between('function renderWsAnswer(','function renderWsPack('),context);
 context.renderWsAnswer({status:"answered",citations:[{ok:true,path:"README.md",start:1,end:2}],gates:[]},"notes-app");
 function buttons(nodes){return nodes.flatMap(n=>n&&typeof n==="object"?[...(n.tag==="button"?[n]:[]),...buttons(n.children||[])]:[]);}
 buttons(field("ws-answer").children)[0].props.on.click();assert.equal(opened[0][3],"notes-app","citation keeps result project after selector changes");
 vm.runInContext(between('async function wsShowFile(','function wsBody() {'),context);
 const file=context.wsShowFile("README.md",1,2,"notes-app");assert.equal(requests.at(-1).path,"/workspace/notes-app/file?path=README.md");requests.at(-1).resolve({status:404});await file;
 console.log("PASS async user flows: duplicate shortcut guard, late chat/history isolation, draft preservation, workspace race, source-bound citation");
})();