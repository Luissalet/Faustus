const {test}=require('node:test');const assert=require('node:assert/strict');
const {localNavigation,externalNavigation,permissionCheck,permissionRequest,deepLinkPath,findDeepLink}=require('./policy.cjs');
test('desktop navigation is origin-bound, without native URL protocols',()=>{
  assert.equal(localNavigation('http://127.0.0.1:7000/studio','http://127.0.0.1:7000'),true);
  for(const url of ['http://127.0.0.1:7001','http://127.0.0.1:7000.evil.test','file:///C:/secret','javascript:alert(1)','http://user@127.0.0.1:7000'])assert.equal(localNavigation(url,'http://127.0.0.1:7000'),false);
  assert.equal(externalNavigation('https://example.com'),true);
  for(const url of ['file:///C:/secret','shell:AppsFolder','javascript:alert(1)','https://user:pass@example.com'])assert.equal(externalNavigation(url),false);
});
test('the local page may write sanitized clipboard text without a prompt',()=>{
  const origin='http://127.0.0.1:7000';
  const granted=new Set();
  assert.equal(permissionCheck('clipboard-sanitized-write',origin,origin,granted),true);
  assert.equal(permissionRequest('clipboard-sanitized-write',origin+'/studio',origin),'allow');
});
test('clipboard-read and media still need a prompt; anything else is denied',()=>{
  const origin='http://127.0.0.1:7000';
  const granted=new Set();
  assert.equal(permissionCheck('clipboard-read',origin,origin,granted),false);
  assert.equal(permissionRequest('clipboard-read',origin+'/studio',origin),'prompt');
  assert.equal(permissionRequest('media',origin+'/studio',origin),'prompt');
  assert.equal(permissionRequest('notifications',origin+'/studio',origin),'prompt');
  assert.equal(permissionCheck('geolocation',origin,origin,granted),false);
  assert.equal(permissionRequest('geolocation',origin+'/studio',origin),'deny');
  assert.equal(permissionCheck('clipboard-sanitized-write','https://evil.example',origin,granted),false);
  assert.equal(permissionRequest('clipboard-sanitized-write','https://evil.example',origin),'deny');
});
test('a grant remembered from the prompt is enough for a later check',()=>{
  const origin='http://127.0.0.1:7000';
  const granted=new Set(['clipboard-read']);
  assert.equal(permissionCheck('clipboard-read',origin,origin,granted),true);
  assert.equal(permissionCheck('media',origin,origin,granted),false);
});

test('main.cjs parks the window in the tray and releases models on Quit', () => {
  const src = require('node:fs').readFileSync(require('node:path').join(__dirname, 'main.cjs'), 'utf8');
  assert.match(src, /new Tray\(/);
  assert.match(src, /parkInTray\(\)/);
  assert.match(src, /Salir \/ Quit/);
  assert.match(src, /--smoke-test.*--no-tray|--no-tray.*--smoke-test/);
  assert.match(src, /join\(root,'Stop-Local-Models\.ps1'\)/);
  assert.ok(require('node:fs').existsSync(require('node:path').join(__dirname, 'tray.ico')));
});

test('faustus:// links open reading places only, with the selecting query keys', () => {
  assert.equal(deepLinkPath('faustus://studio?s=abc'), '/studio?s=abc');
  assert.equal(deepLinkPath('faustus:///studio?s=abc'), '/studio?s=abc');
  assert.equal(deepLinkPath('faustus://studio?draft=hola&send=1&image=https://x'), '/studio?draft=hola', 'send and image are dropped');
  assert.equal(deepLinkPath('faustus://board?issue=FAU-12'), '/board?issue=FAU-12');
  assert.equal(deepLinkPath('faustus://library/documents'), '/library/documents');
  assert.equal(deepLinkPath('faustus://'), '/studio');
  assert.equal(deepLinkPath('faustus://studio/../api'), '/studio/api', 'dot segments never climb out of the first place');
  for (const bad of ['faustus://api/auth/settings', 'faustus://../api/auth/settings', 'https://studio', 'faustus://user:pw@studio', 'faustus://evil%2F..']) {
    assert.equal(deepLinkPath(bad), null, bad);
  }
  assert.equal(findDeepLink(['electron.exe', '.', 'faustus://home']), '/home');
  assert.equal(findDeepLink(['electron.exe', '--flag']), null);
});

test('main.cjs registers faustus:// and routes a link from a second launch', () => {
  const src = require('node:fs').readFileSync(require('node:path').join(__dirname, 'main.cjs'), 'utf8');
  assert.match(src, /setAsDefaultProtocolClient\('faustus'/);
  assert.match(src, /second-instance',\(_event,argv\)=>/);
  assert.match(src, /openDeepLink\(findDeepLink\(argv\)\)/);
  assert.match(src, /if\(pendingLink\)/);
});
