'use strict';
// Which Faustus this window attaches to.
//
// On this PC it is `http://127.0.0.1:<FAUSTUS_PORT>` and the app starts that
// server itself. When the Hoard Hub moved Faustus to a Spark ("Run on"), it
// leaves `data/runtime/placement.json` behind and `server_runtime.py start`
// refuses to run a second copy here (`placed_elsewhere()`); the same file
// names the address Faustus answers at meanwhile, and that is where the
// window, the tray and the alerts go. Read with the same rule as the
// launcher, so the two never disagree. `FAUSTUS_DESKTOP_URL` forces an
// address (a second window on a test instance).
const {readFileSync} = require('node:fs');
const {join} = require('node:path');

function readPlacement(root) {
  try {
    const where = JSON.parse(readFileSync(join(root, 'data', 'runtime', 'placement.json'), 'utf8'));
    return where && typeof where === 'object' && !Array.isArray(where) ? where : null;
  } catch { return null; }
}

function httpOrigin(value) {
  try {
    const url = new URL(String(value || ''));
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.origin : '';
  } catch { return ''; }
}

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]']);

/** {origin, remote, where}: `remote` means the server is not this app's to start or stop. */
function resolveTarget({env = {}, placement = null, port = 7000} = {}) {
  const forced = httpOrigin(env.FAUSTUS_DESKTOP_URL);
  if (forced) return {origin: forced, remote: true, where: LOOPBACK.has(new URL(forced).hostname) ? '' : new URL(forced).hostname};
  const local = {origin: `http://127.0.0.1:${port}`, remote: false, where: ''};
  if (!placement || [undefined, null, '', 'pc'].includes(placement.location)) return local;
  const moved = httpOrigin(placement.urls && placement.urls.faustus);
  // Moved but with no address yet: the launcher's own refusal is the message to show.
  return moved ? {origin: moved, remote: true, where: String(placement.node || '') || new URL(moved).hostname} : local;
}

module.exports = {readPlacement, resolveTarget, httpOrigin};
