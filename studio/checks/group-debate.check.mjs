import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {dirname,resolve,join} from 'node:path';
import {mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'../..');
const {build}=await import(pathToFileURL(join(root,'node_modules/esbuild/lib/main.js')));
const out=join(mkdtempSync(join(tmpdir(),'group-debate-')),'view.mjs');
await build({entryPoints:[join(root,'studio/src/screens/group/room-model.ts')],bundle:true,format:'esm',platform:'node',outfile:out,logLevel:'silent'});
const {roomView}=await import(pathToFileURL(out));
const members=['a','b'].map(sessionId=>({sessionId,personaName:sessionId,route:{model:sessionId}}));
const snapshot={group:{parentId:'room',participants:members},busy:false,turns:[
  {id:'root',message:'Consolidate the list',status:'done',debate:true},
  {id:'peer',message:'Internal peer prompt',request_id:'peer:root:1:stable',status:'done',debate:true},
  {id:'final',message:'Internal final prompt',request_id:'final:root',status:'done',debate:true},
],steps:[
  {id:'a0',turn_id:'root',member_id:'a',status:'done',text:'Proposal A',error:'',detail:{}},
  {id:'b0',turn_id:'root',member_id:'b',status:'done',text:'Proposal B',error:'',detail:{}},
  {id:'a1',turn_id:'peer',member_id:'a',status:'done',text:'Agreement. [REPORTER: @b]',error:'',detail:{}},
  {id:'b1',turn_id:'final',member_id:'b',status:'done',text:'The agreed final list.',error:'',detail:{}},
]};
let view=roomView([],snapshot,{});
assert.equal(view.turns.length,3);
assert.equal(view.turns[0].role,'user');
assert.equal(view.turns[1].debate.replies.length,3);
assert.deepEqual(view.turns[1].debate.replies.map(t=>t.text),['Proposal A','Proposal B','Agreement.']);
assert.equal(view.turns[1].debate.state,'done');
assert.equal(view.turns[2].text,'The agreed final list.');
assert.equal(view.turns[2].debate,undefined,'the final answer must remain outside the debate');
assert.equal(view.turns.filter(t=>t.role==='user').length,1,'internal prompts are never attributed to the person');
snapshot.busy=true;snapshot.turns[2].status='running';snapshot.steps[3].status='running';
view=roomView([],snapshot,{});
assert.equal(view.turns[1].debate.state,'working');
assert.equal(view.turns[1].debate.finalizing,true);
assert.deepEqual(view.turns[1].debate.speakers,['b']);
snapshot.turns[2].status='waiting_user';snapshot.steps[3].status='waiting_user';
snapshot.steps[3].detail={question:{question:'Choose A or B',question_id:'q',revision:1}};
assert.equal(roomView([],snapshot,{}).turns[1].debate.state,'waiting_user');
const stopped={group:snapshot.group,busy:false,turns:[{id:'cancelled',message:'Keep my instruction',status:'stopped',debate:true}],steps:[]};
assert.equal(roomView([],stopped,{}).turns[0].text,'Keep my instruction');
assert.equal(roomView([],stopped,{}).turns.length,1);
const queued={...stopped,turns:[{...stopped.turns[0],status:'queued'}],busy:true};
assert.equal(roomView([],queued,{}).turns[1].debate.state,'working','show progress before the first model delta');
const later={group:snapshot.group,busy:false,turns:[
  {id:'old',message:'Original instruction',status:'done',debate:true},
  {id:'cancelled',message:'Keep my cancelled instruction',status:'stopped',debate:true},
  {id:'new',message:'Newer instruction',status:'done',debate:true},
],steps:[]};
const saved=[{role:'user',content:'Original instruction',metadata:{group_turn_id:'old'},index:0},
  {role:'user',content:'Newer instruction',metadata:{group_turn_id:'new'},index:1}];
assert.deepEqual(roomView(saved,later,{}).turns.filter(t=>t.role==='user').map(t=>t.text),
 ['Original instruction','Keep my cancelled instruction','Newer instruction'],'cancelled messages must not jump after later history');
const native=[{role:'user',content:'Consolidate the list',index:0,metadata:{group_turn_id:'root'}},
 {role:'assistant',content:'Proposal A',index:1,metadata:{group_turn_id:'root',group_step_id:'a0',group_debate:true}}];
assert.equal(roomView(native,snapshot,{}).turns[1].debate.replies[0].historyIndex,1,'reply actions retain the canonical history position inside the fold');
const answered={...snapshot,turns:[snapshot.turns[0]],steps:snapshot.steps.slice(0,2)};
const continuation=[native[0],{role:'assistant',content:'B finished first',index:1,metadata:{group_turn_id:'root',group_step_id:'b0',group_debate:true}},
 {role:'user',content:'My decision',index:2,metadata:{group_answer_id:'answer-q'}},
 {role:'assistant',content:'A continued after my decision',index:3,metadata:{group_turn_id:'root',group_step_id:'a0',group_debate:true}}];
const resumed=roomView(continuation,answered,{});
assert.deepEqual(resumed.turns[1].debate.replies.map(t=>t.historyIndex),[1,3],'preserve saved reply/decision order within a parallel round');
assert.equal(resumed.turns.find(t=>t.text==='My decision').historyIndex,2);
console.log('PASS: complete debate, one separate final, native prompts hidden, reporter activity, decisions and queued/stopped instructions.');
