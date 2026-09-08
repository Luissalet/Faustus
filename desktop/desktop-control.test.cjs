const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync}=require('node:fs');
const {tmpdir}=require('node:os');
const {join}=require('node:path');
const {startDesktopControl}=require('./desktop-control.cjs');

test('six non-interactive overlays acknowledge readiness and Escape cancels',async()=>{
  const root=mkdtempSync(join(tmpdir(),'faustus-overlay-'));
  const dir=join(root,'data','runtime');mkdirSync(dir,{recursive:true});
  const made=[];let escape;
  class Window{
    constructor(options){this.options=options;made.push(this);}
    setIgnoreMouseEvents(value){this.ignore=value;}
    setContentProtection(){}
    setAlwaysOnTop(){}
    async loadURL(url){assert.match(decodeURIComponent(url),/Esc para detener/);}
    showInactive(){this.shown=true;}
    isDestroyed(){return !!this.destroyed;}
    destroy(){this.destroyed=true;}
  }
  const stop=startDesktopControl({BrowserWindow:Window,screen:{getAllDisplays:()=>Array.from({length:6},(_,i)=>({bounds:{x:(i-2)*1920,y:0,width:1920,height:1080}}))},globalShortcut:{register:(key,fn)=>{escape=fn;return true;},unregister:()=>{}}},root);
  try{
    writeFileSync(join(dir,'desktop-control.json'),JSON.stringify({token:'test',updated:Date.now()/1000}));
    for(let i=0;i<30&&!made.length;i++)await new Promise(r=>setTimeout(r,30));
    assert.equal(made.length,6);
    assert.ok(made.every(w=>w.shown&&w.ignore&&w.options.focusable===false));
    assert.equal(JSON.parse(readFileSync(join(dir,'desktop-control-ack.json'))).ready,true);
    escape();
    assert.equal(JSON.parse(readFileSync(join(dir,'desktop-cancel.json'))).token,'test');
    assert.ok(made.every(w=>w.destroyed));
    await new Promise(r=>setTimeout(r,150));
    assert.equal(made.length,6,'cancelled run must not reactivate');
  }finally{stop();rmSync(root,{recursive:true,force:true});}
});
