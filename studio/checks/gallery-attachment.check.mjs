import assert from 'node:assert/strict';
import {build} from 'esbuild';
const bundle=await build({entryPoints:['studio/src/lib/gallery-attachment.ts'],bundle:true,platform:'node',format:'esm',write:false});
const {galleryAttachment}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
let calls=0;
globalThis.fetch=async(url,options)=>{calls++;assert.equal(options.redirect,'error');return new Response('png',{headers:{'Content-Type':'image/png'}});};
for(const url of ['https://evil.test/image.png','//evil.test/image.png','data:image/png,x','javascript:alert(1)'])
  await assert.rejects(()=>galleryAttachment(url,'x.png','http://localhost'));
assert.equal(calls,0);
const file=await galleryAttachment('/api/generated-image/x.png','x.png','http://localhost');
assert.equal(file.type,'image/png');assert.equal(file.name,'x.png');
globalThis.fetch=async()=>new Response('not found',{status:404});
await assert.rejects(()=>galleryAttachment('/missing','x.png','http://localhost'),/404/);
globalThis.fetch=async()=>new Response('<html>login</html>',{headers:{'Content-Type':'text/html'}});
await assert.rejects(()=>galleryAttachment('/login','x.png','http://localhost'),/not return an image/);
console.log('ALL OK: gallery attachments reject remote links, failed downloads and non-images');
