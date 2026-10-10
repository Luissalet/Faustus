import assert from 'node:assert/strict';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules/esbuild/lib/main.js')).href);
const dir = mkdtempSync(join(tmpdir(), 'fs-shell-fence-'));
try {
  const out = join(dir, 'markdown.mjs');
  await build({ entryPoints: [join(root, 'studio/src/lib/markdown.ts')], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent' });
  const { parseMarkdown } = await import(pathToFileURL(out).href);
  const input = readFileSync(0, 'utf8').trim();
  if (input) {
    const { reply, output } = JSON.parse(input);
    const blocks = parseMarkdown(reply).blocks;
    assert.equal(blocks.length, 2, 'a literal reply has its header and one output block');
    assert.equal(blocks[1].kind, 'code');
    assert.equal(blocks[1].code, output.trimEnd(), 'the output never becomes headings, links or lists');
  } else {
    for (const mark of ['`', '~']) {
      const open = mark.repeat(4), short = mark.repeat(3);
      const parsed = parseMarkdown(open + 'text\nfirst\n' + short + '\n# literal heading\n' + open + '\nafter').blocks;
      assert.equal(parsed[0].kind, 'code');
      assert.equal(parsed[0].code, 'first\n' + short + '\n# literal heading');
      assert.equal(parsed[1].kind, 'para');
      const longer = parseMarkdown(open + '\nbody\n' + mark.repeat(5)).blocks;
      assert.equal(longer[0].code, 'body');
    }
    assert.equal(parseMarkdown('```text\nbody\n~~~\nstill body\n```').blocks[0].code, 'body\n~~~\nstill body');
  }
  console.log('ALL OK: literal output and matching fence length');
} finally { rmSync(dir, {recursive:true,force:true}); }
