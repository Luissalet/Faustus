import { build } from 'esbuild';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const root = resolve(import.meta.dirname, '../..');
const dir = mkdtempSync(join(tmpdir(), 'group-reasoning-'));
try {
  await build({ stdin: { resolveDir: root, contents: `
    import assert from 'node:assert/strict';
    import {presetFrom,listGroupPresets} from './studio/src/adapters/group';
    import {getReasoningLevels} from './studio/src/adapters/composer';
    import {reasoningChoice} from './studio/src/screens/group/ParticipantReasoning';
    const seats=[{route:{model:'GLM'},...reasoningChoice('max')},{route:{model:'Qwen'},...reasoningChoice('off')}];
    const preset=presetFrom('Review',seats);
    globalThis.fetch=async url=>new Response(JSON.stringify({groups:[preset]}),{headers:{'Content-Type':'application/json'}});
    const restored=(await listGroupPresets())[0].participants;
    assert.equal(restored[0].reasoningEffort,'max'); assert.equal(restored[0].thinkingEnabled,true);
    assert.equal(restored[1].thinkingEnabled,false); assert.equal(restored[1].reasoningEffort,undefined);
    assert.equal(reasoningChoice('').thinkingEnabled,undefined);
    let requested=''; globalThis.fetch=async url=>{requested=url;return new Response(JSON.stringify({levels:['low','high','max'],thinking_supported:true,thinking_toggle:false}),{headers:{'Content-Type':'application/json'}})};
    const caps=await getReasoningLevels('spark4','Qwen');
    assert.match(requested,/endpoint_id=spark4/); assert.match(requested,/model=Qwen/);
    assert.equal(caps.thinking_toggle,false); assert.deepEqual(caps.levels,['low','high','max']);
    console.log('PASS: different participant settings survive presets, explicit false stays false, inherit is unset, capabilities use selected endpoint/model');
  ` }, bundle:true, platform:'node', format:'esm', jsx:'automatic', loader:{'.css':'empty'}, banner:{js:"import {createRequire} from 'node:module'; const require=createRequire(import.meta.url);"}, outfile:join(dir,'check.mjs'), logLevel:'silent' });
  await import(pathToFileURL(join(dir,'check.mjs')).href);
} finally { rmSync(dir,{recursive:true,force:true}); }
