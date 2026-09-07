import assert from 'node:assert/strict';
import {build} from 'esbuild';
async function module(path) {
  const built = await build({entryPoints:[path], bundle:true, platform:'node', format:'esm', write:false});
  return import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`);
}
const {clipboardFiles, insertPastedText} = await module('studio/src/lib/clipboard-attachments.ts');
const {createAttachmentUploads} = await module('studio/src/lib/attachment-uploads.ts');
const picture = new File(['PNG'], 'image.png', {type:'image/png'});
const fileItem = {kind:'file', type:'image/png', getAsFile:()=>picture};
assert.deepEqual(clipboardFiles({items:[fileItem],files:[]}), [picture], 'items-only screenshot works');
assert.deepEqual(clipboardFiles({items:[],files:[picture]}), [picture], 'files-only screenshot works');
assert.equal(clipboardFiles({items:[fileItem],files:[picture]}).length, 1, 'same paste is not attached twice');
assert.equal(clipboardFiles({items:[{kind:'string',type:'text/html'}],files:[]}).length, 0, 'HTML is not fetched');
assert.equal(clipboardFiles({items:[{kind:'file',getAsFile:()=>null}],files:[picture]}).length, 1);
assert.deepEqual(insertPastedText('before AFTER end',7,12,'text'), {value:'before text end',caret:11});

const calls = [], ready = [], revoked = [], snapshots = [];
const queue = createAttachmentUploads({
  upload: (file,signal) => new Promise((resolve,reject) => calls.push({file,signal,resolve,reject})),
  ready: value => ready.push(...value), change: value => snapshots.push(value.map(x=>({...x}))),
  preview: file => 'blob:'+file.name, revoke: url => revoked.push(url), timeoutMessage:()=> 'timeout', emptyMessage:()=> 'empty',
});
const settle = async () => { for(let i=0;i<6;i++) await Promise.resolve(); };
try {
  queue.add([picture, new File(['a'],'second.png'), new File(['b'],'third.png')]);
  assert.equal(calls.length, 2, 'upload concurrency is bounded');
  assert.deepEqual(snapshots.at(-1).map(x=>x.state), ['uploading','uploading','queued']);
  assert.equal(queue.hasPending(), true, 'send guard is synchronous, even before React render');
  queue.remove('pending-1');
  assert.equal(calls[0].signal.aborted, true);
  calls[0].resolve([{id:'removed'}]); await settle();
  assert.equal(ready.length, 0, 'removed upload cannot come back');
  assert.equal(calls.length, 3, 'queued file starts when a slot is freed');
  calls[1].reject(new Error('server failure')); await settle();
  assert.equal(snapshots.at(-1).find(x=>x.id==='pending-2').state, 'failed');
  assert.equal(queue.hasPending(), true, 'failed attachments block silent image omission on Send');
  queue.retry('pending-2');
  assert.equal(calls.length,4);
  calls[3].resolve([{id:'ok'}]); await settle();
  assert.deepEqual(ready, [{id:'ok'}]);
  queue.dispose();
  assert.equal(calls[2].signal.aborted,true);
  calls[2].resolve([{id:'old-session'}]); await settle();
  assert.deepEqual(ready,[{id:'ok'}], 'old-session upload cannot attach to a new conversation');
  assert.equal(new Set(revoked).size,3, 'all preview URLs are released');
  assert.equal(revoked.length,3, 'preview URLs are released once');
  queue.resume(); queue.add([picture]);
  calls[4].resolve([]); await settle();
  assert.equal(snapshots.at(-1)[0].error,'empty');
  assert.equal(queue.hasPending(), true);
} finally { queue.dispose(); }
console.log('ALL OK: clipboard representations, text preservation, previews, bounded uploads, retry, removal and session isolation');
