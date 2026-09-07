import assert from 'node:assert/strict';
import {build} from 'esbuild';
async function load(path){const b=await build({entryPoints:[path],bundle:true,platform:'node',format:'esm',write:false});return import('data:text/javascript;base64,'+Buffer.from(b.outputFiles[0].text).toString('base64'));}
const {initialPanel,panelReducer,fileKey,docKey}=await load('studio/src/screens/studio/panel.ts');
const {safeHref,workspaceLink}=await load('studio/src/lib/markdown.ts');
const {persistPanels,readPanel}=await load('studio/src/screens/studio/panel-storage.ts');
const file={workspace:'D:/project',path:'plan.md'};
let state=panelReducer(initialPanel,{type:'file',...file});
state=panelReducer(state,{type:'draft',key:fileKey(file),draft:{text:'draft',base:'old',revision:'abc'}});
state=panelReducer(state,{type:'tab',tab:'agents'});
assert.equal(state.drafts[fileKey(file)].text,'draft');
assert.equal(panelReducer(state,{type:'forget',key:fileKey(file)}),state);
const doc={id:'doc1',title:'One',content:'old',version:1,streaming:false,language:'markdown',suggestions:[]};
state=panelReducer(state,{type:'doc',doc});
state=panelReducer(state,{type:'file',...file});
state=panelReducer(state,{type:'doc-saved',doc:{...doc,content:'new'}});
assert.equal(state.tab,'file');
assert.equal(state.documents[0].content,'new');
assert.equal(state.drafts[fileKey(file)].text,'draft');
assert.equal(panelReducer(state,{type:'width',width:9999}).width,900);
assert.equal(workspaceLink(safeHref('D:\\Project files\\plan.md')),'D:\\Project files\\plan.md');
assert.equal(workspaceLink('report.md:42'),'report.md');
assert.equal(workspaceLink(safeHref('report.md:42')),'report.md');
assert.equal(workspaceLink(safeHref('D:/My Project/report.md:42:3')),'D:/My Project/report.md');
assert.equal(workspaceLink('https://example.com/report.md'),null);
assert.equal(workspaceLink('javascript:alert(1)'),null);
assert.equal(safeHref('javascript:alert(1)'),'#');
assert.equal(workspaceLink('notes%20draft.md'),'notes draft.md');
assert.equal(workspaceLink('//evil.test/file.md'),null);
// Delayed save: leave the resource, return and type before the first response.
for(const kind of ['file','doc']) {
 const key=kind==='file'?fileKey(file):docKey(doc);
 const submitted={text:'first edit',base:'original',...(kind==='file'?{revision:'v1'}:{})};
 let pending=panelReducer(initialPanel,{type:'draft',key,draft:submitted});
 pending=panelReducer(pending,{type:'tab',tab:'agents'});
 pending=panelReducer(pending,{type:'tab',tab:kind});
 pending=panelReducer(pending,{type:'draft',key,draft:{...submitted,text:'newer edit'}});
 pending=panelReducer(pending,{type:'draft-saved',key,submitted,base:'first edit',revision:kind==='file'?'v2':undefined});
 assert.equal(pending.drafts[key].text,'newer edit');
 assert.equal(pending.drafts[key].base,'first edit');
 if(kind==='file')assert.equal(pending.drafts[key].revision,'v2');
 // Repeated late completion must not overwrite the rebased draft.
 assert.equal(panelReducer(pending,{type:'draft-saved',key,submitted,base:'stale',revision:'v0'}),pending);
 const next=pending.drafts[key];
 pending=panelReducer(pending,{type:'draft-saved',key,submitted:next,base:'newer edit',revision:'v3'});
 assert.equal(pending.drafts[key],undefined);
}
// Late metadata and archive responses must not replace content or steal focus.
let background=panelReducer(initialPanel,{type:'doc',doc});
background=panelReducer(background,{type:'doc-saved',doc:{...doc,content:'agent update',version:2}});
background=panelReducer(background,{type:'doc-renamed',id:doc.id,title:'Renamed'});
assert.equal(background.doc.content,'agent update');
assert.equal(background.doc.version,2);
const second={...doc,id:'doc2',title:'Two',suggestions:[{id:'two'}]};
background=panelReducer(background,{type:'doc',doc:second});
background=panelReducer(background,{type:'suggestions',docId:doc.id,suggestions:[]});
assert.equal(background.doc.suggestions.length,1);
background=panelReducer(background,{type:'forget',key:docKey(doc)});
assert.equal(background.tab,'doc');
assert.equal(background.doc.id,'doc2');
assert.equal(background.documents.length,1);

// A save completing in chat A while B is visible must survive page reload.
const saved=new Map(),written=new Map();let writes=0;
const storage={getItem:key=>saved.get(key)||null,setItem:(key,value)=>{writes++;saved.set(key,value);}};
const a=panelReducer(initialPanel,{type:'draft',key:fileKey(file),draft:{text:'first',base:'original'}});
persistPanels(storage,{a,b:state,'private:secret':a},written);
const updated=panelReducer(a,{type:'draft',key:fileKey(file),draft:{text:'newer',base:'first'}});
persistPanels(storage,{a:updated,b:state,'private:secret':updated},written);
assert.equal(writes,3); // unchanged B is not serialized again
assert.equal(readPanel(storage,'a').drafts[fileKey(file)].text,'newer');
assert.equal(saved.has('faustus.panel.private:secret'),false);
persistPanels(storage,{stream:{...state,streamDoc:doc,live:true,frames:[{src:'large frame'}]}},written);
assert.equal(JSON.parse(saved.get('faustus.panel.stream')).streamDoc,null);
assert.deepEqual(readPanel(storage,'stream').frames,[]);
assert.doesNotThrow(()=>persistPanels({setItem:()=>{throw Error('full');}},{c:a},written));
assert.equal(written.has('c'),false); // retry remains possible after storage fills
assert.deepEqual(readPanel({getItem:()=>'{broken'},'a'),initialPanel);
console.log('ALL OK: workbench drafts, background persistence, document races, width, safe local links');
