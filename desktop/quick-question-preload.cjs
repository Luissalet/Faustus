const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('faustusQuick', Object.freeze({
  isQuick: true,
  openStudio: id => ipcRenderer.invoke('faustus:quick', 'studio', id),
  hide: () => ipcRenderer.invoke('faustus:quick', 'hide'),
  onFocus: callback => {
    const listener = () => callback();
    ipcRenderer.on('faustus:quick-focus', listener);
    return () => ipcRenderer.removeListener('faustus:quick-focus', listener);
  },
}));
