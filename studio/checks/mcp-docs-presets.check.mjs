// Remote documentation presets in "New MCP server": GitMCP for one GitHub
// repository ({repo} filled from owner/repo or a pasted address) and DeepWiki
// for any public repository, both Streamable HTTP, no key. The owner/repo
// parser is exercised here on the source's own regex.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
const lib = read('../src/lib/mcpPresets.ts');
assert.match(lib, /url: 'https:\/\/gitmcp\.io\/\{repo\}'/, 'GitMCP preset');
assert.match(lib, /url: 'https:\/\/mcp\.deepwiki\.com\/mcp'/, 'DeepWiki preset');
assert.equal((lib.match(/transport: 'http'/g) || []).length, 2, 'both over Streamable HTTP');
const src = lib.match(/const m = raw\.match\((\/.*\/)\);/)[1];
const re = new Function(`return ${src}`)();
const parse = (text) => {
  const raw = text.trim().replace(/\.git$/, '');
  const m = raw.match(re);
  return m && m[2] !== '.' && m[2] !== '..' ? `${m[1]}/${m[2]}` : null;
};
assert.equal(parse('idosal/git-mcp'), 'idosal/git-mcp');
assert.equal(parse('https://github.com/idosal/git-mcp.git'), 'idosal/git-mcp');
assert.equal(parse('https://github.com/fastapi/fastapi/tree/master/docs'), 'fastapi/fastapi');
assert.equal(parse('not a repo'), null);
assert.equal(parse('https://evil.example.com/a/b'), null);
assert.equal(parse('owner/..'), null);
const form = read('../src/screens/settings/IntegrationsMore.tsx');
assert.match(form, /if \(p\.url\) \{[\s\S]*?setTransport\(p\.transport \?\? 'http'\)/, 'a remote preset sets URL and transport, no command');
assert.match(form, /data-testid="mcp-repo"/, 'the repository field');
console.log('mcp-docs-presets: ALL OK');
