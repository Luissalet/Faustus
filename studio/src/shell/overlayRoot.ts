/**
 * Where dialogs, menus, popovers and the palette are portalled.
 *
 * Found the hard way: while the pilot is mounted, everything under <body>
 * except the Studio root is hidden so the legacy tree stops painting. Radix
 * and cmdk portal into <body> by default, so the rule was hiding every
 * overlay we opened — Ctrl+K appeared to do nothing at all.
 *
 * One dedicated body-level container, exempted by id in shell.css, fixes it
 * for every overlay at once and keeps the exemption explicit instead of
 * scattering `:not(...)` selectors.
 */

const ID = 'fs-overlay-root';

export function ensureOverlayRoot(): HTMLElement {
  const existing = document.getElementById(ID);
  if (existing) return existing;
  const created = document.createElement('div');
  created.id = ID;
  // The overlay root sits outside .fs-shell, so without this it inherits the
  // legacy stylesheet: the palette came up in the old monospace face with the
  // old input underline. Carrying the base class means every portalled
  // surface gets Studio's font, colours and focus ring.
  created.className = 'fs-app';
  document.body.appendChild(created);
  return created;
}

export function overlayRoot(): HTMLElement | undefined {
  return document.getElementById(ID) ?? undefined;
}

export function removeOverlayRoot(): void {
  document.getElementById(ID)?.remove();
}
