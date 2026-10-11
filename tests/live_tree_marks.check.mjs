// #247: workspace tree lit up with what the turn read / wrote.
// Driven by tests/test_studio_live_tree_marks.py, or by hand:
//   node tests/live_tree_marks.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-ftree-')), 'file-activity.mjs');
await build({
  entryPoints: [join(root, 'studio', 'src', 'screens', 'studio', 'file-activity.ts')],
  bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent',
});
const m = await import(pathToFileURL(out).href);
// The real stream decoder and turn reducer, to replay events captured from a live run.
async function bundle(entry, name) {
  const file = join(mkdtempSync(join(tmpdir(), 'fs-ftree-')), name);
  await build({ entryPoints: [join(root, entry)], bundle: true, format: 'esm', platform: 'node', outfile: file, logLevel: 'silent' });
  return import(pathToFileURL(file).href);
}
const { decode } = await bundle('studio/src/adapters/chat.ts', 'chat.mjs');
const { apply, blankTurn } = await bundle('studio/src/screens/studio/model.ts', 'model.mjs');
const read = (p) => readFileSync(join(root, p), 'utf8').replace(/\r\n/g, '\n');

let failed = 0;
const assert = (c, msg) => { if (!c) { failed += 1; console.error('FAIL:', msg); } else console.log('ok:', msg); };
const eq = (x, y, msg) => assert(JSON.stringify(x) === JSON.stringify(y), `${msg} (got ${JSON.stringify(x)})`);

const WIN = 'D:\\proj\\app';
const POSIX = '/home/me/app';

// ── markKey ──
eq(m.markKey('src/a.py', POSIX), 'src/a.py', 'relative path keeps its place');
eq(m.markKey('./src//a.py', POSIX), 'src/a.py', 'dot and double slashes are squashed');
eq(m.markKey('/home/me/app/src/a.py', POSIX), 'src/a.py', 'absolute posix path inside the workspace is relative');
eq(m.markKey('/home/me/other/a.py', POSIX), null, 'a path outside the workspace has no key');
eq(m.markKey('../a.py', POSIX), null, 'climbing out of the workspace has no key');
eq(m.markKey('src/../lib/b.py', POSIX), 'lib/b.py', '.. inside the workspace is resolved');
eq(m.markKey('D:\\proj\\app\\Src\\A.py', WIN), 'src/a.py', 'windows paths fold case and separators');
eq(m.markKey('d:/PROJ/app/src/a.py', WIN), 'src/a.py', 'windows drive letter and case do not matter');
eq(m.markKey('D:\\proj\\appendix\\a.py', WIN), null, 'a sibling that shares a prefix is outside');
eq(m.markKey('', POSIX), null, 'empty path has no key');
eq(m.markKey('https://example.com/a.py', POSIX), null, 'a URL is not a file');
eq(m.markKey('/home/me/app', POSIX), '', 'the workspace root is the empty key');
eq(m.markKey(42, POSIX), null, 'non-string has no key');
eq(m.markKey('\\\\?\\D:\\proj\\app\\Src\\a.py', WIN), 'src/a.py', 'a \\\\?\\ long path resolves inside a windows workspace');
eq(m.markKey('~/app/x.py', POSIX), 'x.py', '~/ resolves through the home implied by the workspace');
eq(m.markKey('~/app/x.py', 'C:\\Users\\me\\app'), 'x.py', '~/ under a windows user folder');
eq(m.markKey('~/other.py', POSIX), null, '~/ outside the workspace has no key');
eq(m.markKey('~/x.py', '/work/proj'), null, '~/ with a workspace outside any home has no key');

// ── per tool ──
const st = (tool, command, extra = {}) => ({ tool, command, output: '', state: 'succeeded', ...extra });
const marks = (steps, ws = POSIX) => m.fileMarksFromSteps(steps, ws);

eq(marks([st('read_file', 'notes/a.md')]), { 'notes/a.md': 'read' }, 'read_file with a plain first-line path');
eq(marks([st('read_file', '{"path":"notes/a.md","offset":3}')]), { 'notes/a.md': 'read' }, 'read_file with JSON args');
eq(marks([st('read_file', '{"file_path":"/home/me/app/notes/a.md"}')]), { 'notes/a.md': 'read' }, 'read_file with file_path and an absolute path');
eq(marks([st('write_file', 'out/b.txt\nhello')]), { 'out/b.txt': 'written' }, 'write_file path on the first line');
eq(marks([st('write_file', '{"path":"out/b.txt","content":"x"}')]), { 'out/b.txt': 'written' }, 'write_file JSON');
eq(marks([st('edit_file', '{"path":"src/c.py","old_string":"a","new_string":"b"}')]), { 'src/c.py': 'written' }, 'edit_file JSON');
eq(marks([st('append_file', 'log.txt\nmore')]), { 'log.txt': 'written' }, 'append_file counts as a write');
eq(marks([st('edit_file', '{"path":"zz.py"}', { diff: { file: 'src/d.py' } })]), { 'src/d.py': 'written', 'zz.py': 'written' }, 'a server diff that carries a real path names a written file too');
eq(marks([st('edit_file', '{"path":"src/main.py"}', { diff: { file: 'main.py' } })]), { 'src/main.py': 'written' }, 'a bare basename in the diff lights no phantom root file');

const patch = '*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** Add File: src/new.py\n+hi\n*** Delete File: old.txt\n*** End Patch';
eq(marks([st('apply_patch', patch)]), { 'src/a.py': 'written', 'src/new.py': 'written', 'old.txt': 'written' }, 'apply_patch headers');
eq(marks([st('apply_patch', JSON.stringify({ patch_text: patch }))]), { 'src/a.py': 'written', 'src/new.py': 'written', 'old.txt': 'written' }, 'apply_patch JSON patch_text');

const grepOut = '/home/me/app/src/a.py:12:def foo():\n/home/me/app/src/a.py:30:    foo()\n/home/me/app/lib/z.py:3:foo = 1\n/etc/passwd:1:root\n[grep stopped early]';
eq(marks([st('grep', '{"pattern":"foo"}', { output: grepOut })]), { 'src/a.py': 'read', 'lib/z.py': 'read' }, 'grep hits are read files, once each, outside files dropped');
eq(marks([st('grep', 'foo', { output: 'D:\\proj\\app\\src\\a.py:5:x' })], WIN), { 'src/a.py': 'read' }, 'grep with a windows drive path');
eq(marks([st('ls', 'src'), st('glob', '**/*.py', { output: 'src/a.py' })]), {}, 'ls and glob list names without reading files');

// bash
eq(marks([st('bash', 'cat src/a.py | head -5')]), { 'src/a.py': 'read' }, 'bash cat is a read');
eq(marks([st('bash', 'echo hi > out/x.txt')]), { 'out/x.txt': 'written' }, 'bash redirect is a write');
eq(marks([st('bash', 'echo hi >> "out/y z.txt"')]), { 'out/y z.txt': 'written' }, 'bash append redirect with quotes');
eq(marks([st('bash', "sed -i 's/a/b/' src/a.py")]), { 'src/a.py': 'written' }, 'sed -i is a write');
eq(marks([st('bash', "sed -n '1,5p' src/a.py")]), { 'src/a.py': 'read' }, 'plain sed is a read');
eq(marks([st('bash', 'grep -rn foo src/a.py')]), { 'src/a.py': 'read' }, 'grep skips its pattern and reads the file');
eq(marks([st('bash', 'cp a.txt backup/a.txt && cat a.txt')]), { 'a.txt': 'read', 'backup/a.txt': 'written' }, 'cp writes the destination, reads the source');
eq(marks([st('bash', 'mv old.txt new.txt')]), { 'old.txt': 'written', 'new.txt': 'written' }, 'mv touches both');
eq(marks([st('bash', 'curl https://x.dev/a.py -o /dev/null; ls -la')]), {}, 'urls and /dev/null are not files');
eq(marks([st('bash', JSON.stringify({ command: 'tee notes/t.md < in.txt' }))]), { 'in.txt': 'read', 'notes/t.md': 'written' }, 'bash JSON command, input redirect is a read');
eq(marks([st('bash', 'cat /etc/hosts')]), {}, 'a read outside the workspace is not marked');

// ── grep output that is not a path ──
eq(m.grepPaths('12:30:45 ERROR boom\n10:31:02 ok\nsrc/a.py:3:x'), ['src/a.py'], 'log timestamps are not files');
eq(m.grepPaths('Makefile:3:all:\nREADME:9:x\nserver.log:14:error'), ['Makefile', 'README', 'server.log'], 'bare well-known names and dotted names are files');
eq(m.grepPaths('[grep stopped early after the time budget of 20s: results are partial]'), [], 'the notice line is not a file');

// ── bash: the false positives a review found ──
eq(marks([st('bash', "cat > s.py <<'EOF'\nimport os\ncat other.txt\nrm victim.log\nEOF\ncat real.txt")]), { 's.py': 'written', 'real.txt': 'read' }, 'here-doc body is data, not commands');
eq(marks([st('bash', 'cat <<-END > t.txt\n\trm x.log\n\tEND\nls')]), { 't.txt': 'written' }, 'a <<- here-doc ends at its tab-indented delimiter');
eq(marks([st('bash', 'echo "a; b > c/d.txt"')]), {}, 'an operator inside double quotes is text');
eq(marks([st('bash', "echo 'x && rm y.txt'")]), {}, 'an operator inside single quotes is text');
eq(marks([st('bash', 'grep -A 3 foo.bar a.py')]), { 'a.py': 'read' }, 'a grep option value is not the pattern or a file');
eq(marks([st('bash', 'grep -A3 -B 1 foo.bar a.py')]), { 'a.py': 'read' }, 'attached and detached option values');
eq(marks([st('bash', 'rg -g "*.py" -t py needle src/m.py')]), { 'src/m.py': 'read' }, 'ripgrep glob and type values');
eq(marks([st('bash', 'grep -e foo -f pats.txt a.py')]), { 'pats.txt': 'read', 'a.py': 'read' }, 'the pattern file is read, -e is the pattern');
eq(marks([st('bash', 'cd sub && cat a.txt')]), { 'sub/a.txt': 'read' }, 'a leading cd moves the paths');
eq(marks([st('bash', 'cd /home/me/app/lib && cat x.py')]), { 'lib/x.py': 'read' }, 'an absolute cd inside the workspace');
eq(marks([st('bash', 'cd "$DIR" && cat a.txt')]), {}, 'a cd we cannot resolve drops the marks after it');
eq(marks([st('bash', 'cd .. && cat a.txt')]), {}, 'a cd out of the workspace drops them too');
eq(marks([st('bash', 'grep -r foo src/')]), {}, 'a recursive search of a folder reads no file');
eq(marks([st('bash', 'grep -rn foo src/deep')]), {}, 'a folder without extension under -r is not a file');
eq(marks([st('bash', 'rm -rf build/ dist')]), {}, 'rm -rf of folders writes no file');
eq(marks([st('bash', 'rm -rf build/out.o')]), { 'build/out.o': 'written' }, 'rm -rf of a file still counts');
eq(marks([st('bash', 'cat a.txt 2>&1 | head')]), { 'a.txt': 'read' }, '2>&1 is not a redirect to a file');
eq(marks([st('bash', 'sleep 1 & cat a.txt')]), { 'a.txt': 'read' }, 'a background & ends the segment');

// ── states ──
eq(marks([st('write_file', 'a.txt\nx', { state: 'running' })]), { 'a.txt': 'written' }, 'a running write already lights up');
eq(marks([st('write_file', 'a.txt\nx', { state: 'partial' })]), { 'a.txt': 'written' }, 'a partial write lights up');
eq(marks([st('write_file', 'a.txt\nx', { state: 'failed' })]), {}, 'a failed write marks nothing');
eq(marks([st('write_file', 'a.txt\nx', { state: 'waiting' })]), {}, 'a write waiting for approval marks nothing');
eq(marks([st('read_file', 'a.txt', { state: 'failed' })]), {}, 'a failed read marks nothing');

// ── an approval card is open, or answered: the REAL events of a live run ──
// Captured from Faustus (fake model): read_file calc.py, then edit_file docs/guide.md, which the server gate
// holds because the read armed it. The server closes the held call as "succeeded" with a wait message.
const fx = JSON.parse(read('tests/fixtures/live_tree_approval_events.json'));
const FWS = '/work/proj';
const play = (...legs) => {
  let turn = blankTurn('assistant');
  for (const leg of legs) for (const ev of leg) {
    const decoded = decode(JSON.parse(JSON.stringify(ev).split('{{WS}}').join(FWS)), null);
    if (decoded) turn = apply(turn, decoded);
  }
  return turn;
};
const asTurns = (turn) => [{ role: 'user', steps: [] }, turn];
{
  const pending = play(fx.approve[0]);
  const edit = pending.steps.find((s) => s.tool === 'edit_file');
  assert(edit && /^Waiting for an exact user approval/.test(edit.output), `the held edit_file step carries the wait message (state ${edit && edit.state})`);
  assert(pending.ask && pending.ask.kind === 'tool_approval', 'the approval card is open');
  eq(m.turnFileMarks(asTurns(pending), FWS), { 'calc.py': 'read' }, 'edit_file waiting for approval is NOT written (only the read is marked)');
  const approved = play(fx.approve[0], fx.approve[1]);
  eq(m.turnFileMarks(asTurns(approved), FWS), { 'calc.py': 'read', 'docs/guide.md': 'written' }, 'approved: the edit that really ran is written');
  eq(approved.steps.filter((s) => s.tool === 'edit_file').length, 2, 'the parked step stays next to the replayed one and marks nothing by itself');
  const running = play(fx.approve[0], [fx.approve[1][0]]);
  eq(m.turnFileMarks(asTurns(running), FWS), { 'calc.py': 'read', 'docs/guide.md': 'written' }, 'approved and running: already written');
  const denied = play(fx.deny[0], fx.deny[1]);
  eq(m.turnFileMarks(asTurns(denied), FWS), { 'calc.py': 'read' }, 'denied: the edit never ran, nothing written');
}
eq(m.stepRan({ state: 'succeeded', output: 'Waiting for an exact user approval.', resultStatus: 'succeeded' }), false, 'a parked step did not run');
eq(m.stepRan({ state: 'succeeded', output: 'Edited a.py (1 replacement)', resultStatus: 'denied' }), false, 'a denied result did not run');
eq(m.stepRan({ state: 'succeeded', output: 'Edited a.py (1 replacement)', resultStatus: 'succeeded' }), true, 'a finished edit ran');

// ── written wins ──
eq(marks([st('read_file', 'a.txt'), st('edit_file', '{"path":"a.txt"}')]), { 'a.txt': 'written' }, 'read then write -> written');
eq(marks([st('edit_file', '{"path":"a.txt"}'), st('read_file', 'a.txt')]), { 'a.txt': 'written' }, 'write then read -> still written');
eq(marks([st('read_file', 'A.txt'), st('read_file', './a.txt')], POSIX), { 'A.txt': 'read', 'a.txt': 'read' }, 'posix paths are case-sensitive');
eq(marks([st('read_file', 'A.txt'), st('edit_file', '{"path":"./a.txt"}')], WIN), { 'a.txt': 'written' }, 'windows paths are not');

// ── turn boundaries ──
const turn = (role, steps = []) => ({ role, steps });
const turns1 = [turn('user'), turn('assistant', [st('read_file', 'a.txt'), st('write_file', 'b.txt\nx')])];
eq(m.turnFileMarks(turns1, POSIX), { 'a.txt': 'read', 'b.txt': 'written' }, 'marks of the turn that just ran');
const turns2 = [...turns1, turn('user'), turn('assistant', [])];
eq(m.turnFileMarks(turns2, POSIX), {}, 'a new user message clears the marks');
const turns3 = [...turns1, turn('user'), turn('assistant', [st('read_file', 'c.txt')])];
eq(m.turnFileMarks(turns3, POSIX), { 'c.txt': 'read' }, 'only the new turn counts');
eq(m.turnFileMarks(turns1, ''), {}, 'no workspace, no marks');
eq(m.turnFileMarks([turn('assistant', [st('read_file', 'z.txt')])], POSIX), { 'z.txt': 'read' }, 'with no user message the whole transcript is the turn');
eq(m.turnFileMarks([turn('user'), turn('assistant', [st('read_file', 'a.txt')]), turn('assistant', [st('write_file', 'b.txt\nx')])], POSIX), { 'a.txt': 'read', 'b.txt': 'written' }, 'several assistant turns after one prompt (group/team) all count');

// ── hidden marks ──
eq(m.splitHidden({ '.env': 'read', 'a/.git/config': 'read', 'a/b.py': 'written' }), { shown: { 'a/b.py': 'written' }, hidden: 2 }, 'dot-files and dot-folders are counted as hidden, not shown');
eq(m.isHiddenKey('src/.cache/x'), true, 'a dot segment hides the path');
eq(m.isHiddenKey('src/cache/x.py'), false, 'a plain path is not hidden');

// ── folders and counts ──
const mk = { 'src/deep/a.py': 'read', 'src/b.py': 'written', 'top.txt': 'read' };
eq(m.folderMarks(mk), { '': { read: 2, written: 1 }, src: { read: 1, written: 1 }, 'src/deep': { read: 1, written: 0 } }, 'ancestors collect counts');
eq(m.markCounts(mk), { read: 2, written: 1 }, 'legend counts');
eq(m.folderMarkKind({ read: 2, written: 1 }), 'written', 'folder shows written first');
eq(m.folderMarkKind({ read: 2, written: 0 }), 'read', 'folder shows read otherwise');
eq(m.folderMarkKind({ read: 0, written: 0 }), null, 'nothing below, no mark');
eq(m.folderMarkKind(undefined), null, 'unknown folder, no mark');

// ── wiring ──
const panel = read('studio/src/screens/studio/SidePanel.tsx');
assert(panel.includes("id:'files'") && panel.includes('<FileTree') && panel.includes('turnFileMarks(turns,workspace)'), 'SidePanel has the Files tab fed by turnFileMarks');
assert(panel.includes("(tab.id!=='files'||Boolean(workspace))"), 'the Files tab only exists with a workspace');
assert(read('studio/src/screens/studio/panel.ts').includes("'outputs' | 'files'"), 'PanelTab knows files');
const tree = read('studio/src/screens/studio/FileTree.tsx');
for (const s of ['file-tree-file', 'file-tree-folder', 'file-tree-legend', 'data-mark', 'data-under']) assert(tree.includes(s), `FileTree exposes ${s}`);
const tsv = read('docs/ui/i18n/es.tsv');
const unq = (x) => x.replace(/\\'/g, "'");
const keys = [...tree.matchAll(/\bt\('((?:[^'\\]|\\.)*)'/g)].map((x) => unq(x[1]));
for (const x of tree.matchAll(/\btn\([^,]+,\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)'/g)) keys.push(unq(x[1]), unq(x[2]));
keys.push('Files');
assert(tree.includes("tn(written, '{n} written', '{n} written#')") && tree.includes("tn(read, '{n} read', '{n} read#')"), 'the counters use the repo plural helper (1 escrito / 2 escritos)');
assert(read('studio/src/i18n/en.ts').includes("'{n} written#': '{n} written'") && read('studio/src/i18n/en.ts').includes("'{n} read#': '{n} read'"), 'English maps the plural keys back');
assert(keys.length > 8, `FileTree strings found (${keys.length})`);
for (const k of keys) assert(tsv.split('\n').some((l) => l.split('\t')[0] === k), `es.tsv has a row for "${k}"`);

console.log(failed ? `${failed} FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
