/**
 * Faustus Studio — entry point.
 *
 * `static/index.html` loads this and nothing else: the shell is the
 * interface, not a pilot beside another one. `?gallery=1` still mounts the
 * component gallery instead, which is the review surface for the design
 * system (UI-011).
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

const wantsGallery = new URLSearchParams(window.location.search).has('gallery');

if (!document.querySelector(`link[href="${stylesUrl}"]`)) {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = stylesUrl;
  document.head.appendChild(link);
}
import('./app').then((app) => app.mount(wantsGallery));
