const {contextBridge,ipcRenderer}=require('electron');
// No generic IPC, filesystem, shell, URL loading or model access is exposed.
contextBridge.exposeInMainWorld('faustusWindow',Object.freeze({
  command: action => ipcRenderer.invoke('faustus:window',action),
  subscribe: callback => {
    const listener=(_event,state)=>callback(state);
    ipcRenderer.on('faustus:window-state',listener);
    return ()=>ipcRenderer.removeListener('faustus:window-state',listener);
  },
}));

// Login is server-rendered, before the React shell. It still needs controls.
window.addEventListener('DOMContentLoaded',()=>{
  if(location.protocol!=='http:'||document.getElementById('studio-root'))return;
  const bar=document.createElement('header');bar.className='fs-desktop-bar';
  bar.setAttribute('aria-label','Window controls / Controles de ventana');
  const style=document.createElement('style');
  style.textContent=`.fs-desktop-bar{position:fixed;inset:0 0 auto;height:40px;z-index:1000;display:flex;align-items:stretch;background:var(--fs-surface-1,var(--panel,#121418));color:var(--fs-text-1,var(--fg,#f2f1ed));border-bottom:1px solid var(--fs-border,var(--border,#2b3038));font:13px system-ui;box-sizing:border-box}.fs-desktop-bar span{-webkit-app-region:drag;flex:1;display:flex;align-items:center;padding:0 18px;color:var(--fs-brand,var(--red,#e06c75));font-weight:600}.fs-desktop-bar button{width:44px;height:39px;display:grid;place-items:center;padding:0;border:0;border-radius:0;background:transparent;color:inherit;cursor:pointer}.fs-desktop-bar button:hover{background:var(--fs-surface-3,#30343a)}.fs-desktop-bar button:focus-visible{outline:2px solid var(--fs-focus,#8ab4ff);outline-offset:-3px}.fs-desktop-bar button:last-child:hover{background:var(--fs-danger-solid,#c8323e);color:var(--fs-on-danger-solid,#fff)}`;
  document.head.appendChild(style);
  const brand=document.createElement('span');brand.textContent='Faustus';bar.appendChild(brand);
  const icons=['<path d="M4 12h16"/>','<rect x="5" y="5" width="14" height="14"/>','<path d="M4 9V4h5m6 0h5v5M4 15v5h5m6 0h5v-5"/>','<path d="m6 6 12 12M6 18 18 6"/>'];
  ['minimize','maximize','fullscreen','close'].forEach((action,index)=>{
    const button=document.createElement('button');button.type='button';
    button.title=['Minimize / Minimizar','Maximize or restore / Maximizar o restaurar','Full screen / Pantalla completa','Close / Cerrar'][index];button.setAttribute('aria-label',button.title);
    button.innerHTML='<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'+icons[index]+'</svg>';
    button.addEventListener('click',()=>void ipcRenderer.invoke('faustus:window',action));bar.appendChild(button);
  });
  document.body.appendChild(bar);
});
