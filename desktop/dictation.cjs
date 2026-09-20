const {join}=require('node:path');

// Dictate anywhere: a configurable global push-to-talk hotkey that records
// from the default mic and lands the transcript in whatever app has focus,
// not just Studio's own composer (src/dictation_paste.py +
// routes/dictation_routes.py do the actual capture/paste on the server).
//
// Electron's globalShortcut only fires once per registered accelerator and
// gives no keyup — there is no cross-platform hold-detection without a
// native low-level keyboard hook, which this app does not depend on. So the
// "push-to-talk" shortcut here is press-to-start / press-again-to-stop: one
// accelerator toggles recording, which is the closest honest equivalent
// without adding a dependency. Settings UI text says so ("press again").
//
// The actual mic capture happens in a hidden BrowserWindow loaded at the
// Faustus origin itself (desktop/dictation-recorder-preload.cjs +
// routes/dictation_routes.py's /api/dictation/recorder page) so it passes
// the same origin check every other Faustus window does and can be granted
// the "media" permission the way Studio's own mic already is — never a
// data:/file: URL, which the app's permission policy would refuse.
function startDictation({BrowserWindow,globalShortcut,ipcMain,net},{origin,pollMs=3000,onState=()=>{},logger=console}={}){
  let registered='',recorderWindow=null,recording=false,polling=true,capturedTarget=null;

  async function fetchSettings(){
    try{
      const response=await net.fetch(origin+'/api/settings');
      if(!response.ok)return null;
      return await response.json();
    }catch(error){logger.error?.('Faustus dictation: could not read settings:',error.message);return null;}
  }

  async function captureTarget(){
    const response=await net.fetch(origin+'/api/dictation/capture-target',{method:'POST'});
    if(!response.ok)throw new Error('capture-target failed: '+response.status);
    return response.json();
  }

  function destroyRecorderWindow(){
    if(recorderWindow&&!recorderWindow.isDestroyed())recorderWindow.destroy();
    recorderWindow=null;
  }

  async function ensureRecorderWindow(){
    if(recorderWindow&&!recorderWindow.isDestroyed())return recorderWindow;
    recorderWindow=new BrowserWindow({show:false,width:1,height:1,skipTaskbar:true,webPreferences:{preload:join(__dirname,'dictation-recorder-preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true}});
    await recorderWindow.loadURL(origin+'/api/dictation/recorder');
    return recorderWindow;
  }

  async function startRecording(){
    try{
      capturedTarget=await captureTarget();
    }catch(error){
      logger.error?.('Faustus dictation: could not capture the foreground window:',error.message);
      onState({recording:false,error:error.message});
      return;
    }
    const settings=await fetchSettings();
    const method=settings&&settings.dictation_paste_method||'clipboard';
    const window=await ensureRecorderWindow();
    if(window.isDestroyed())return;
    window.webContents.send('faustus:dictation-start',capturedTarget,method);
    recording=true;
    onState({recording:true,target:capturedTarget});
  }

  function stopRecording(){
    recording=false;
    if(recorderWindow&&!recorderWindow.isDestroyed())recorderWindow.webContents.send('faustus:dictation-stop');
    onState({recording:false,target:capturedTarget});
  }

  function onHotkey(){
    if(recording)stopRecording();else void startRecording();
  }

  function resultListener(_event,result){
    // Best-effort feedback only (tray tooltip, logs) — the paste already
    // happened server-side by the time this arrives.
    destroyRecorderWindow();
    if(!result||!result.ok)logger.error?.('Faustus dictation failed:',result&&result.body&&result.body.detail||'unknown error');
    onState({recording:false,result});
  }
  ipcMain.on('faustus:dictation-result',resultListener);

  function apply(settings){
    const enabled=Boolean(settings&&settings.dictation_anywhere_enabled);
    const hotkey=String((settings&&settings.dictation_global_hotkey)||'').trim();
    const wanted=enabled&&hotkey?hotkey:'';
    if(wanted===registered)return;
    if(registered){globalShortcut.unregister(registered);registered='';if(recording)stopRecording();}
    if(wanted){
      const ok=globalShortcut.register(wanted,onHotkey);
      registered=ok?wanted:'';
      if(!ok)logger.error?.(`Faustus dictation: could not register hotkey ${wanted}`);
    }
  }

  async function tick(){
    if(!polling)return;
    const settings=await fetchSettings();
    if(settings)apply(settings);
  }

  void tick();
  const timer=setInterval(()=>void tick(),pollMs);
  return function stop(){
    polling=false;clearInterval(timer);
    if(registered){globalShortcut.unregister(registered);registered='';}
    destroyRecorderWindow();
    ipcMain.removeListener('faustus:dictation-result',resultListener);
  };
}
module.exports={startDictation};
