import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Window } from 'happy-dom';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const window = new Window({url:'http://localhost/library'});
for (const key of ['window','document','navigator','HTMLElement','Element','Node','MutationObserver','ResizeObserver','Event','CustomEvent','getComputedStyle']) Object.defineProperty(globalThis,key,{configurable:true,value:key==='getComputedStyle'?window.getComputedStyle.bind(window):key==='window'?window:window[key]});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
globalThis.requestAnimationFrame=callback=>setTimeout(callback,0);
globalThis.cancelAnimationFrame=clearTimeout;
const outfile=join(mkdtempSync(join(tmpdir(),'faustus-knowledge-')),'test.mjs');
await build({stdin:{contents:`export {LibraryScreen} from './studio/src/screens/Library'; export {KnowledgeScreen} from './studio/src/screens/library/Knowledge'; export {ContextualTools} from './studio/src/shell/contextualTools'; export {setLang} from './studio/src/i18n'; export {createElement,act} from 'react'; export {createRoot} from 'react-dom/client'; export {MemoryRouter,Routes,Route,useLocation} from 'react-router';`,resolveDir:process.cwd(),loader:'tsx'},bundle:true,format:'esm',platform:'node',outfile,define:{'process.env.NODE_ENV':'"development"'},loader:{'.css':'empty'},logLevel:'silent'});
const m=await import(pathToFileURL(outfile).href);
const requests=[];
globalThis.fetch=async(input,init={})=>{
  const url=String(input);requests.push({url,method:init.method||'GET'});
  if(url==='/api/brain/tree')return Response.json({folders:['Notes'],notes:[]});
  if(url==='/api/memory')return Response.json({memory:[{id:'kept',text:'Original stored memory',category:'fact',timestamp:1}]});
  if(url==='/api/gallery/library')return Response.json({items:[]});
  if(url==='/api/documents/library')return Response.json({documents:[]});
  return Response.json({});
};
function proof(){const loc=m.useLocation();return m.createElement('output',{'data-testid':'location'},loc.pathname+loc.search+loc.hash);}
function harness(){return m.createElement('div',null,m.createElement(proof),m.createElement(m.ContextualTools),m.createElement(m.Routes,null,
  m.createElement(m.Route,{path:'/library',element:m.createElement(m.LibraryScreen)}),
  m.createElement(m.Route,{path:'/memory',element:m.createElement(m.KnowledgeScreen)}),
  m.createElement(m.Route,{path:'/brain',element:m.createElement(m.KnowledgeScreen)})));
}
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
async function settle(){await m.act(async()=>{for(let i=0;i<8;i++)await tick();});}
async function mount(url){const host=document.createElement('div');document.body.append(host);const root=m.createRoot(host);await m.act(async()=>root.render(m.createElement(m.MemoryRouter,{initialEntries:[url]},m.createElement(harness))));await settle();return {host,root};}
const at=host=>host.querySelector('[data-testid="location"]').textContent;
const click=async(host,id)=>{await m.act(async()=>host.querySelector(`[data-testid="${id}"]`).click());await settle();};

const {host,root}=await mount('/library?type=documento&q=retained');
assert.ok(host.querySelector('[data-testid="library-type-knowledge"]'),'Knowledge is discoverable from Library');
await click(host,'library-type-knowledge');
assert.equal(at(host),'/memory');
assert.equal(host.querySelector('[data-testid="library-type-knowledge"]').getAttribute('aria-selected'),'true');
assert.ok(host.querySelector('[data-testid="memory"]'),'the original Memory screen is mounted');
assert.match(host.textContent,/Original stored memory/,'stored content survives the new navigation');
await click(host,'memory-tab-changes');
assert.equal(at(host),'/memory?t=changes');
assert.ok(host.querySelector('[data-testid="library-knowledge"]'),'native Memory query changes stay in Knowledge');
await click(host,'knowledge-brain');
assert.equal(at(host),'/brain');
assert.ok(host.querySelector('[data-testid="brain-screen"]'),'the original Brain screen is mounted');
const parent=host.querySelector('a[href="/library?type=knowledge"]');
assert.ok(parent,'return navigation points to Knowledge');
await m.act(async()=>parent.click());await settle();
assert.equal(at(host),'/memory');
assert.equal(host.querySelector('[data-testid="library-type-knowledge"]').getAttribute('aria-selected'),'true');
await click(host,'library-type-chats');
assert.equal(at(host),'/library?type=chats');
assert.ok(host.querySelector('[data-testid="library"]'));
await m.act(async()=>root.unmount());

const legacy=await mount('/brain?note=Notes%2Fkept.md&view=graph#section');
assert.equal(at(legacy.host),'/brain?note=Notes%2Fkept.md&view=graph#section','legacy URLs and every parameter remain unchanged');
assert.equal(legacy.host.querySelector('[data-testid="knowledge-brain"]').getAttribute('aria-current'),'page');
await m.act(async()=>legacy.root.unmount());
await m.act(async()=>m.setLang('es',{persist:false}));
const spanish=await mount('/memory?t=changes');
assert.equal(spanish.host.querySelector('[data-testid="library-type-knowledge"]').textContent,'Conocimiento');
assert.ok(requests.every(request=>request.method==='GET'),'navigation performs no writes or migrations');
await m.act(async()=>spanish.root.unmount());
console.log('PASS Library → Knowledge, native Memory/Brain, return, legacy queries, Spanish and read-only navigation');
await window.happyDOM.abort();
process.exit(0);
