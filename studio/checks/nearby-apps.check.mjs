// Nearby apps — Connectors screen: `discoverApps()`/`adoptApp()` exist on
// the adapter, the panel is mounted in Connectors.tsx, "Add" calls
// `adoptApp`, and a `relocated_from` connector renders the "followed the
// app" note. Static source assertions only, no bundling — the same style
// `l72-missing-route.check.mjs` uses.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const adapter = readFileSync(new URL('../src/adapters/connectors.ts', import.meta.url), 'utf8');
assert.match(adapter, /export interface DiscoveredApp/, 'DiscoveredApp type exists');
assert.match(adapter, /export async function discoverApps/, 'discoverApps() exists');
assert.match(adapter, /'\/api\/app-connectors\/discover'/, 'discoverApps() calls GET /api/app-connectors/discover');
assert.match(adapter, /export const adoptApp/, 'adoptApp() exists');
assert.match(adapter, /'\/api\/app-connectors\/adopt'/, 'adoptApp() posts to /api/app-connectors/adopt');
assert.match(adapter, /relocated_from\?:\s*string \| null/, 'Connector carries relocated_from');

const panel = readFileSync(new URL('../src/screens/connectors/NearbyApps.tsx', import.meta.url), 'utf8');
assert.match(panel, /export function NearbyApps/, 'NearbyApps panel exists');
assert.match(panel, /discoverApps\(\)/, 'panel scans with discoverApps()');
assert.match(panel, /adoptApp\(\{\s*port:\s*app\.port\s*\}\)/, 'one-click Add calls adoptApp({ port })');
assert.match(panel, /onRequestForm/, 'a partial match (missing placeholders) hands off to the form instead of guessing');
assert.match(panel, /Nothing answering on this machine's ports/, 'empty state after a scan');

const form = readFileSync(new URL('../src/screens/connectors/NewConnectorForm.tsx', import.meta.url), 'utf8');
assert.match(form, /initialValues\?:\s*Record<string,\s*string>/, 'NewConnectorForm accepts initialValues');
assert.match(form, /initialValues\?\.\[p\]/, 'initialValues is merged over the preset defaults');

const screen = readFileSync(new URL('../src/screens/connectors/Connectors.tsx', import.meta.url), 'utf8');
assert.match(screen, /import \{ NearbyApps \}/, 'Connectors.tsx imports NearbyApps');
assert.match(screen, /<NearbyApps/, 'the panel is mounted in Connectors.tsx');
assert.match(screen, /relocated_from/, 'a relocated connector is rendered');
assert.match(screen, /Followed the app from \{from\} to \{to\}/, 'the "followed the app" note text');

console.log('nearby-apps: ALL OK');
