const {test}=require('node:test');
const assert=require('node:assert/strict');
const {startDictation}=require('./dictation.cjs');

function fakeNet(routes){
  return {fetch: async url=>{
    for(const [pattern,handler] of routes)if(url.includes(pattern))return handler(url);
    return {ok:false,status:404,json:async()=>({})};
  }};
}
function jsonResponse(body,ok=true,status=200){return {ok,status,json:async()=>body};}

class FakeWindow{
  constructor(){this.sent=[];this.destroyed=false;}
  async loadURL(){}
  isDestroyed(){return this.destroyed;}
  destroy(){this.destroyed=true;}
  get webContents(){return {send:(...args)=>this.sent.push(args)};}
}

function fakeElectron({windows=[]}={}){
  const shortcuts={};
  let windowIndex=0;
  return {
    BrowserWindow: class{constructor(){this.instance=windows[windowIndex++]||new FakeWindow();return this.instance;}},
    globalShortcut: {
      register: (key,fn)=>{shortcuts[key]=fn;return true;},
      unregister: key=>{delete shortcuts[key];},
      _fire: key=>shortcuts[key]&&shortcuts[key](),
      _registered: ()=>Object.keys(shortcuts),
    },
    ipcMain: {
      listeners:{},
      on(event,fn){this.listeners[event]=fn;},
      removeListener(event){delete this.listeners[event];},
    },
  };
}

test('polling registers the configured hotkey only when dictation is enabled', async()=>{
  const electron=fakeElectron();
  const net=fakeNet([['/api/settings',()=>jsonResponse({dictation_anywhere_enabled:false,dictation_global_hotkey:'Ctrl+Alt+Space'})]]);
  const stop=startDictation({...electron,net},{origin:'http://127.0.0.1:7000',pollMs:1000000});
  await new Promise(r=>setImmediate(r));
  assert.deepEqual(electron.globalShortcut._registered(),[]);
  stop();
});

test('enabling dictation registers the hotkey; disabling unregisters it', async()=>{
  const electron=fakeElectron();
  let enabled=true;
  const net={fetch: async url=>{
    if(url.includes('/api/settings'))return jsonResponse({dictation_anywhere_enabled:enabled,dictation_global_hotkey:'Ctrl+Alt+Space'});
    return jsonResponse({});
  }};
  const stop=startDictation({...electron,net},{origin:'http://127.0.0.1:7000',pollMs:5});
  await new Promise(r=>setTimeout(r,20));
  assert.deepEqual(electron.globalShortcut._registered(),['Ctrl+Alt+Space']);
  enabled=false;
  await new Promise(r=>setTimeout(r,20));
  assert.deepEqual(electron.globalShortcut._registered(),[]);
  stop();
});

test('changing the hotkey re-registers the new one and drops the old', async()=>{
  const electron=fakeElectron();
  let hotkey='Ctrl+Alt+Space';
  const net={fetch: async url=>{
    if(url.includes('/api/settings'))return jsonResponse({dictation_anywhere_enabled:true,dictation_global_hotkey:hotkey});
    return jsonResponse({});
  }};
  const stop=startDictation({...electron,net},{origin:'http://127.0.0.1:7000',pollMs:5});
  await new Promise(r=>setTimeout(r,20));
  assert.deepEqual(electron.globalShortcut._registered(),['Ctrl+Alt+Space']);
  hotkey='Ctrl+Alt+D';
  await new Promise(r=>setTimeout(r,20));
  assert.deepEqual(electron.globalShortcut._registered(),['Ctrl+Alt+D']);
  stop();
});

test('press captures the foreground target and starts the hidden recorder; press again stops it', async()=>{
  const electron=fakeElectron();
  const states=[];
  const captured={handle:42,title:'Notepad'};
  const net={fetch: async url=>{
    if(url.includes('/api/settings'))return jsonResponse({dictation_anywhere_enabled:true,dictation_global_hotkey:'Ctrl+Alt+Space',dictation_paste_method:'type'});
    if(url.includes('/api/dictation/capture-target'))return jsonResponse(captured);
    return jsonResponse({});
  }};
  const stop=startDictation({...electron,net},{origin:'http://127.0.0.1:7000',pollMs:5,onState:s=>states.push(s)});
  await new Promise(r=>setTimeout(r,20));
  electron.globalShortcut._fire('Ctrl+Alt+Space');
  await new Promise(r=>setTimeout(r,20));
  assert.equal(states.at(-1).recording,true);
  assert.deepEqual(states.at(-1).target,captured);

  electron.globalShortcut._fire('Ctrl+Alt+Space');
  assert.equal(states.at(-1).recording,false);
  stop();
});

test('a failed capture-target never starts a recording window', async()=>{
  const electron=fakeElectron();
  const states=[];
  const net={fetch: async url=>{
    if(url.includes('/api/settings'))return jsonResponse({dictation_anywhere_enabled:true,dictation_global_hotkey:'Ctrl+Alt+Space'});
    if(url.includes('/api/dictation/capture-target'))return jsonResponse({},false,501);
    return jsonResponse({});
  }};
  const stop=startDictation({...electron,net},{origin:'http://127.0.0.1:7000',pollMs:5,onState:s=>states.push(s)});
  await new Promise(r=>setTimeout(r,20));
  electron.globalShortcut._fire('Ctrl+Alt+Space');
  await new Promise(r=>setTimeout(r,20));
  assert.equal(states.at(-1).recording,false);
  assert.ok(states.at(-1).error);
  stop();
});

test('a dictation result destroys the hidden recorder window', async()=>{
  const win=new FakeWindow();
  const electron=fakeElectron({windows:[win]});
  const net={fetch: async url=>{
    if(url.includes('/api/settings'))return jsonResponse({dictation_anywhere_enabled:true,dictation_global_hotkey:'Ctrl+Alt+Space'});
    if(url.includes('/api/dictation/capture-target'))return jsonResponse({handle:1,title:'x'});
    return jsonResponse({});
  }};
  const stop=startDictation({...electron,net},{origin:'http://127.0.0.1:7000',pollMs:5});
  await new Promise(r=>setTimeout(r,20));
  electron.globalShortcut._fire('Ctrl+Alt+Space');
  await new Promise(r=>setTimeout(r,20));
  assert.equal(win.destroyed,false);
  electron.ipcMain.listeners['faustus:dictation-result']({},{ok:true,body:{text:'hola'}});
  assert.equal(win.destroyed,true);
  stop();
});

test('stop() unregisters the hotkey and tears down the recorder window', async()=>{
  const win=new FakeWindow();
  const electron=fakeElectron({windows:[win]});
  const net={fetch: async url=>{
    if(url.includes('/api/settings'))return jsonResponse({dictation_anywhere_enabled:true,dictation_global_hotkey:'Ctrl+Alt+Space'});
    if(url.includes('/api/dictation/capture-target'))return jsonResponse({handle:1,title:'x'});
    return jsonResponse({});
  }};
  const stop=startDictation({...electron,net},{origin:'http://127.0.0.1:7000',pollMs:5});
  await new Promise(r=>setTimeout(r,20));
  electron.globalShortcut._fire('Ctrl+Alt+Space');
  await new Promise(r=>setTimeout(r,20));
  stop();
  assert.deepEqual(electron.globalShortcut._registered(),[]);
  assert.equal(win.destroyed,true);
});
