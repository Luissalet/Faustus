// PWA (lot P-B): install + push notifications. Static source assertions
// only, the same style apps.check.mjs and whatsapp.check.mjs use — no
// bundling, no browser.
import { readFileSync, existsSync } from 'node:fs';
import assert from 'node:assert/strict';

const indexHtml = readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');
assert.match(indexHtml, /<link rel="manifest" href="\/manifest\.webmanifest">/, 'index.html links /manifest.webmanifest');
assert.match(indexHtml, /navigator\.serviceWorker\.register\('\/sw\.js',\s*\{\s*scope:\s*'\/'\s*\}\)/, "index.html registers /sw.js with scope '/'");
assert.match(indexHtml, /apple-touch-icon" href="\/static\/pwa\/apple-touch-icon\.png"/, 'apple-touch-icon points at static/pwa');

const pwaRoutes = readFileSync(new URL('../../routes/pwa_routes.py', import.meta.url), 'utf8');
assert.match(pwaRoutes, /@router\.get\("\/sw\.js"\)/, 'GET /sw.js is served');
assert.match(pwaRoutes, /Service-Worker-Allowed/, 'the /sw.js response sets Service-Worker-Allowed');
assert.match(pwaRoutes, /@router\.get\("\/manifest\.webmanifest"\)/, 'GET /manifest.webmanifest is served');
assert.match(pwaRoutes, /application\/manifest\+json/, 'the manifest is served with application/manifest+json');

const appPy = readFileSync(new URL('../../app.py', import.meta.url), 'utf8');
assert.match(appPy, /"\/sw\.js"/, '/sw.js is auth-exempt');
assert.match(appPy, /"\/manifest\.webmanifest"/, '/manifest.webmanifest is auth-exempt');

const manifest = JSON.parse(readFileSync(new URL('../../static/manifest.json', import.meta.url), 'utf8'));
assert.equal(manifest.display, 'standalone', 'manifest display is standalone');
assert.equal(manifest.start_url, '/?source=pwa', 'manifest start_url is /?source=pwa');
for (const icon of manifest.icons) {
  assert.ok(icon.src.startsWith('/static/pwa/'), `icon ${icon.src} is served from /static/pwa/`);
  const onDisk = new URL('../..' + icon.src, import.meta.url);
  assert.ok(existsSync(onDisk), `icon file exists on disk: ${icon.src}`);
}
assert.ok(manifest.icons.some((i) => i.purpose === 'maskable'), 'a maskable icon is declared');
assert.ok(manifest.icons.some((i) => i.purpose !== 'maskable'), 'a non-maskable ("any") icon is declared');

const sw = readFileSync(new URL('../../static/sw.js', import.meta.url), 'utf8');
assert.match(sw, /addEventListener\('push',/, 'sw.js has a push handler');
assert.match(sw, /showNotification\(/, "the push handler calls showNotification");
assert.match(sw, /addEventListener\('notificationclick',/, 'sw.js has a notificationclick handler');
assert.match(sw, /clients\.openWindow\(/, 'notificationclick falls back to clients.openWindow');
assert.match(sw, /addEventListener\('pushsubscriptionchange',/, 'sw.js re-subscribes on pushsubscriptionchange');

const adapter = readFileSync(new URL('../src/adapters/push.ts', import.meta.url), 'utf8');
for (const fn of [
  'getVapidKey',
  'subscribeThisDevice',
  'unsubscribeThisDevice',
  'listSubscriptions',
  'sendTest',
  'currentSubscription',
  'isSupported',
]) {
  assert.match(adapter, new RegExp(`export (async )?function ${fn}|export const ${fn}`), `adapters/push.ts exports ${fn}`);
}

const installPrompt = readFileSync(new URL('../src/lib/installPrompt.ts', import.meta.url), 'utf8');
assert.match(installPrompt, /beforeinstallprompt/, 'installPrompt.ts listens for beforeinstallprompt');
assert.match(installPrompt, /export function captureInstallPrompt/, 'captureInstallPrompt is exported');
assert.match(installPrompt, /export async function promptInstall/, 'promptInstall is exported');

const main = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8');
assert.match(main, /captureInstallPrompt\(\)/, 'main.tsx captures the install prompt eagerly');

const thisDevice = readFileSync(new URL('../src/screens/settings/ThisDevice.tsx', import.meta.url), 'utf8');
assert.match(thisDevice, /export function ThisDeviceSection/, 'ThisDeviceSection exists');
assert.match(thisDevice, /subscribeThisDevice\(/, 'ThisDevice enables push via subscribeThisDevice');
assert.match(thisDevice, /unsubscribeThisDevice\(/, 'ThisDevice disables push via unsubscribeThisDevice');
assert.match(thisDevice, /promptInstall\(/, 'ThisDevice offers the install prompt');

const settings = readFileSync(new URL('../src/screens/Settings.tsx', import.meta.url), 'utf8');
assert.match(settings, /import \{ ThisDeviceSection \} from '\.\/settings\/ThisDevice'/, 'Settings.tsx imports ThisDeviceSection');
assert.match(settings, /\{ key: 'device', label: 'This device'/, "Settings.tsx has a 'device' section next to Appearance");
assert.match(settings, /section === 'device' && <ThisDeviceSection say=\{say\} \/>/, 'Settings.tsx mounts <ThisDeviceSection />');

const platform = readFileSync(new URL('../src/shell/platform.ts', import.meta.url), 'utf8');
assert.match(platform, /data-platform/, 'platform.ts writes data-platform onto <html>');
assert.match(platform, /export function usePlatform/, 'usePlatform is exported');

const routes = readFileSync(new URL('../src/shell/routes.ts', import.meta.url), 'utf8');
assert.match(routes, /export const MOBILE_DESTINATIONS/, 'routes.ts exports MOBILE_DESTINATIONS');
assert.match(routes, /MOBILE_DESTINATIONS: Destination\[\] = \[[\s\S]{0,400}\/settings/, 'MOBILE_DESTINATIONS includes /settings');

const appShell = readFileSync(new URL('../src/shell/AppShell.tsx', import.meta.url), 'utf8');
assert.match(appShell, /platform === 'mobile' \? MOBILE_DESTINATIONS : DESTINATIONS/, "Rail switches to MOBILE_DESTINATIONS on mobile");
assert.match(appShell, /platform !== 'mobile' && \(\s*<Suspense fallback=\{null\}>\s*<Tour/, 'the tour is hidden on mobile');

console.log('pwa: ALL OK');
