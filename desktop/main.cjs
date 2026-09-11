const {app,BrowserWindow,dialog,shell,session,ipcMain,net}=require('electron');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {join,resolve}=require('node:path');
const {existsSync,mkdirSync}=require('node:fs');
const {localNavigation,externalNavigation}=require('./policy.cjs');
const execute=promisify(execFile),root=resolve(__dirname,'..');
const python=join(root,'venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const port=Number(process.env.FAUSTUS_PORT||7000),origin=`http://127.0.0.1:${port}`;
let mainWindow,ownedToken='',quitting=false,startup=null,stopDesktopControl=()=>{};
const splash='data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><title>Faustus</title><body style="background:#17191d;color:#eee;font:18px system-ui;margin:0"><header style="height:40px;display:flex;background:#121418"><span style="-webkit-app-region:drag;flex:1;padding:8px 18px;color:#e06c75">Faustus</span><button aria-label="Close / Cerrar" onclick="window.faustusWindow.command('close')" style="background:transparent;border:0;color:inherit;padding:0 20px">×</button></header><main style="padding:48px"><h1>Faustus</h1><p>Starting your local workspace… / Iniciando tu espacio local…</p><p>You can close this window to cancel. / Puedes cerrar esta ventana para cancelar.</p></main></body>`);
app.setName('Faustus');
// Per-checkout cookies stay separate from browsers and from other installations.
const profile=join(root,'data','desktop-profile');mkdirSync(profile,{recursive:true});
app.setPath('userData',profile);app.setPath('sessionData',profile);
const runtime=async(args)=>{
  const {stdout}=await execute(python,[join(root,'server_runtime.py'),...args],{cwd:root,windowsHide:true,timeout:150000,maxBuffer:1024*1024});
  const result=JSON.parse(stdout.trim());if(result.error)throw new Error(result.error);return result;
};
const windowState=window=>({maximized:window.isMaximized(),fullscreen:window.isFullScreen()});
// ACT-04: closing the app window is not the same thing as stopping the
// server, and the two must never be conflated silently. `ownedToken` is only
// set when THIS instance started the server (see server_runtime.py 'start'
// and src/process_ownership.py: a process is only ours to stop if we hold
// the object that started it) - so it is exactly the fact the confirmation
// needs: whether closing the window also ends the turn running server-side,
// or leaves it running for whoever else is attached. Skipped for: secondary
// (non-main) windows, which never own the server and whose close cannot
// trigger shutdown(); the splash screen's own cancel button, which the
// splash text already documents as "closes to cancel starting up"; and the
// automated smoke test, which has no dialog to click.
//
// Best-effort count of what a stop would interrupt: GET /api/queue (agent
// runs, background jobs, research, media runs still in flight - the same
// admin-only endpoint Studio's queue panel reads). `net.fetch` carries the
// default session's cookies for this same-origin request the way a page
// load does, so it succeeds under normal auth exactly when the running
// server would already let this window in; any failure (auth off in a way
// that still 403s a bare fetch, server mid-restart, timeout) degrades to an
// unknown count rather than blocking the close dialog on it.
async function activeTaskCount(){
  try{
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),2000);
    const response=await net.fetch(origin+'/api/queue',{signal:controller.signal});
    clearTimeout(timer);
    if(!response.ok)return null;
    const body=await response.json();
    return Array.isArray(body.items)?body.items.length:null;
  }catch{return null;}
}
async function closeConfirmed(target,senderUrl){
  if(target!==mainWindow||senderUrl===splash||process.argv.includes('--smoke-test'))return 'close';
  const owned=!!ownedToken;
  if(!owned){
    const {response}=await dialog.showMessageBox(target,{
      type:'question',title:'Faustus',
      message:'Close window? / ¿Cerrar la ventana?',
      detail:'This window uses a server shared with other Faustus windows. Closing it does not stop that server: the turn keeps running there. / Esta ventana usa un servidor compartido con otras ventanas de Faustus. Cerrarla no lo detiene: el turno sigue en el servidor.',
      buttons:['Cancel / Cancelar','Close / Cerrar'],
      defaultId:1,cancelId:0,
    });
    return response===1?'close':'cancel';
  }
  const count=await activeTaskCount();
  const countEn=count===null?'it may still have work in progress (could not check)'
    :count>0?`this stops ${count} task${count===1?'':'s'} still in progress`
    :'nothing is in progress on it right now';
  const countEs=count===null?'puede seguir con trabajo en marcha (no se pudo comprobar)'
    :count>0?`esto detiene ${count} tarea${count===1?'':'s'} en marcha`
    :'no hay nada en marcha en él ahora mismo';
  const {response}=await dialog.showMessageBox(target,{
    type:'warning',
    title:'Faustus',
    message:'Close window? / ¿Cerrar la ventana?',
    detail:`This window started its own local server; ${countEn}. Keep the server running to leave that work alone. / Esta ventana inició su propio servidor local; ${countEs}. Mantén el servidor en marcha para no interrumpir ese trabajo.`,
    buttons:['Cancel / Cancelar','Keep the server running / Mantener el servidor','Close and stop the server / Cerrar y detener el servidor'],
    defaultId:0,
    cancelId:0,
  });
  return response===2?'stop':response===1?'keep':'cancel';
}
function secureWindow(window){
  for(const event of ['maximize','unmaximize','enter-full-screen','leave-full-screen'])window.on(event,()=>window.webContents.send('faustus:window-state',windowState(window)));
  window.webContents.setWindowOpenHandler(({url})=>{
    if(localNavigation(url,origin))return {action:'allow',overrideBrowserWindowOptions:{frame:false,autoHideMenuBar:true,webPreferences:{preload:join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true}}};
    if(externalNavigation(url))void shell.openExternal(url);
    return {action:'deny'};
  });
  window.webContents.on('will-navigate',(event,url)=>{
    if(!localNavigation(url,origin)){event.preventDefault();if(externalNavigation(url))void shell.openExternal(url);}
  });
  window.webContents.on('did-create-window',secureWindow);
}
async function shutdown(){
  if(quitting)return;quitting=true;stopDesktopControl();
  try{await startup;}catch{/* A failed page load must not skip server cleanup. */}
  try{if(ownedToken)await runtime(['stop','--token',ownedToken]);}
  catch(error){console.error('Faustus shutdown:',error.message);}
  finally{app.exit(0);}
}
if(!Number.isInteger(port)||port<1024||port>65535){app.quit();}
else if(!app.requestSingleInstanceLock()){app.quit();}
else{
  app.on('second-instance',()=>{if(mainWindow){if(mainWindow.isMinimized())mainWindow.restore();mainWindow.show();mainWindow.focus();}});
  app.on('before-quit',event=>{if(!quitting){event.preventDefault();void shutdown();}});
  app.on('window-all-closed',()=>void shutdown());
  app.whenReady().then(async()=>{
    stopDesktopControl=require('./desktop-control.cjs').startDesktopControl(require('electron'),root);
    if(!existsSync(python)){dialog.showErrorBox('Faustus','Run Start-Faustus-Desktop.bat to install this checkout first.');app.quit();return;}
    const allowed=new Set();
    session.defaultSession.setPermissionCheckHandler((contents,permission,requestingOrigin)=>localNavigation(requestingOrigin,origin)&&allowed.has(permission));
    session.defaultSession.setPermissionRequestHandler(async(contents,permission,callback)=>{
      if(!localNavigation(contents.getURL(),origin)||!['media','notifications','clipboard-read'].includes(permission)){callback(false);return;}
      const response=await dialog.showMessageBox(mainWindow,{type:'question',title:'Faustus',message:`Allow ${permission} / ¿Permitir ${permission}?`,detail:'Only for this Faustus window. / Sólo para esta ventana de Faustus.',buttons:['Allow / Permitir','Deny / Denegar'],defaultId:1,cancelId:1});
      if(response.response===0)allowed.add(permission);callback(response.response===0);
    });
    mainWindow=new BrowserWindow({title:'Faustus',frame:false,width:1400,height:920,minWidth:390,minHeight:600,show:true,autoHideMenuBar:true,backgroundColor:'#17191d',webPreferences:{preload:join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true}});
    ipcMain.handle('faustus:window',async(event,action)=>{
      const target=BrowserWindow.fromWebContents(event.sender);
      if(!target||event.senderFrame!==target.webContents.mainFrame||!(localNavigation(event.senderFrame.url,origin)||(target===mainWindow&&event.senderFrame.url===splash&&action==='close')))throw new Error('Untrusted window');
      if(!['state','minimize','maximize','fullscreen','close'].includes(action))throw new Error('Unknown window action');
      if(action==='minimize')target.minimize();
      if(action==='maximize'){if(target.isFullScreen())target.setFullScreen(false);if(target.isMaximized())target.unmaximize();else target.maximize();}
      if(action==='fullscreen')target.setFullScreen(!target.isFullScreen());
      let closeDecision='close';
      if(action==='close'){
        closeDecision=await closeConfirmed(target,event.senderFrame.url);
        if(closeDecision==='cancel')return windowState(target);
        // 'keep': this window's server stays up for whoever else is attached -
        // clearing ownedToken before the window closes is what makes
        // shutdown() (triggered by the 'closed' event just below) skip the
        // stop call, the same way it already does for a non-owning window.
        if(closeDecision==='keep')ownedToken='';
      }
      const state=windowState(target);
      if(action==='close')setImmediate(()=>{if(!target.isDestroyed())target.close();});
      return state;
    });
    secureWindow(mainWindow);
    mainWindow.on('closed',()=>void shutdown());
    // The native bridge accepts only window controls from the main local page.
    await mainWindow.loadURL(splash);
    startup=(async()=>{
      const result=await runtime(['start','--port',String(port),'--owner','desktop']);
      if(result.started)ownedToken=result.token;
      if(!result.healthy)throw new Error('The shared Faustus server is not ready. Check logs/.');
      if(!mainWindow.isDestroyed()&&!quitting){
        if(!result.started){mainWindow.setTitle('Faustus — shared server / servidor compartido');mainWindow.on('page-title-updated',event=>event.preventDefault());}
        await mainWindow.loadURL(origin+'/studio');
        if(process.argv.includes('--smoke-test')){
          console.log('FAUSTUS_DESKTOP_LOADED '+(result.started?'owned':'shared'));
          await require('./smoke.cjs').run(mainWindow,root);
        }
      }
    })();
    try{await startup;}catch(error){console.error(error.stack||error.message);if(!quitting&&!process.argv.includes('--smoke-test'))dialog.showErrorBox('Faustus',error.message);app.quit();}
  }).catch(error=>{dialog.showErrorBox('Faustus',error.message);app.quit();});
}
