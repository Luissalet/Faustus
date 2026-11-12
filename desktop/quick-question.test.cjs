const assert = require('node:assert/strict');
const {test} = require('node:test');
const {setImmediate: nextTick} = require('node:timers/promises');
const {startQuickQuestion} = require('./quick-question.cjs');

const reply = settings => ({ok: true, json: async () => settings});

function native(fetch) {
  const keys = new Set();
  return {
    keys,
    net: {fetch},
    ipcMain: {handle() {}, removeHandler() {}},
    globalShortcut: {
      register(key) {
        if (key === 'unavailable' || keys.has(key)) return false;
        keys.add(key);
        return true;
      },
      unregister(key) { keys.delete(key); },
    },
  };
}

test('the previous shortcut stays registered until a replacement succeeds', async t => {
  let hotkey = 'Ctrl+Shift+Space';
  const electron = native(async () => reply({quick_question_hotkey: hotkey}));
  const quick = startQuickQuestion(electron, {origin: 'http://127.0.0.1:7000', profile: __dirname});
  t.after(() => quick.stop());
  await nextTick();
  assert.equal(quick.view().registered, hotkey);

  hotkey = 'unavailable';
  await quick.poll();
  assert.deepEqual(quick.view(), {registered: 'Ctrl+Shift+Space', shortcutUnavailable: true, visible: false});

  hotkey = 'Ctrl+Alt+F10';
  await quick.poll();
  assert.deepEqual([...electron.keys], [hotkey]);
  assert.equal(quick.view().shortcutUnavailable, false);
});

test('a placement change discards a settings response from the previous server', async t => {
  const local = 'http://127.0.0.1:7000';
  const remote = 'https://spark.test';
  const requests = [];
  let origin = local;
  let finishLocal;
  const localResponse = new Promise(resolve => { finishLocal = resolve; });
  const electron = native(async url => {
    requests.push(url);
    if (url.startsWith(local)) return localResponse;
    return reply({quick_question_hotkey: 'Ctrl+Alt+F10'});
  });
  const quick = startQuickQuestion(electron, {origin: () => origin, profile: __dirname});
  t.after(() => quick.stop());

  origin = remote;
  quick.retarget();
  finishLocal(reply({quick_question_hotkey: 'Ctrl+Shift+Space'}));
  await nextTick();
  assert.deepEqual(requests, [local + '/api/auth/settings', remote + '/api/auth/settings']);
  assert.deepEqual([...electron.keys], ['Ctrl+Alt+F10']);
});
