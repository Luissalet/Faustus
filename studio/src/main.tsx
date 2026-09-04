/**
 * Faustus Studio — entry point.
 *
 * Two mounts, both explicit:
 *
 *   ?gallery=1                     → the component gallery (UI-011 review)
 *   flag `faustus_studio_shell`    → the AppShell (UI-021)
 *
 * With neither, this file does nothing at all. That is deliberate: the
 * bundle can be loaded on the legacy page without touching it, and the
 * rollback is a reload with the flag off.
 *
 * This file stays tiny on purpose: it links the stylesheet and does one
 * dynamic import. Everything else lives in hashed chunks — see app.tsx for
 * why that is load-bearing and not tidiness.
 *
 * The stylesheet is imported as a URL, not as CSS: Vite then emits it with
 * a content hash in its name and hands back the path, and the <link> is
 * written here. index.html used to link `assets/index.css?v=…` itself, and
 * Vite's chunk loader — which knows the file by its bare name — added a
 * second copy, so every rule applied twice and the later copy won ties.
 */

import stylesUrl from './styles/index.css?url';
import { isStudioEnabled } from './shell/flag';

const wantsGallery = new URLSearchParams(window.location.search).has('gallery');

if (wantsGallery || isStudioEnabled()) {
  if (!document.querySelector(`link[href="${stylesUrl}"]`)) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = stylesUrl;
    document.head.appendChild(link);
  }
  import('./app').then((app) => app.mount(wantsGallery));
}
