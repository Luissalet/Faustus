// Apps (apps_contract.md, lot B as amended): the "Apps" card grid lives
// INSIDE the Processes screen, backed by `/api/launch-profiles*`
// (routes/connector_routes.py + src/launch_profiles.py). Static source
// assertions only, no bundling — the same style process-center.check.mjs
// and whatsapp.check.mjs use.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const adapter = readFileSync(new URL('../src/adapters/apps.ts', import.meta.url), 'utf8');
assert.match(adapter, /export interface AppProfile/, 'AppProfile type exists');
assert.match(adapter, /export interface AppProfileInput/, 'AppProfileInput type exists');
assert.match(adapter, /export interface AppStatus/, 'AppStatus type exists');
assert.match(adapter, /export async function listApps/, 'listApps() exists');
assert.match(adapter, /export function saveApp/, 'saveApp() exists');
assert.match(adapter, /export const deleteApp/, 'deleteApp() exists');
assert.match(adapter, /export async function appStatuses/, 'appStatuses() exists');
assert.match(adapter, /export const appStatus\b/, 'appStatus() exists');
assert.match(adapter, /export const startApp/, 'startApp() exists');
assert.match(adapter, /export const stopApp/, 'stopApp() exists');
assert.match(adapter, /export const restartApp/, 'restartApp() exists');
assert.match(adapter, /export const openApp/, 'openApp() exists');
assert.match(adapter, /export const appIconUrl/, 'appIconUrl() exists');
assert.match(adapter, /export async function appLog/, 'appLog() exists');
assert.match(adapter, /'\/api\/launch-profiles'/, 'listApps calls GET /api/launch-profiles');
assert.match(adapter, /\/api\/launch-profiles\/status/, 'appStatuses calls GET /api/launch-profiles/status');
assert.match(adapter, /\/api\/launch-profiles\/\$\{encodeURIComponent\(id\)\}\/stop/, 'stopApp calls POST /api/launch-profiles/{id}/stop');
assert.match(adapter, /\/api\/launch-profiles\/\$\{encodeURIComponent\(id\)\}\/restart/, 'restartApp (and startApp) call POST /api/launch-profiles/{id}/restart');
assert.match(adapter, /\/api\/launch-profiles\/\$\{encodeURIComponent\(id\)\}\/open/, 'openApp calls POST /api/launch-profiles/{id}/open');
assert.match(adapter, /\/api\/launch-profiles\/\$\{encodeURIComponent\(id\)\}\/icon/, 'appIconUrl builds the icon route');
assert.match(adapter, /\/api\/launch-profiles\/\$\{encodeURIComponent\(id\)\}\/log\?lines=/, 'appLog calls GET /api/launch-profiles/{id}/log?lines=');
assert.match(adapter, /credentials:\s*'same-origin'/, 'requests use credentials: same-origin');

const apps = readFileSync(new URL('../src/screens/processes/Apps.tsx', import.meta.url), 'utf8');
assert.match(apps, /export function AppsSection/, 'AppsSection exists');
assert.match(apps, /id="apps"/, 'the section has id="apps" (so /processes#apps scrolls to it)');
assert.match(apps, /if \(!document\.hidden\) reloadStatuses\(\); \}, 5000\)/, 'statuses poll every 5s');
assert.match(apps, /appStatuses\(\)/, 'statuses come from ONE appStatuses() call, not per-card');
assert.match(apps, /t\('Add app'\)/, 'has an Add app action');
assert.match(apps, /testId="apps-add"/, 'Add app has testid apps-add');
assert.match(apps, /testId="apps-card"|data-testid="apps-card"/, 'each card has testid apps-card');
assert.match(apps, /testId=\{running \? 'apps-open' : 'apps-start'\}/, 'the primary action has testid apps-start when stopped, apps-open when running');
assert.match(apps, /testId="apps-stop"/, 'Stop action has testid apps-stop');
assert.match(apps, /data-testid="apps-stop-confirm"/, 'Stop has an inline two-step confirm (testid apps-stop-confirm)');
assert.match(apps, /testId="apps-restart"/, 'Restart action has testid apps-restart');
assert.match(apps, /testId="apps-console"/, 'Console toggle has testid apps-console');
assert.match(apps, /testId="apps-edit"/, 'Edit action has testid apps-edit');
assert.match(apps, /testId="apps-remove"/, 'Remove action has testid apps-remove');
assert.match(apps, /fs-modes__confirm/, 'uses the inline confirm pattern, not window.confirm');
assert.doesNotMatch(apps, /window\.confirm/, 'never uses window.confirm');
assert.match(apps, /appIconUrl\(/, 'card icon uses appIconUrl with an initial-letter fallback on error');
assert.match(apps, /onError=\{\(\) => setBroken\(true\)\}/, 'icon falls back to the initial on image load error');
assert.match(apps, /if \(!document\.hidden\) reload\(\); \}, 3000\)/, 'the console auto-refreshes every 3s while open');
assert.match(apps, /appLog\(appId,\s*300\)/, 'console reads the tail via GET /log?lines=300');
assert.match(apps, /data-console-open/, 'an open console expands its card across the full grid width');

const form = readFileSync(new URL('../src/screens/processes/AppForm.tsx', import.meta.url), 'utf8');
assert.match(form, /export function AppForm/, 'AppForm exists');
assert.match(form, /data-testid="apps-form"/, 'the form panel has testid apps-form');
assert.match(form, /testId="apps-form-save"/, 'Save has testid apps-form-save');
assert.match(form, /saveApp\(/, 'the form saves via saveApp (POST or PATCH depending on existing)');
assert.match(form, /'process'.*'open_exe'.*'open_url'|open_exe.*open_url|kind === 'open_url'/, 'the kind picker distinguishes process/open_exe/open_url');
assert.match(form, /desktop/, 'the form has the desktop-window toggle');
assert.match(form, /stop_cmd/, 'the form has a stop command field');
assert.match(form, /appIconUrl\(saved\.id\)/, 'the icon field previews via appIconUrl once saved');

const css = readFileSync(new URL('../src/screens/processes/processes.css', import.meta.url), 'utf8');
assert.match(css, /\.fs-apps__grid \{[^}]*grid-template-columns:\s*repeat\(auto-fill,\s*minmax\(300px/, 'the grid is auto-fill, min 300px per card');
assert.match(css, /\.fs-apps__icon \{[^}]*inline-size:\s*48px/, 'the card icon is 48px');
assert.doesNotMatch(css, /outline:\s*none/, 'never disables the focus outline');
assert.doesNotMatch(css, /#[0-9a-fA-F]{3,8}\b/, 'no literal hex colours — tokens only');

const screen = readFileSync(new URL('../src/screens/processes/Processes.tsx', import.meta.url), 'utf8');
assert.match(screen, /import \{ AppsSection \} from '\.\/Apps'/, 'Processes.tsx imports AppsSection');
assert.match(screen, /<AppsSection say=\{say\}\s*\/>/, 'Processes.tsx renders <AppsSection say={say} />');

const launchProfiles = readFileSync(new URL('../src/screens/connectors/LaunchProfiles.tsx', import.meta.url), 'utf8');
assert.match(launchProfiles, /\/processes#apps/, 'the Connectors launch-profiles panel links to /processes#apps');

console.log('apps: ALL OK');
