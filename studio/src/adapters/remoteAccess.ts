/**
 * A11Y-04 — "acceso remoto sólo con auth + HTTPS o túnel, con aviso claro si
 * no": distinguishing, from the browser's own `location`, whether this page
 * was loaded the safe way (the machine's own loopback, or any host over
 * HTTPS — which covers a reverse proxy/tunnel doing TLS termination) versus
 * plain HTTP to a non-loopback host, e.g. `http://192.168.1.20:7000` typed
 * on a phone on the same LAN — the one case with nothing between a snooped
 * network and the conversation. Pure so it is testable without a real
 * `window.location`.
 */
export function isInsecureRemoteAccess(hostname: string, protocol: string): boolean {
  const host = (hostname || '').toLowerCase();
  const loopback = host === 'localhost' || host === '127.0.0.1' || host === '::1' || host === '[::1]' || host.endsWith('.localhost');
  if (loopback) return false;
  return protocol !== 'https:';
}
