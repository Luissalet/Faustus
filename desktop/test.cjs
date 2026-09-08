const {test}=require('node:test');const assert=require('node:assert/strict');
const {localNavigation,externalNavigation}=require('./policy.cjs');
test('desktop navigation is origin-bound, without native URL protocols',()=>{
  assert.equal(localNavigation('http://127.0.0.1:7000/studio','http://127.0.0.1:7000'),true);
  for(const url of ['http://127.0.0.1:7001','http://127.0.0.1:7000.evil.test','file:///C:/secret','javascript:alert(1)','http://user@127.0.0.1:7000'])assert.equal(localNavigation(url,'http://127.0.0.1:7000'),false);
  assert.equal(externalNavigation('https://example.com'),true);
  for(const url of ['file:///C:/secret','shell:AppsFolder','javascript:alert(1)','https://user:pass@example.com'])assert.equal(externalNavigation(url),false);
});
