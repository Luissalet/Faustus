import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {dirname,resolve,join} from 'node:path';
import {mkdtempSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'../..');
const {build}=await import(pathToFileURL(join(root,'node_modules/esbuild/lib/main.js')).href);
const output=join(mkdtempSync(join(tmpdir(),'execution-defaults-')),'adapter.mjs');
await build({entryPoints:[join(root,'studio/src/adapters/projects.ts')],bundle:true,platform:'node',format:'esm',outfile:output,logLevel:'silent'});
const projects=await import(pathToFileURL(output).href);
let request;
globalThis.fetch=async(path,options)=>{request={path,options};return new Response(JSON.stringify({id:'project',execution_defaults:{thinking_enabled:false}}),{status:200});};
await projects.updateProject('project/space',{execution_defaults:{thinking_enabled:false,reasoning_effort:null},execution_defaults_expected_revision:9});
assert.equal(request.path,'/api/projects/project%2Fspace');
assert.equal(request.options.credentials,'same-origin');
assert.deepEqual(JSON.parse(request.options.body),{execution_defaults:{thinking_enabled:false,reasoning_effort:null},execution_defaults_expected_revision:9});
globalThis.fetch=async(path,options)=>{request={path,options};return new Response(JSON.stringify({project_id:'project',defaults:{},revision:0}),{status:200});};
await projects.getSessionExecutionDefaults('sid/space');
assert.equal(request.path,'/api/projects/execution-defaults/session/sid%2Fspace');
globalThis.fetch=async()=>new Response(JSON.stringify({detail:'new revision'}),{status:409});
await assert.rejects(projects.updateProject('project',{execution_defaults:{thinking_enabled:true},execution_defaults_expected_revision:8}),error=>error.status===409);
const studio=readFileSync(join(root,'studio/src/screens/Studio.tsx'),'utf8');
assert(studio.includes("const EXECUTION_KEY = 'faustus_studio_execution:'"));
assert(studio.includes('onSetAutonomyPreset={value => pickExecution'));
assert(studio.includes('executionPicks.initial_response_timeout_s'));
const chat=readFileSync(join(root,'studio/src/adapters/chat.ts'),'utf8');
assert(chat.includes("fd.append('initial_response_timeout_s', String(options.initialResponseTimeoutS))"));
const chatOutput=join(dirname(output),'chat.mjs');
await build({entryPoints:[join(root,'studio/src/adapters/chat.ts')],bundle:true,platform:'node',format:'esm',outfile:chatOutput,logLevel:'silent'});
const {sendTurn}=await import(pathToFileURL(chatOutput).href);
let posted;
globalThis.fetch=async(_path,options)=>{posted=options.body;return new Response('data: [DONE]\n\n');};
const cases=[
  [{},{},'read_only','read_only','read_only'],
  [{},{},'bounded_autonomous','bounded_autonomous','bounded_autonomous'],
  [{},{},undefined,'supervised','supervised'],
  [{},{autonomy_preset:'bounded_autonomous'},'read_only','bounded_autonomous',null],
  [{autonomy_preset:'read_only'},{autonomy_preset:'bounded_autonomous'},'supervised','read_only','read_only'],
  [{autonomy_preset:'supervised'},{autonomy_preset:'read_only'},'read_only','supervised','supervised'],
];
for(const [pick,defaults,fallback,display,wire] of cases){
  const resolution=projects.resolveExecutionAutonomy(pick,defaults,fallback);
  assert.equal(resolution.effective,display);
  for await(const _ of sendTurn({sessionId:'first-turn',message:'Hello',mode:'agent',autonomyPreset:resolution.override})){}
  assert(posted instanceof FormData);
  assert.equal(posted.get('autonomy_preset'),wire,`display=${display}, fallback=${fallback}`);
}
for(const invalid of [{},{project_id:'p',defaults:{},revision:-1},{project_id:'p',defaults:{autonomy_preset:'invented'},revision:0}]){
  globalThis.fetch=async()=>new Response(JSON.stringify(invalid),{status:200});
  await assert.rejects(projects.getSessionExecutionDefaults('invalid'),/Invalid project execution preferences/);
}
globalThis.fetch=async()=>new Response(JSON.stringify({detail:'offline'}),{status:503});
await assert.rejects(projects.getSessionExecutionDefaults('offline'),error=>error.status===503);
console.log('Execution defaults: typed PATCH/CAS, validated session resolution, pick/project/global precedence and six actual FormData sends passed');
