const {app,BrowserWindow,dialog,shell,session,ipcMain,net,Tray,Menu,nativeImage,Notification}=require('electron');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {join,resolve}=require('node:path');
const {existsSync,mkdirSync,appendFileSync,statSync,truncateSync,watchFile,unwatchFile}=require('node:fs');
const {localNavigation,localOpenTarget,externalNavigation,permissionCheck,permissionRequest,deepLinkPath,findDeepLink}=require('./policy.cjs');
const {readPlacement,resolveTarget}=require('./target.cjs');
const execute=promisify(execFile),root=resolve(__dirname,'..');
const python=join(root,'venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const port=Number(process.env.FAUSTUS_PORT||7000);
// Which Faustus this window attaches to: the one on this PC, or the Spark the
// Hoard Hub moved it to (target.cjs reads the same marker the launcher obeys).
// `origin` follows it when Faustus moves while the app is open.
const findPlace=()=>resolveTarget({env:process.env,placement:readPlacement(root),port});
let place=findPlace(),origin=place.origin,fixedTitle='';
let mainWindow,tray=null,turnTray=null,uiLang='es',ownedToken='',quitting=false,startup=null,stopDesktopControl=()=>{},stopDictation=()=>{};
let quickQuestion=null;
// A faustus:// link waiting for the app to load, and whether it has.
let pendingLink=findDeepLink(process.argv),appLoaded=false;
// Closing the window parks Faustus in the tray (hidden icons). Quitting from
// the tray stops its server and releases the local models it uses.
const trayEnabled=!process.argv.includes('--smoke-test')&&!process.argv.includes('--no-tray');

const alive=w=>w&&!w.isDestroyed();
const liveContents=c=>c&&!c.isDestroyed();
const splash='data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><title>Faustus</title><body style="background:#17191d;color:#eee;font:18px system-ui;margin:0"><header style="height:40px;display:flex;background:#121418"><span style="-webkit-app-region:drag;flex:1;padding:8px 18px;color:#e06c75">Faustus</span><button aria-label="Close / Cerrar" onclick="window.faustusWindow.command('close')" style="background:transparent;border:0;color:inherit;padding:0 20px">×</button></header><main style="padding:48px"><h1>Faustus</h1><p>Starting your local workspace… / Iniciando tu espacio local…</p><p>You can close this window to cancel. / Puedes cerrar esta ventana para cancelar.</p></main></body>`);
app.setName('Faustus');
// Its own identity on the Windows taskbar: the window (and a pinned shortcut) group under Faustus's logo, not Electron's.
if(process.platform==='win32')app.setAppUserModelId('faustus.desktop');
// Per-checkout cookies stay separate from browsers and from other installations.
// FAUSTUS_DESKTOP_PROFILE names another folder: a second window with its own session and its own single-instance lock.
const profile=process.env.FAUSTUS_DESKTOP_PROFILE?resolve(process.env.FAUSTUS_DESKTOP_PROFILE):join(root,'data','desktop-profile');mkdirSync(profile,{recursive:true});
app.setPath('userData',profile);app.setPath('sessionData',profile);
// What the tray did (turns followed, alerts shown, voice played): logs/desktop-tray.log, kept under 1 MB.
const trayLogFile=join(root,'logs','desktop-tray.log');
try{mkdirSync(join(root,'logs'),{recursive:true});if(statSync(trayLogFile).size>1024*1024)truncateSync(trayLogFile,0);}catch{/* no log yet */}
const trayLog=line=>{try{appendFileSync(trayLogFile,new Date().toISOString()+' '+line+'\n');}catch{/* never worth a crash */}};
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
    const local=localOpenTarget(url,origin);
    if(local==='page'){if(alive(window))void window.loadURL(url);return {action:'deny'};}
    if(local==='file'){void shell.openExternal(url);return {action:'deny'};}
    if(externalNavigation(url))void shell.openExternal(url);
    return {action:'deny'};
  });
  // Ctrl+R / F5 reload the page (a new build of the interface), F12 opens the developer tools.
  window.webContents.on('before-input-event',(event,input)=>{
    if(input.type!=='keyDown')return;
    const key=String(input.key||'').toLowerCase();
    if(key==='f5'||(key==='r'&&(input.control||input.meta)&&!input.alt)){event.preventDefault();window.webContents.reload();}
    else if(key==='f12'){event.preventDefault();window.webContents.toggleDevTools();}
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
function openDeepLink(path){
  if(!path)return;
  if(!appLoaded||!alive(mainWindow)){pendingLink=path;return;}
  showMainWindow();
  void mainWindow.loadURL(origin+path);
}
// A chat picked from the tray or from a "turn finished" notification. The
// interface is already loaded, so it is asked to go there itself (a route
// change: the draft in the composer stays); a full load is the fallback.
function openChat(id){
  const path='/studio?s='+encodeURIComponent(id);
  if(!appLoaded||!alive(mainWindow)){pendingLink=path;return;}
  showMainWindow();
  let onApp=false;
  try{const url=new URL(mainWindow.webContents.getURL());onApp=url.origin===origin&&!/^\/(login|setup)/.test(url.pathname);}catch{/* the splash */}
  if(!onApp){void mainWindow.loadURL(origin+path);return;}
  mainWindow.webContents.executeJavaScript(`(()=>{try{history.pushState({},'',${JSON.stringify(path)});dispatchEvent(new PopStateEvent('popstate',{state:{}}));return true;}catch{return false;}})()`,true)
    .then(done=>{if(!done&&alive(mainWindow))void mainWindow.loadURL(origin+path);})
    .catch(()=>{if(alive(mainWindow))void mainWindow.loadURL(origin+path);});
}
// Whether the owner is looking at the window, and at which chat: a turn that
// ends in front of them needs neither a notification nor an "unseen" mark.
function windowFocus(){
  if(!alive(mainWindow)||!mainWindow.isVisible()||mainWindow.isMinimized()||!mainWindow.isFocused())return {focused:false,visibleId:''};
  let visibleId='';
  try{const url=new URL(mainWindow.webContents.getURL());if(url.origin===origin&&url.pathname.startsWith('/studio'))visibleId=url.searchParams.get('s')||'';}catch{/* not a page of the app */}
  return {focused:true,visibleId};
}
function readUiLanguage(){
  if(!appLoaded||!alive(mainWindow))return;
  mainWindow.webContents.executeJavaScript('document.documentElement.lang',true).then(value=>{if(typeof value==='string'&&value)uiLang=value;}).catch(()=>{});
}
function registerProtocol(){
  if(process.argv.includes('--smoke-test'))return;
  try{
    // A development checkout runs as `electron .`: the handler has to name the app folder too.
    if(process.defaultApp&&process.argv.length>=2)app.setAsDefaultProtocolClient('faustus',process.execPath,[resolve(process.argv[1])]);
    else app.setAsDefaultProtocolClient('faustus');
  }catch(error){console.error('Faustus protocol:',error.message);}
}
// Windows shows a notification under the name and icon registered for the
// app's identity; without them it is an anonymous toast with no logo.
function registerNotificationIdentity(){
  if(process.platform!=='win32'||process.argv.includes('--smoke-test'))return;
  const key='HKCU\\Software\\Classes\\AppUserModelId\\faustus.desktop';
  for(const [name,value] of [['DisplayName','Faustus'],['IconUri',join(__dirname,'tray.ico')]])
    execFile('reg.exe',['add',key,'/v',name,'/t','REG_SZ','/d',value,'/f'],{windowsHide:true},error=>{if(error)trayLog('notification identity: '+error.message);});
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
// The tray menu: what the turns are doing (turn-tray.cjs fills that part in
// and redraws it when it changes), then the fixed entries.
function trayMenu(turnItems=[],toggles=[]){
  return Menu.buildFromTemplate([
    ...turnItems,
    ...(turnItems.length?[{type:'separator'}]:[]),
    {label:'Abrir Faustus / Open',click:()=>showMainWindow()},
    ...toggles,
    {type:'separator'},
    {label:'Salir / Quit',click:()=>void quitFromTray()},
  ]);
}
function createTray(){
  const iconPath=join(__dirname,process.platform==='win32'?'tray.ico':'tray.png');
  let image=nativeImage.createFromPath(iconPath);
  if(image.isEmpty())image=nativeImage.createEmpty();
  tray=new Tray(image);
  tray.setToolTip('Faustus');
  tray.setContextMenu(trayMenu());
  tray.on('click',()=>{if(alive(mainWindow)&&mainWindow.isVisible()&&!mainWindow.isMinimized())mainWindow.focus();else showMainWindow();});
  tray.on('double-click',()=>showMainWindow());
  // The icon says what the turns are doing, its menu lists them, and a long
  // turn that ends out of sight gets a notification (and a spoken line).
  turnTray=require('./turn-tray.cjs').startTurnTray({net,Notification,nativeImage,BrowserWindow,ipcMain},{
    tray,origin:()=>origin,focus:windowFocus,openChat,showMain:showMainWindow,iconPath,lang:()=>uiLang,log:trayLog,
    stateFile:join(profile,'turn-status.json'),
    rebuildMenu:(items,toggles)=>tray.setContextMenu(trayMenu(items,toggles)),
    setOverlay:(overlay,description)=>{if(process.platform==='win32'&&alive(mainWindow))mainWindow.setOverlayIcon(overlay,description||'');},
  });
}
// A message sent from the window, or its answer closing, is a turn starting
// or ending: the tray looks at once instead of waiting for its next round.
function watchChatRequests(){
  if(!turnTray)return;
  const look=details=>{if(details.method==='POST'&&turnTray)turnTray.kick(500);};
  try{
    const filter={urls:[origin+'/api/chat',origin+'/api/chat/stop*',origin+'/api/chat_stream*']};
    session.defaultSession.webRequest.onSendHeaders(filter,look);
    session.defaultSession.webRequest.onCompleted(filter,look);
    session.defaultSession.webRequest.onErrorOccurred(filter,look);
  }catch(error){trayLog('chat requests not watched: '+error.message);}
}
async function shutdown(){
  if(quitting)return;quitting=true;stopDesktopControl();stopDictation();
  quickQuestion?.stop();
  try{unwatchFile(join(root,'data','runtime','placement.json'));}catch{/* never watched */}
  try{turnTray?.stop();}catch{/* already stopped */}
  try{await startup;}catch{/* A failed page load must not skip server cleanup. */}
  try{if(ownedToken)await runtime(['stop','--token',ownedToken]);}
  catch(error){console.error('Faustus shutdown:',error.message);}
  try{
    if(ownedToken&&process.platform==='win32')await execute('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',join(root,'Stop-Local-Models.ps1')],{cwd:root,windowsHide:true,timeout:45000});
  }catch(error){console.error('Faustus model shutdown:',error.message);}
  finally{try{tray?.destroy();}catch{/* already gone */}app.exit(0);}
}
// A Faustus on another machine may still be coming up (the Hub has just moved it): wait for it behind the splash.
async function waitForRemote(timeoutMs=90000){
  const deadline=Date.now()+timeoutMs;
  for(;;){
    try{
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
      const response=await net.fetch(origin+'/api/health',{signal:controller.signal});clearTimeout(timer);
      if(response.status<500)return true;
    }catch{/* not up yet */}
    if(Date.now()>=deadline||quitting)return false;
    await new Promise(done=>setTimeout(done,1500));
  }
}
// Starts (or joins) the Faustus of this PC, or joins the one the Hub moved to a Spark, and loads the interface.
async function attach(){
  place=findPlace();origin=place.origin;let started=false;
  if(place.remote){
    ownedToken='';
    if(!await waitForRemote())throw new Error(`Faustus does not answer at ${origin}. / Faustus no responde en ${origin}.`);
  }else{
    if(!existsSync(python))throw new Error('Run Start-Faustus-Desktop.bat to install this checkout first.');
    const result=await runtime(['start','--port',String(port),'--owner','desktop']);
    if(result.started)ownedToken=result.token;
    if(!result.healthy)throw new Error('The shared Faustus server is not ready. Check logs/.');
    started=result.started;
  }
  if(mainWindow.isDestroyed()||quitting)return;
  fixedTitle=place.remote?(place.where?`Faustus — ${place.where}`:'Faustus'):started?'':'Faustus — shared server / servidor compartido';
  if(fixedTitle)mainWindow.setTitle(fixedTitle);
  trayLog(`attached to ${origin}${place.where?' ('+place.where+')':''}${started?', server started here':''}`);
  watchChatRequests();
  await mainWindow.loadURL(origin+'/studio');
  appLoaded=true;
  if(pendingLink){const path=pendingLink;pendingLink=null;openDeepLink(path);}
  if(process.argv.includes('--smoke-test')){
    console.log('FAUSTUS_DESKTOP_LOADED '+(started?'owned':'shared'));
    await require('./smoke.cjs').run(mainWindow,root);
  }
}
// The Hub moved Faustus (this PC <-> a Spark) with the app open: follow it.
async function followPlacement(){
  if(quitting||!appLoaded||findPlace().origin===origin||!alive(mainWindow))return;
  appLoaded=false;
  try{
    await mainWindow.loadURL(splash);
    startup=attach();await startup;
    stopDictation();
    if(process.platform==='win32')stopDictation=require('./dictation.cjs').startDictation(require('electron'),{origin});
    turnTray?.retarget();
    quickQuestion?.retarget();
  }catch(error){trayLog('could not follow Faustus: '+error.message);if(!quitting)dialog.showErrorBox('Faustus',error.message);}
}
if(!Number.isInteger(port)||port<1024||port>65535){app.quit();}
else if(!app.requestSingleInstanceLock()){app.quit();}
else{
  app.on('second-instance',(_event,argv)=>{if(!alive(mainWindow)){if(!quitting)void shutdown();return;}showMainWindow();openDeepLink(findDeepLink(argv));});
  app.on('open-url',(event,url)=>{event.preventDefault();openDeepLink(deepLinkPath(url));});
  registerProtocol();
  registerNotificationIdentity();
  app.on('before-quit',event=>{if(!quitting){event.preventDefault();void shutdown();}});
  app.on('window-all-closed',()=>void shutdown());
  app.whenReady().then(async()=>{
    stopDesktopControl=require('./desktop-control.cjs').startDesktopControl(require('electron'),root);
    // Windows-only feature; dictation.cjs itself only ever talks to the
    // server's /api/dictation/* routes, which 501 cleanly off Windows, so
    // this is left running everywhere and simply never registers a hotkey
    // when the settings poll reports it unsupported/disabled.
    if(process.platform==='win32')stopDictation=require('./dictation.cjs').startDictation(require('electron'),{origin});
    if(!place.remote&&!existsSync(python)){dialog.showErrorBox('Faustus','Run Start-Faustus-Desktop.bat to install this checkout first.');app.quit();return;}
    const allowed=new Set();
    session.defaultSession.setPermissionCheckHandler((contents,permission,requestingOrigin)=>{
      const from=requestingOrigin||(liveContents(contents)&&contents.getURL())||'';
      return permissionCheck(permission,from,origin,allowed,(liveContents(contents)&&contents.getURL())||'');
    });
    session.defaultSession.setPermissionRequestHandler(async(contents,permission,callback,details)=>{
      const page=(liveContents(contents)&&contents.getURL())||'';
      const decision=permissionRequest(permission,page,origin,details&&details.requestingUrl);
      if(decision==='allow'){callback(true);return;}
      if(decision!=='prompt'||!alive(mainWindow)){callback(false);return;}
      const response=await dialog.showMessageBox(mainWindow,{type:'question',title:'Faustus',message:`Allow ${permission} / ¿Permitir ${permission}?`,detail:'Only for this Faustus window. / Sólo para esta ventana de Faustus.',buttons:['Allow / Permitir','Deny / Denegar'],defaultId:1,cancelId:1});
      if(response.response===0)allowed.add(permission);callback(response.response===0);
    });
    mainWindow=new BrowserWindow({title:'Faustus',icon:join(__dirname,process.platform==='win32'?'tray.ico':'tray.png'),frame:false,width:1400,height:920,minWidth:390,minHeight:600,show:true,autoHideMenuBar:true,backgroundColor:'#17191d',webPreferences:{preload:join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true}});
    ipcMain.handle('faustus:window',async(event,action)=>{
      const target=BrowserWindow.fromWebContents(event.sender);
      if(!target||event.senderFrame!==target.webContents.mainFrame||!(localNavigation(event.senderFrame.url,origin)||(target===mainWindow&&event.senderFrame.url===splash&&action==='close')))throw new Error('Untrusted window');
      // Settings > This device asks what the tray says ('alerts-state') and for a test alert ('alerts-test'):
      // two more fixed words on this channel, answered next to the window state.
      if(action==='alerts-state'||action==='alerts-test'){
        if(!turnTray)return {...windowState(target),alerts:{available:false}};
        const test=action==='alerts-test'?await turnTray.test():null,view=turnTray.view();
        return {...windowState(target),alerts:{available:true,tooltip:view.tooltip,link:view.link,where:place.where,test}};
      }
      if(action==='quick-state')return {...windowState(target),quick:quickQuestion?.view()||{registered:'',error:''}};
      if(action==='quick-open'){quickQuestion?.show();return windowState(target);}
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
    mainWindow.on('page-title-updated',event=>{if(fixedTitle)event.preventDefault();});
    // The chat on screen counts as seen; the alerts speak the language of the interface.
    mainWindow.on('focus',()=>{readUiLanguage();turnTray?.touch();});
    for(const moved of ['did-navigate','did-navigate-in-page'])mainWindow.webContents.on(moved,()=>turnTray?.touch());
    mainWindow.webContents.on('did-finish-load',()=>setTimeout(readUiLanguage,1500));
    if(trayEnabled)createTray();
    // The native bridge accepts only window controls from the main local page.
    await mainWindow.loadURL(splash);
    startup=attach();
    try{await startup;}catch(error){console.error(error.stack||error.message);if(!quitting&&!process.argv.includes('--smoke-test'))dialog.showErrorBox('Faustus',error.message);app.quit();return;}
    quickQuestion=require('./quick-question.cjs').startQuickQuestion(require('electron'),{origin:()=>origin,profile,secureWindow,openChat,showMain:showMainWindow});
    if(!process.env.FAUSTUS_DESKTOP_URL)watchFile(join(root,'data','runtime','placement.json'),{interval:5000},()=>void followPlacement());
  }).catch(error=>{dialog.showErrorBox('Faustus',error.message);app.quit();});
}
