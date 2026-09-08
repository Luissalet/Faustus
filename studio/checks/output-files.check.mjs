import assert from 'node:assert/strict';
import {build} from 'esbuild';
const bundle=await build({entryPoints:['studio/src/screens/studio/output-files.ts'],bundle:true,platform:'node',format:'esm',write:false});
const {outputFiles}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const step={tool:'write_file',state:'succeeded',command:'{"path":"report.md"}'};
for(const state of ['queued','running','waiting','paused','failed','cancelled']) {
  assert.deepEqual(outputFiles([{steps:[{...step,state,diff:{file:'proposed.md'}}]}]),[]);
}
assert.deepEqual(outputFiles([{steps:[step,{...step,command:'report.md\ncontents'},
  {tool:'transform_media',state:'succeeded',output:'{"path":"output.png"}'},
  {tool:'apply_patch',state:'succeeded',diff:{file:'patched.md'}},
  {tool:'read_file',state:'succeeded',command:'source.md'},
]}]),['report.md','output.png','patched.md']);
for(const command of ['null','{}','{"path":false}','{"path":"  "}']) {
  assert.doesNotThrow(()=>outputFiles([{steps:[{tool:'transform_media',state:'succeeded',command}]}]));
}
console.log('ALL OK: output files exclude incomplete/failed writes and deduplicate completed results');
