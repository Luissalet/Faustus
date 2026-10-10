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

// ── states ──
eq(marks([st('write_file', 'a.txt\nx', { state: 'running' })]), { 'a.txt': 'written' }, 'a running write already lights up');
eq(marks([st('write_file', 'a.txt\nx', { state: 'partial' })]), { 'a.txt': 'written' }, 'a partial write lights up');
eq(marks([st('write_file', 'a.txt\nx', { state: 'failed' })]), {}, 'a failed write marks nothing');
eq(marks([st('write_file', 'a.txt\nx', { state: 'waiting' })]), {}, 'a write waiting for approval marks nothing');
eq(marks([st('read_file', 'a.txt', { state: 'failed' })]), {}, 'a failed read marks nothing');

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
const keys = [...tree.matchAll(/\bt\('((?:[^'\\]|\\.)*)'/g)].map((x) => x[1].replace(/\\'/g, "'"));
keys.push('Files');
assert(keys.length > 8, `FileTree strings found (${keys.length})`);
for (const k of keys) assert(tsv.split('\n').some((l) => l.split('\t')[0] === k), `es.tsv has a row for "${k}"`);

console.log(failed ? `${failed} FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
