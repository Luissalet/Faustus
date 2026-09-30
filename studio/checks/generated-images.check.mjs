import assert from 'node:assert/strict';
import { build, stop } from 'esbuild';
const bundled = await build({entryPoints:['studio/src/screens/studio/model.ts'], bundle:true, platform:'node', format:'esm', write:false});
const {blankTurn, apply, restoreFromMetadata} = await import(`data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString('base64')}`);
try {
  const url = '/api/generated-image/result_123.png';
  const source = '/api/generated-image/source_123.png';
  const meta = {tool_events:[{tool:'edit_image', round:1, command:'add a hat', output:'edited', exit_code:0, image_url:url, source_image_id:'source'}]};
  const live = apply(blankTurn('assistant'), {type:'image', url});
  assert.deepEqual(live.images, [url]);
  assert.deepEqual(apply(live, {type:'image', url}).images, [url]);
  const reopened = restoreFromMetadata(blankTurn('assistant'), JSON.parse(JSON.stringify(meta)));
  assert.deepEqual(reopened.images, [url], 'reopening restores the edited result');
  assert.deepEqual(restoreFromMetadata(reopened, meta).images, [url], 'repeat hydration is idempotent');
  const existing = apply(blankTurn('assistant'), {type:'image', url:source});
  assert.deepEqual(restoreFromMetadata(existing, meta).images, [source,url]);
  for (const unsafe of ['javascript:alert(1)', 'data:image/png;base64,invalid', '/api/generated-image/../secret', '/api/generated-image/result.png?token=x', 'https://user:secret@example.com/image.png']) {
    assert.deepEqual(apply(blankTurn('assistant'), {type:'image',url:unsafe}).images, []);
    assert.deepEqual(restoreFromMetadata(blankTurn('assistant'), {tool_events:[{...meta.tool_events[0],image_url:unsafe}]}).images, []);
  }
  assert.deepEqual(restoreFromMetadata(blankTurn('assistant'), {tool_events:[{...meta.tool_events[0],exit_code:1}]}).images, []);
  // The Prospero studio reports no exit code: its saved result still reopens.
  assert.deepEqual(restoreFromMetadata(blankTurn('assistant'), {tool_events:[{...meta.tool_events[0],exit_code:null}]}).images, [url]);
  // A parked approval carries no image and adds none.
  assert.deepEqual(restoreFromMetadata(blankTurn('assistant'), {tool_events:[{tool:'edit_image',round:0,command:'x',output:'Waiting for an exact user approval.',exit_code:null}]}).images, []);
  assert.deepEqual(restoreFromMetadata(blankTurn('assistant'), {tool_events:[{tool:'read_file',round:1,command:'x',output:'x',exit_code:0}]}).images, []);
  console.log('ALL OK: image result live/reopen, dedupe, source preserved, unsafe URLs and failures excluded');
} finally {stop();}
