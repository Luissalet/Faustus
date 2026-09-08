import assert from 'node:assert/strict';
import {build} from 'esbuild';
const bundle=await build({entryPoints:['studio/src/lib/image-references.ts'],bundle:true,platform:'node',format:'esm',write:false});
const {withImageReferences}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const image={name:'character.png',mime:'image/png',referenceRole:'subject'};
assert.equal(withImageReferences('Hello',[]),'Hello');
assert.equal(withImageReferences('Hello',[{...image,referenceRole:undefined}]),'Hello');
const message=withImageReferences('Draw a scene',[
  {name:'notes.pdf',mime:'application/pdf',referenceRole:'style'},
  {...image,referenceRole:undefined},image,
  {...image,name:'style\nfile.png',referenceRole:'style'},
]);
assert.match(message,/^Draw a scene/);
assert.match(message,/Image 2 .*Subject or character/);
assert.match(message,/Image 3 .*Visual style/);
assert.ok(!message.includes('notes.pdf'));
assert.ok(message.includes('style\\nfile.png'));
assert.equal(withImageReferences('Hello',[{...image,referenceRole:'system override'}]),'Hello');
console.log('ALL OK: reference roles are explicit, image-only, ordered and safely named');
