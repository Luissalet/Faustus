// Process center (control center): the adapter talks to
// /api/process-center* the way routes/process_center_routes.py +
// src/process_center.py define it, the screen renders every section with a
// Stop per row, Ollama needs an explicit `allow_protected`, and the route
// is wired into the shell and the nav. Static source assertions only, no
// bundling — the same style nearby-apps.check.mjs uses.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const adapter = readFileSync(new URL('../src/adapters/processCenter.ts', import.meta.url), 'utf8');
assert.match(adapter, /export interface ProcRow/, 'ProcRow type exists');
assert.match(adapter, /export interface BgJobRow/, 'BgJobRow type exists');
assert.match(adapter, /export interface ProcessSnapshot/, 'ProcessSnapshot type exists');
assert.match(adapter, /export async function fetchProcesses\(watched\s*=\s*true\)/, 'fetchProcesses(watched = true) exists');
assert.match(adapter, /\/api\/process-center\?watched=/, 'fetchProcesses() calls GET /api/process-center?watched=');
assert.match(adapter, /export const stopProcess/, 'stopProcess() exists');
assert.match(adapter, /export const stopPort/, 'stopPort() exists');
assert.match(adapter, /'\/api\/process-center\/stop'/, 'stop calls POST /api/process-center/stop');
assert.match(adapter, /mcp_server:\s*string\s*\|\s*null/, 'ProcRow carries the matched MCP server name from src/process_center.py');

const screen = readFileSync(new URL('../src/screens/processes/Processes.tsx', import.meta.url), 'utf8');
assert.match(screen, /export function ProcessesScreen/, 'ProcessesScreen exists');
assert.match(screen, /fetchProcesses\(true\)/, 'the screen fetches with watched apps included');
assert.match(screen, /if \(!document\.hidden\) reload\(\); \}, 8000\)/, 'auto-refreshes every 8s, only while the tab is visible');
assert.match(screen, /!autoRefresh \|\| busyCount > 0/, 'auto-refresh pauses while a stop is in flight');
assert.match(screen, /t\('Listening ports'\)/, 'renders the Listening ports section');
assert.match(screen, /t\('Started by Faustus'\)/, 'renders the Started by Faustus section');
assert.match(screen, /t\('Background jobs'\)/, 'renders the Background jobs section');
assert.match(screen, /t\('Other apps'\)/, 'renders the Other apps section');
assert.match(screen, /t\('Stop all started by Faustus'\)/, '"Stop all started by Faustus" action exists');
assert.match(screen, /allow_protected:\s*isOllama/, 'Ollama stop sends allow_protected: true');
assert.match(screen, /fs-modes__confirm/, 'uses the inline confirm pattern, not window.confirm');
assert.doesNotMatch(screen, /window\.confirm/, 'never uses window.confirm');
assert.match(screen, /available === false/, 'shows the psutil-unavailable banner');
assert.match(screen, /Not visible in the process list/, 'a bg job whose pid is not in any list says so');
assert.match(screen, /recycled/, 'handles the recycled stop code');
assert.match(screen, /row\.mcp_server\s*&&/, 'a row with a matched MCP server renders it');
assert.match(screen, /data-testid="process-mcp-server"/, 'the MCP server match has its own testid for a "Started by Faustus" row');

const appShell = readFileSync(new URL('../src/shell/AppShell.tsx', import.meta.url), 'utf8');
assert.match(appShell, /const ProcessesScreen = lazyChunk\(\(\) => import\('\.\.\/screens\/processes\/Processes'\)/, 'AppShell lazy-loads the Processes screen');
assert.match(appShell, /<Route path="\/processes" element=\{<ProcessesScreen \/>\}/, 'AppShell routes /processes');

const routes = readFileSync(new URL('../src/shell/routes.ts', import.meta.url), 'utf8');
assert.match(routes, /\{ path: '\/processes', label: 'Processes', icon: Cpu \}/, 'TOOLS lists /processes');
assert.match(routes, /'\/processes',/, 'SERVER_ROUTES lists /processes');

console.log('process-center: ALL OK');
