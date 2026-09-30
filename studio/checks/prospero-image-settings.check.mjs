// Executes the real preview, screen, router and settings adapter in happy-dom.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Window} from 'happy-dom';
import {writeFileSync, unlinkSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';

const dom = new Window({url:'http://localhost/fixture?backend=configured&lang=en'});
for (const key of ['window','document','localStorage','navigator','HTMLElement','location'])
  Object.defineProperty(globalThis,key,{value:dom[key] ?? dom,configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const host = document.createElement('main'); host.id='fixture'; document.body.append(host);
const output = resolve('.prospero-image-settings-check.mjs');
const bundled = await build({entryPoints:['studio/checks/prospero-image-settings-preview.tsx'],bundle:true,
  jsx:'automatic',format:'esm',platform:'node',packages:'external',write:false,loader:{'.css':'empty'}});
writeFileSync(output,bundled.outputFiles[0].text);
try {
  const {act} = await import('react');
  let preview;
  const flush = async (action = () => {}) => {
    await act(async () => {await action();await new Promise(resolve=>setTimeout(resolve,0));});
  };
  await flush(async()=>{preview=await import(pathToFileURL(output));});
  for(let i=0;i<4;i++) await flush();
  const select = () => document.getElementById('img-backend');
  assert.ok(select(),'Actual defaults section must render the backend selector');
  assert.equal(select().value,'configured');
  assert.equal(document.getElementById('img-model').value,'saved-image-model');
  assert.equal(document.getElementById('img-quality').value,'high');
  const posts = () => preview.fixtureRequests.filter(row=>row.url==='/api/auth/settings' && row.method==='POST');
  const change = async value => flush(()=>{select().value=value;select().dispatchEvent(new window.Event('change',{bubbles:true}));});
  await change('prospero');
  assert.equal(document.getElementById('img-model'),null);
  assert.equal(document.getElementById('img-quality'),null);
  assert.match(document.body.textContent,/Uses your connected Prospero studio/);
  await flush(()=>document.querySelector('[data-testid="settings-save"]').click());
  assert.deepEqual(posts()[0].body,{image_execution_backend:'prospero'});
  assert.equal(posts()[0].credentials,'same-origin');
  assert.equal(posts()[0].contentType,'application/json');
  assert.equal(preview.fixtureSettings.image_model,'saved-image-model');
  assert.equal(preview.fixtureSettings.image_quality,'high');
  assert.equal(preview.fixtureSettings.default_model,'saved-chat-model');
  await change('configured');
  assert.equal(document.getElementById('img-model').value,'saved-image-model');
  assert.equal(document.getElementById('img-quality').value,'high');
  await flush(()=>document.querySelector('[data-testid="settings-save"]').click());
  assert.deepEqual(posts()[1].body,{image_execution_backend:'configured'});
  assert.equal(posts().length,2);
  assert.equal(preview.fixtureSettings.vision_model,'saved-vision-model');
  assert.deepEqual(preview.unexpectedRequests,[]);
  await flush(()=>preview.previewRoot.unmount());
  console.log('ALL OK: real Settings defaults, Prospero/configured save contract, model/quality/chat/vision preferences retained');
} finally {
  unlinkSync(output);
  await dom.happyDOM.close();
}
