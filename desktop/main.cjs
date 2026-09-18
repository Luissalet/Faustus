const {app,BrowserWindow,dialog,shell,session,ipcMain,net,Tray,Menu,nativeImage}=require('electron');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {join,resolve}=require('node:path');
const {existsSync,mkdirSync}=require('node:fs');
const {localNavigation,externalNavigation,permissionCheck,permissionRequest}=require('./policy.cjs');
const execute=promisify(execFile),root=resolve(__dirname,'..');
const python=join(root,'venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const port=Number(process.env.FAUSTUS_PORT||7000),origin=`http://127.0.0.1:${port}`;
let mainWindow,tray=null,ownedToken='',quitting=false,startup=null,stopDesktopControl=()=>{};
// Closing the window parks Faustus in the tray (hidden icons), like the chat
// desktop apps do; the tray menu's Quit is what really stops it. The smoke
// test keeps the old close-means-quit path so it can finish on its own.
const trayEnabled=!process.argv.includes('--smoke-test')&&!process.argv.includes('--no-tray');

const alive=w=>w&&!w.isDestroyed();
const liveContents=c=>c&&!c.isDestroyed();
const splash='data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><title>Faustus</title><body style="background:#17191d;color:#eee;font:18px system-ui;margin:0"><header style="height:40px;display:flex;background:#121418"><span style="-webkit-app-region:drag;flex:1;padding:8px 18px;color:#e06c75">Faustus</span><button aria-label="Close / Cerrar" onclick="window.faustusWindow.command('close')" style="background:transparent;border:0;color:inherit;padding:0 20px">×</button></header><main style="padding:48px"><h1>Faustus</h1><p>Starting your local workspace… / Iniciando tu espacio local…</p><p>You can close this window to cancel. / Puedes cerrar esta ventana para cancelar.</p></main></body>`);
app.setName('Faustus');
// Its own identity on the Windows taskbar: the window (and a pinned shortcut) group under Faustus's logo, not Electron's.
if(process.platform==='win32')app.setAppUserModelId('faustus.desktop');
// Per-checkout cookies stay separate from browsers and from other installations.
const profile=join(root,'data','desktop-profile');mkdirSync(profile,{recursive:true});
app.setPath('userData',profile);app.setPath('sessionData',profile);
const runtime=async(args)=>{
  const {stdout}=await execute(python,[join(root,'server_runtime.py'),...args],{cwd:root,windowsHide:true,timeout:150000,maxBuffer:1024*1024});
  const result=JSON.parse(stdout.trim());if(result.error)throw new Error(result.error);return result;
};
const windowState=window=>({maximized:alive(window)&&window.isMaximized(),fullscreen:alive(window)&&window.isFullScreen()});
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
async function closeConfirmed(target,senderUrl){
  // No questions, ever (the owner's rule): the X parks the window in the
  // tray; Quit from the tray closes the window and stops the server this
  // window started. A window on a shared server just closes and leaves the
  // server to the others.
  if(target!==mainWindow||senderUrl===splash||process.argv.includes('--smoke-test'))return 'close';
  return ownedToken?'stop':'close';
}
function secureWindow(window){
  for(const event of ['maximize','unmaximize','enter-full-screen','leave-full-screen'])window.on(event,()=>{if(!alive(window))return;window.webContents.send('faustus:window-state',windowState(window));});
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
function showMainWindow(){
  if(!alive(mainWindow))return;
  if(!mainWindow.isVisible())mainWindow.show();
  if(mainWindow.isMinimized())mainWindow.restore();
  mainWindow.focus();
}
function parkInTray(){
  if(!alive(mainWindow))return;
  // Silently: no balloon, no hint. The tray icon is the whole message.
  mainWindow.hide();
}
async function quitFromTray(){
  // Quit means quit: close the window and stop the server this window
  // started, without asking (the owner's rule: "always yes, no prompts").
  if(quitting)return;
  void shutdown();
}
function createTray(){
  const iconPath=join(__dirname,process.platform==='win32'?'tray.ico':'tray.png');
  let image=nativeImage.createFromPath(iconPath);
  if(image.isEmpty())image=nativeImage.createEmpty();
  tray=new Tray(image);
  tray.setToolTip('Faustus');
  tray.setContextMenu(Menu.buildFromTemplate([
    {label:'Abrir Faustus / Open',click:()=>showMainWindow()},
    {type:'separator'},
    {label:'Salir / Quit',click:()=>void quitFromTray()},
  ]));
  tray.on('click',()=>{if(alive(mainWindow)&&mainWindow.isVisible()&&!mainWindow.isMinimized())mainWindow.focus();else showMainWindow();});
  tray.on('double-click',()=>showMainWindow());
}
async function shutdown(){
  if(quitting)return;quitting=true;stopDesktopControl();
  try{await startup;}catch{/* A failed page load must not skip server cleanup. */}
  try{if(ownedToken)await runtime(['stop','--token',ownedToken]);}
  catch(error){console.error('Faustus shutdown:',error.message);}
  finally{try{tray?.destroy();}catch{/* already gone */}app.exit(0);}
}
if(!Number.isInteger(port)||port<1024||port>65535){app.quit();}
else if(!app.requestSingleInstanceLock()){app.quit();}
else{
  app.on('second-instance',()=>{if(!alive(mainWindow)){if(!quitting)void shutdown();return;}showMainWindow();});
  app.on('before-quit',event=>{if(!quitting){event.preventDefault();void shutdown();}});
  app.on('window-all-closed',()=>void shutdown());
  app.whenReady().then(async()=>{
    stopDesktopControl=require('./desktop-control.cjs').startDesktopControl(require('electron'),root);
    if(!existsSync(python)){dialog.showErrorBox('Faustus','Run Start-Faustus-Desktop.bat to install this checkout first.');app.quit();return;}
    const allowed=new Set();
    session.defaultSession.setPermissionCheckHandler((contents,permission,requestingOrigin)=>{
      const from=requestingOrigin||(liveContents(contents)&&contents.getURL())||'';
      return permissionCheck(permission,from,origin,allowed);
    });
    session.defaultSession.setPermissionRequestHandler(async(contents,permission,callback)=>{
      const page=(liveContents(contents)&&contents.getURL())||'';
      const decision=permissionRequest(permission,page,origin);
      if(decision==='allow'){callback(true);return;}
      if(decision!=='prompt'||!alive(mainWindow)){callback(false);return;}
      const response=await dialog.showMessageBox(mainWindow,{type:'question',title:'Faustus',message:`Allow ${permission} / ¿Permitir ${permission}?`,detail:'Only for this Faustus window. / Sólo para esta ventana de Faustus.',buttons:['Allow / Permitir','Deny / Denegar'],defaultId:1,cancelId:1});
      if(response.response===0)allowed.add(permission);callback(response.response===0);
    });
    mainWindow=new BrowserWindow({title:'Faustus',icon:join(__dirname,process.platform==='win32'?'tray.ico':'tray.png'),frame:false,width:1400,height:920,minWidth:390,minHeight:600,show:true,autoHideMenuBar:true,backgroundColor:'#17191d',webPreferences:{preload:join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true}});
    ipcMain.handle('faustus:window',async(event,action)=>{
      const target=BrowserWindow.fromWebContents(event.sender);
      if(!target||event.senderFrame!==target.webContents.mainFrame||!(localNavigation(event.senderFrame.url,origin)||(target===mainWindow&&event.senderFrame.url===splash&&action==='close')))throw new Error('Untrusted window');
      if(!['state','minimize','maximize','fullscreen','close'].includes(action))throw new Error('Unknown window action');
      if(action==='minimize')target.minimize();
      if(action==='maximize'){if(target.isFullScreen())target.setFullScreen(false);if(target.isMaximized())target.unmaximize();else target.maximize();}
      if(action==='fullscreen')target.setFullScreen(!target.isFullScreen());
      let closeDecision='close';
      if(action==='close'&&trayEnabled&&target===mainWindow&&event.senderFrame.url!==splash&&!quitting){
        parkInTray();return windowState(target);
      }
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
    mainWindow.on('close',event=>{if(trayEnabled&&!quitting){event.preventDefault();parkInTray();}});
    mainWindow.on('closed',()=>void shutdown());
    if(trayEnabled)createTray();
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
