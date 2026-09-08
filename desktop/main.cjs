const {app,BrowserWindow,dialog,shell,session,ipcMain}=require('electron');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {join,resolve}=require('node:path');
const {existsSync,mkdirSync}=require('node:fs');
const {localNavigation,externalNavigation}=require('./policy.cjs');
const execute=promisify(execFile),root=resolve(__dirname,'..');
const python=join(root,'venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const port=Number(process.env.FAUSTUS_PORT||7000),origin=`http://127.0.0.1:${port}`;
let mainWindow,ownedToken='',quitting=false,startup=null;
const splash='data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><title>Faustus</title><body style="background:#17191d;color:#eee;font:18px system-ui;margin:0"><header style="height:40px;display:flex;background:#121418"><span style="-webkit-app-region:drag;flex:1;padding:8px 18px;color:#e06c75">Faustus</span><button aria-label="Close / Cerrar" onclick="window.faustusWindow.command('close')" style="background:transparent;border:0;color:inherit;padding:0 20px">×</button></header><main style="padding:48px"><h1>Faustus</h1><p>Starting your local workspace… / Iniciando tu espacio local…</p><p>You can close this window to cancel. / Puedes cerrar esta ventana para cancelar.</p></main></body>`);
app.setName('Faustus');
// Per-checkout cookies stay separate from browsers and from other installations.
const profile=join(root,'data','desktop-profile');mkdirSync(profile,{recursive:true});
app.setPath('userData',profile);app.setPath('sessionData',profile);
const runtime=async(args)=>{
  const {stdout}=await execute(python,[join(root,'server_runtime.py'),...args],{cwd:root,windowsHide:true,timeout:150000,maxBuffer:1024*1024});
  const result=JSON.parse(stdout.trim());if(result.error)throw new Error(result.error);return result;
};
function secureWindow(window){
  window.webContents.setWindowOpenHandler(({url})=>{
    if(localNavigation(url,origin))return {action:'allow',overrideBrowserWindowOptions:{autoHideMenuBar:true,webPreferences:{nodeIntegration:false,contextIsolation:true,sandbox:true}}};
    if(externalNavigation(url))void shell.openExternal(url);
    return {action:'deny'};
  });
  window.webContents.on('will-navigate',(event,url)=>{
    if(!localNavigation(url,origin)){event.preventDefault();if(externalNavigation(url))void shell.openExternal(url);}
  });
  window.webContents.on('did-create-window',secureWindow);
}
async function shutdown(){
  if(quitting)return;quitting=true;
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
    if(!existsSync(python)){dialog.showErrorBox('Faustus','Run Start-Faustus-Desktop.bat to install this checkout first.');app.quit();return;}
    const allowed=new Set();
    session.defaultSession.setPermissionCheckHandler((contents,permission,requestingOrigin)=>localNavigation(requestingOrigin,origin)&&allowed.has(permission));
    session.defaultSession.setPermissionRequestHandler(async(contents,permission,callback)=>{
      if(!localNavigation(contents.getURL(),origin)||!['media','notifications','clipboard-read'].includes(permission)){callback(false);return;}
      const response=await dialog.showMessageBox(mainWindow,{type:'question',title:'Faustus',message:`Allow ${permission} / ¿Permitir ${permission}?`,detail:'Only for this Faustus window. / Sólo para esta ventana de Faustus.',buttons:['Allow / Permitir','Deny / Denegar'],defaultId:1,cancelId:1});
      if(response.response===0)allowed.add(permission);callback(response.response===0);
    });
    mainWindow=new BrowserWindow({title:'Faustus',frame:false,width:1400,height:920,minWidth:390,minHeight:600,show:true,autoHideMenuBar:true,backgroundColor:'#17191d',webPreferences:{preload:join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true}});
    const windowState=()=>({maximized:mainWindow.isMaximized(),fullscreen:mainWindow.isFullScreen()});
    ipcMain.handle('faustus:window',(event,action)=>{
      if(event.sender!==mainWindow.webContents||event.senderFrame!==mainWindow.webContents.mainFrame||!(localNavigation(event.senderFrame.url,origin)||(event.senderFrame.url===splash&&action==='close')))throw new Error('Untrusted window');
      if(!['state','minimize','maximize','fullscreen','close'].includes(action))throw new Error('Unknown window action');
      if(action==='minimize')mainWindow.minimize();
      if(action==='maximize'){if(mainWindow.isFullScreen())mainWindow.setFullScreen(false);if(mainWindow.isMaximized())mainWindow.unmaximize();else mainWindow.maximize();}
      if(action==='fullscreen')mainWindow.setFullScreen(!mainWindow.isFullScreen());
      const state=windowState();
      if(action==='close')setImmediate(()=>mainWindow.close());
      return state;
    });
    for(const event of ['maximize','unmaximize','enter-full-screen','leave-full-screen'])mainWindow.on(event,()=>mainWindow.webContents.send('faustus:window-state',windowState()));
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
