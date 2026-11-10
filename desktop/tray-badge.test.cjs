const {test}=require('node:test');
const assert=require('node:assert/strict');
const badge=require('./tray-badge.cjs');
const {readPlacement,resolveTarget}=require('./target.cjs');
const {mkdtempSync,mkdirSync,writeFileSync}=require('node:fs');
const {join}=require('node:path');
const {tmpdir}=require('node:os');

// Bitmaps are B, G, R, A.
const pixel=(buffer,size,x,y)=>{const i=(y*size+x)*4;return [buffer[i+2],buffer[i+1],buffer[i],buffer[i+3]];};
const blank=size=>Buffer.alloc(size*size*4);
const count=(buffer,size,rgb)=>{let n=0;for(let y=0;y<size;y++)for(let x=0;x<size;x++){const p=pixel(buffer,size,x,y);if(p[0]===rgb[0]&&p[1]===rgb[1]&&p[2]===rgb[2]&&p[3]===255)n++;}return n;};

test('the mark follows the summary: nothing when idle, the count when running, a sign for the rest',()=>{
  assert.equal(badge.markFor({kind:'idle'},''),null);
  assert.deepEqual(badge.markFor({kind:'running',count:2,corner:''},''),{kind:'running',glyph:'2',corner:''});
  assert.equal(badge.markFor({kind:'running',count:14,corner:'done'},'').glyph,'+');
  assert.deepEqual(badge.markFor({kind:'running',count:1,corner:'done'},''),{kind:'running',glyph:'1',corner:'done'});
  assert.equal(badge.markFor({kind:'waiting',count:1},'').glyph,'?');
  assert.equal(badge.markFor({kind:'failed',count:1},'').glyph,'!');
  assert.equal(badge.markFor({kind:'done',count:3},'').glyph,'tick');
  assert.equal(badge.markFor({kind:'running',count:2},'offline').kind,'offline');
});

test('idle leaves the icon untouched; a mark paints its colour in the bottom-right corner only',()=>{
  for(const size of [16,24,32]){
    const base=blank(size);
    assert.deepEqual(badge.paint(base,size,null),base);
    const out=badge.paint(base,size,{kind:'running',glyph:'3',corner:''});
    assert.notDeepEqual(out,base);
    assert.deepEqual(base,blank(size),'the base bitmap is never written to');
    assert.ok(count(out,size,badge.COLORS.running)>size,'blue pixels at '+size);
    assert.deepEqual(pixel(out,size,1,1),[0,0,0,0],'top-left stays clear');
    assert.deepEqual(pixel(out,size,size-2,1),[0,0,0,0],'no corner dot unless asked');
    assert.ok(count(out,size,[255,255,255])>=5,'the digit is drawn in white');
  }
});

test('each state has its own colour, and the corner dot its own too',()=>{
  const size=32;
  for(const kind of ['waiting','failed','done','offline']){
    const out=badge.paint(blank(size),size,{kind,glyph:kind==='done'?'tick':'?',corner:''});
    assert.ok(count(out,size,badge.COLORS[kind])>40,kind);
    assert.equal(count(out,size,badge.COLORS.running),0,kind+' is not blue');
  }
  const both=badge.paint(blank(size),size,{kind:'running',glyph:'1',corner:'failed'});
  assert.ok(count(both,size,badge.COLORS.failed)>8,'red dot in the top-right corner');
  const [r,g,b,a]=pixel(both,size,size-5,5);
  assert.deepEqual([r,g,b,a],[...badge.COLORS.failed,255]);
});

test('different counts draw different marks',()=>{
  const draw=glyph=>badge.paint(blank(16),16,{kind:'running',glyph,corner:''});
  assert.notDeepEqual(draw('1'),draw('2'));
  assert.notDeepEqual(draw('9'),draw('+'));
});

test('the taskbar overlay is the mark alone, centred',()=>{
  const out=badge.alone(16,{kind:'waiting',glyph:'?'});
  assert.deepEqual(pixel(out,16,0,0),[0,0,0,0]);
  assert.ok(count(out,16,badge.COLORS.waiting)>60);
  assert.deepEqual(badge.alone(16,null),blank(16));
});

test('the window attaches to this PC unless the Hub moved Faustus, or an address is forced',()=>{
  assert.deepEqual(resolveTarget({env:{},placement:null,port:7000}),{origin:'http://127.0.0.1:7000',remote:false,where:''});
  assert.deepEqual(resolveTarget({env:{},placement:{location:'pc',urls:{faustus:'https://spark3.example.ts.net'}},port:7001}).origin,'http://127.0.0.1:7001');
  assert.deepEqual(resolveTarget({env:{},placement:{location:'sparks',node:'Spark3',urls:{faustus:'https://spark3.example.ts.net/'}},port:7000}),
    {origin:'https://spark3.example.ts.net',remote:true,where:'Spark3'});
  // Moved, but the Spark has no address yet: start here and let the launcher's own refusal speak.
  assert.equal(resolveTarget({env:{},placement:{location:'sparks',urls:{}},port:7000}).remote,false);
  for(const bad of ['file:///C:/x','javascript:alert(1)','https://user:pw@host'])assert.equal(resolveTarget({env:{},placement:{location:'sparks',urls:{faustus:bad}},port:7000}).remote,false,bad);
  assert.deepEqual(resolveTarget({env:{FAUSTUS_DESKTOP_URL:'http://127.0.0.1:7000/studio'},placement:{location:'sparks',urls:{faustus:'https://spark3.example.ts.net'}},port:7000}),
    {origin:'http://127.0.0.1:7000',remote:true,where:''});
});

test('the placement marker is read from data/runtime, and a missing or broken one means "here"',()=>{
  const root=mkdtempSync(join(tmpdir(),'faustus-target-'));
  assert.equal(readPlacement(root),null);
  mkdirSync(join(root,'data','runtime'),{recursive:true});
  writeFileSync(join(root,'data','runtime','placement.json'),'{not json');
  assert.equal(readPlacement(root),null);
  writeFileSync(join(root,'data','runtime','placement.json'),JSON.stringify({location:'sparks',node:'Spark3',urls:{faustus:'https://s.example'}}));
  assert.equal(resolveTarget({env:{},placement:readPlacement(root),port:7000}).origin,'https://s.example');
});
