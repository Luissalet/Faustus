import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdtempSync,writeFileSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const root=resolve('.');
const temp=mkdtempSync(join(tmpdir(),'global-approvals-'));
const input=join(temp,'entry.ts');
writeFileSync(input,`export * from ${JSON.stringify(join(root,'studio/src/shell/globalApprovals.ts'))}; export {saveSettings,loadSettings,invalidateSettings} from ${JSON.stringify(join(root,'studio/src/adapters/settings.ts'))};`);
const output=join(temp,'store.mjs');
await build({entryPoints:[input],bundle:true,platform:'node',format:'esm',outfile:output,logLevel:'silent'});
let serverMode='ask',lastPatch,failSave=false;
const timers=[];
globalThis.window=new EventTarget();window.setInterval=callback=>{timers.push(callback);return timers.length;};window.clearInterval=()=>{};
globalThis.document=new EventTarget();document.visibilityState='visible';
globalThis.localStorage={setItem(){}};
const channels=[];
globalThis.BroadcastChannel=class{constructor(){channels.push(this);}postMessage(value){for(const channel of channels)if(channel!==this)queueMicrotask(()=>channel.onmessage?.({data:value}));}close(){}};
globalThis.fetch=async(_url,options)=>{
  if(options?.method==='POST'){
    lastPatch=JSON.parse(options.body);
    if(failSave)return new Response('{}',{status:503});
    serverMode=lastPatch.tool_approval_mode??serverMode;
  }
  return new Response(JSON.stringify({tool_approval_mode:serverMode}),{status:200});
};
const a=await import(pathToFileURL(output).href+'?tab=a');
const b=await import(pathToFileURL(output).href+'?tab=b');
const wait=()=>new Promise(resolve=>setTimeout(resolve,10));
const unA=a.subscribeGlobalApprovals(()=>{}),unB=b.subscribeGlobalApprovals(()=>{});
await wait();assert.equal(a.getGlobalApprovalSnapshot().mode,'ask');
await a.setGlobalApprovalMode('full');await wait();
assert.deepEqual(lastPatch,{tool_approval_mode:'full'});
assert.equal(a.getGlobalApprovalSnapshot().mode,'full');assert.equal(b.getGlobalApprovalSnapshot().mode,'full');
// Another Settings surface uses the same ordinary adapter and broadcasts.
await b.saveSettings({tool_approval_mode:'auto'});await wait();assert.equal(a.getGlobalApprovalSnapshot().mode,'auto');
// An API/MCP writer cannot broadcast. Visible polling/focus fetches the value.
serverMode='ask';for(const timer of timers)timer();await wait();
assert.equal(a.getGlobalApprovalSnapshot().mode,'ask');assert.equal(b.getGlobalApprovalSnapshot().mode,'ask');
serverMode='full';window.dispatchEvent(new Event('focus'));await wait();assert.equal(a.getGlobalApprovalSnapshot().mode,'full');
// A read started before a successful write cannot overwrite its confirmed mode.
const pendingReads=[];globalThis.fetch=(_url,options)=>options?.method==='POST'
  ?Promise.resolve(new Response(JSON.stringify({tool_approval_mode:'auto'})))
  :new Promise(resolve=>{pendingReads.push(resolve);});
const oldRead=a.loadSettings();await a.setGlobalApprovalMode('auto');pendingReads[0](new Response(JSON.stringify({tool_approval_mode:'ask'})));await oldRead;
assert.equal(a.getGlobalApprovalSnapshot().mode,'auto');
globalThis.fetch=async()=>new Response('{}',{status:503});
await assert.rejects(a.setGlobalApprovalMode('full'));assert.equal(a.getGlobalApprovalSnapshot().mode,'auto');
unA();unB();
const composer=readFileSync(join(root,'studio/src/screens/studio/Composer.tsx'),'utf8');
assert(composer.includes('<GlobalApprovalControl />'));assert(!composer.includes('function ApprovalSelector('));
const control=readFileSync(join(root,'studio/src/shell/GlobalApprovalControl.tsx'),'utf8');
assert(!control.includes('administrator'));assert(!control.includes('busy'));
assert(readFileSync(join(root,'studio/src/shell/AppShell.tsx'),'utf8').includes('<GlobalApprovalControl variant="nav" />'));
assert(readFileSync(join(root,'studio/src/screens/Settings.tsx'),'utf8').includes('<GlobalApprovalControl variant="settings" />'));
console.log('Global approvals: shared adapter/store, two tabs, API poll/focus, write/read race, failed save and all surfaces passed');
