import assert from 'node:assert/strict';
import {build} from 'esbuild';
async function load(path){const b=await build({entryPoints:[path],bundle:true,platform:'node',format:'esm',write:false});return import('data:text/javascript;base64,'+Buffer.from(b.outputFiles[0].text).toString('base64'));}
const {subtitleText}=await load('studio/src/screens/studio/LocalVideo.tsx');
const {visualAlias,visualRole}=await load('studio/src/screens/studio/ProjectVisualReferences.tsx');
assert.equal(visualAlias(' @personaje '),'@personaje');
assert.equal(visualAlias('Estilo_ágil'),'@Estilo_ágil');
for(const value of ['', 'hello world','../escape','x'.repeat(49)])assert.throws(()=>visualAlias(value));
assert.equal(visualRole({tags:['visual-role:style']}),'style');
assert.equal(visualRole({tags:['unknown']}),'subject');
const rows=[{start:1.125,end:3,text:'<b>Hola</b>\n\nmundo'}];
assert.match(subtitleText(rows,'srt'),/00:00:01,125 --> 00:00:03,000/);
assert.match(subtitleText(rows,'vtt'),/^WEBVTT/);
assert.ok(!subtitleText(rows,'srt').includes('<b>'));
assert.throws(()=>subtitleText([{start:2,end:1,text:'bad'}],'srt'));
assert.throws(()=>subtitleText([{start:0,end:2,text:'x'},{start:1,end:3,text:'y'}],'srt'));
assert.throws(()=>subtitleText([{start:0,end:Infinity,text:'bad'}],'srt'));
console.log('ALL OK: visual aliases, roles, subtitle formats, escaping and segment limits');
