const {readFileSync,writeFileSync,mkdirSync,renameSync}=require('node:fs');
const {join}=require('node:path');

function startDesktopControl({BrowserWindow,screen,globalShortcut},root){
  const dir=join(root,'data','runtime');mkdirSync(dir,{recursive:true});
  let token='',windows=[],busy=false,stopped='';
  const read=name=>{try{return JSON.parse(readFileSync(join(dir,name),'utf8'));}catch{return {};}};
  const write=(name,data)=>{const file=join(dir,name);writeFileSync(file+'.host-tmp',JSON.stringify(data));renameSync(file+'.host-tmp',file);};
  function clear(){
    globalShortcut.unregister('Escape');
    for(const window of windows)if(!window.isDestroyed())window.destroy();
    windows=[];token='';
  }
  async function tick(){
    if(busy)return;
    busy=true;
    try{
      const state=read('desktop-control.json');
      if(!state.token||Date.now()/1000-state.updated>5){if(token)clear();return;}
      if(state.token===token||state.token===stopped)return;
      clear();token=state.token;
      const current=token;
      if(!globalShortcut.register('Escape',()=>{
        stopped=current;write('desktop-cancel.json',{token:current});clear();
      }))throw new Error('No se pudo registrar Esc para detener el control.');
      const html='data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><style>html,body{margin:0;width:100%;height:100%;background:transparent;overflow:hidden}body{box-sizing:border-box;border:4px solid #ba7cff;box-shadow:inset 0 0 22px #ae61ffaa;font:14px system-ui}span{position:absolute;top:4px;left:50%;transform:translateX(-50%);padding:7px 18px;border-radius:0 0 10px 10px;background:#271238;color:white;white-space:nowrap}</style><span>Faustus controla la pantalla · Esc para detener</span>`);
      await Promise.all(screen.getAllDisplays().map(async display=>{
        const window=new BrowserWindow({...display.bounds,frame:false,transparent:true,focusable:false,alwaysOnTop:true,skipTaskbar:true,show:false,resizable:false,hasShadow:false,enableLargerThanScreen:true,webPreferences:{nodeIntegration:false,contextIsolation:true,sandbox:true}});
        windows.push(window);window.setIgnoreMouseEvents(true);window.setContentProtection(true);window.setAlwaysOnTop(true,'screen-saver');
        await window.loadURL(html);
        if(token===current&&!window.isDestroyed())window.showInactive();
      }));
      if(token===current)write('desktop-control-ack.json',{token:current,ready:true,displays:windows.length});
    }catch(error){
      if(token)write('desktop-control-ack.json',{token,ready:false,error:error.message});
      clear();
    }finally{busy=false;}
  }
  const timer=setInterval(()=>void tick(),100);
  return ()=>{clearInterval(timer);clear();};
}
module.exports={startDesktopControl};
