// Opt-in integration test: uses the real renderer bridge and native window.
const assert=require('node:assert/strict');
const {writeFileSync}=require('node:fs');
const {join}=require('node:path');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
exports.run=async(window,root)=>{
  for(let i=0;i<100;i++){
    if(await window.webContents.executeJavaScript("Boolean(document.querySelector('.fs-desktop-bar button'))"))break;
    await pause(100);
  }
  const command=action=>window.webContents.executeJavaScript(`window.faustusWindow.command(${JSON.stringify(action)})`);
  console.log('FAUSTUS_RENDERER '+await window.webContents.executeJavaScript("JSON.stringify({url:location.href,bridge:typeof window.faustusWindow,body:document.body.innerText.slice(0,500)})"));
  assert.equal(await window.webContents.executeJavaScript("document.querySelectorAll('.fs-desktop-bar button').length"),4);
  await command('maximize');await pause(300);assert.equal(window.isMaximized(),true);
  await command('maximize');await pause(300);assert.equal(window.isMaximized(),false);
  await command('fullscreen');await pause(500);assert.equal(window.isFullScreen(),true);
  await command('fullscreen');await pause(500);assert.equal(window.isFullScreen(),false);
  const minimized=command('minimize');await pause(300);assert.equal(window.isMinimized(),true);
  window.restore();await minimized;await pause(1000);
  await assert.rejects(command('execute-shell'));
  const childReady=new Promise(resolve=>window.webContents.once('did-create-window',resolve));
  await window.webContents.executeJavaScript("void window.open(location.origin+'/login','faustus-qa-child')");
  const child=await childReady;
  for(let i=0;i<100;i++){
    if(!child.webContents.isLoading()&&await child.webContents.executeJavaScript("Boolean(document.querySelector('.fs-desktop-bar button'))"))break;
    await pause(100);
  }
  assert.equal(await child.webContents.executeJavaScript("document.querySelectorAll('.fs-desktop-bar button').length"),4);
  const childClosed=new Promise(resolve=>child.once('closed',resolve));
  void child.webContents.executeJavaScript("window.faustusWindow.command('close')").catch(()=>{});
  await childClosed;assert.equal(window.isDestroyed(),false);
  console.log('FAUSTUS_CHILD_WINDOW_PASSED');
  const bar=await window.webContents.executeJavaScript("(()=>{const el=document.querySelector('.fs-desktop-bar');const r=el.getBoundingClientRect();return {height:r.height,width:r.width,bg:getComputedStyle(el).backgroundColor}})()");
  assert.equal(bar.height,40);assert.ok(bar.width>=390);assert.notEqual(bar.bg,'rgba(0, 0, 0, 0)');
  writeFileSync(join(root,'logs','desktop-smoke.png'),(await window.webContents.capturePage()).toPNG());
  console.log('FAUSTUS_WINDOW_CONTROLS_PASSED '+JSON.stringify(bar));
  // Let startup settle first; closing destroys the renderer promise itself.
  setTimeout(()=>void command('close').catch(()=>{}),100);
};
