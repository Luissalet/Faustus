// Optional Android device preset in "New MCP server": a pinned exact package
// version (never a moving tag), adb located by the server itself when
// ANDROID_HOME is empty, labelled optional in the picker, and its setup help
// translated.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
const lib = read('../src/lib/mcpPresets.ts');
const block = lib.split("name: 'Android device (adb)'")[1].split("name: 'Filesystem'")[0];
assert.match(block, /optional: true/, 'flagged optional');
assert.match(block, /args: \['-y', '@mobilenext\/mobile-mcp@\d+\.\d+\.\d+'\]/, 'exact pinned version');
assert.doesNotMatch(block, /@latest|@next|@beta/, 'no moving tag');
assert.match(block, /env: \{ ANDROID_HOME: '' \}/, 'ANDROID_HOME present but empty (the server falls back to the SDK folder and PATH)');
assert.match(block, /LOCALAPPDATA/, 'help names the Windows SDK folder');
assert.match(block, /USB debugging/, 'help explains USB debugging');
const form = read('../src/screens/settings/IntegrationsMore.tsx');
assert.match(form, /p\.optional \? `\$\{p\.name\} — \$\{t\('optional'\)\}` : p\.name/, 'the picker marks optional presets');
const es = read('../src/i18n/es.ts');
assert.match(es, /"Optional\. Lets the agent see and tap a connected Android phone/, 'help translated to Spanish');
console.log('mcp-android-preset: ALL OK');
