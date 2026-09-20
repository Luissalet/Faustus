const {contextBridge,ipcRenderer}=require('electron');
// No generic IPC: only the three messages "dictate anywhere" needs, same
// spirit as preload.cjs's faustusWindow bridge.
contextBridge.exposeInMainWorld('dictationRecorder',Object.freeze({
  onStart: callback => ipcRenderer.on('faustus:dictation-start',(_event,target,method)=>callback(target,method)),
  onStop: callback => ipcRenderer.on('faustus:dictation-stop',()=>callback()),
  reportResult: result => ipcRenderer.send('faustus:dictation-result',result),
}));
