// /review: the staged review of the working folder (POST /api/review/worktree).
// Needs a working folder, sends the chat's own model name (the server picks
// the owner's endpoint for it; a URL never travels from the client).
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
const cmds = read('../src/screens/studio/commands.ts');
assert.match(cmds, /name: 'review', aliases: \['cr', 'code-review'\]/, '/review is registered');
const adapter = read('../src/adapters/commands.ts');
assert.match(adapter, /post<[^>]+>\('\/api\/review\/worktree', \{\s*workspace, base: base \|\| 'HEAD', model: model \|\| undefined,/, 'the adapter posts workspace, base and model only');
assert.doesNotMatch(adapter, /review\/worktree[\s\S]{0,200}endpoint_url/, 'no endpoint URL from the client');
const studio = read('../src/screens/Studio.tsx');
assert.match(studio, /case 'review': \{\s*if \(!workspace\)/, 'needs a working folder');
assert.match(studio, /worktreeReviewMarkdown\(workspace, base, route\?\.model\)/, 'uses the chat model');
console.log('review-command: ALL OK');
