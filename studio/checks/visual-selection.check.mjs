import assert from 'node:assert/strict';
import {build} from 'esbuild';
const bundle=await build({entryPoints:['studio/src/screens/studio/FrameSelection.tsx'],bundle:true,platform:'node',format:'esm',write:false});
const {selectionPrompt,selectionImage}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const selection={frame:{src:'data:image/png;base64,AAAA',url:'https://example.com/\n"test',title:'Button',at:1000},x:20,y:75,request:' Make it larger '};
const prompt=selectionPrompt(selection);
assert.ok(prompt.startsWith('Make it larger\n\n'));
assert.match(prompt,/"pointPercent":\{"x":20,"y":75\}/);
assert.match(prompt,/1970-01-01T00:00:01.000Z/);
assert.match(prompt,/past observation/);
assert.ok(prompt.includes('\\n\\"test'));
for(const src of ['https://remote.test/x.png','data:image/svg+xml;base64,AAAA','data:image/png;base64,%%%']){
  await assert.rejects(()=>selectionImage({...selection,frame:{...selection.frame,src}}),/cannot be attached/);
}
let drawn=false,marked=false;
globalThis.Image=class {naturalWidth=640;naturalHeight=480;async decode(){}};
globalThis.File=class {constructor(parts,name,options){this.parts=parts;this.name=name;this.type=options.type;}};
globalThis.document={createElement:()=>({getContext:()=>({drawImage(){drawn=true;},beginPath(){},arc(x,y){assert.equal(x,128);assert.equal(y,360);marked=true;},stroke(){},moveTo(){},lineTo(){}}),toBlob(cb){cb(new Blob(['png'],{type:'image/png'}));}})};
const file=await selectionImage(selection);
assert.equal(file.type,'image/png');assert.ok(drawn&&marked);
globalThis.Image=class {naturalWidth=100000;naturalHeight=100000;async decode(){}};
await assert.rejects(()=>selectionImage(selection),/too large/);
console.log('ALL OK: visual selection keeps provenance, rejects URLs and oversized captures, marks the requested coordinates');
