/**
 * `beforeinstallprompt` (lot P-B): Chrome/Edge/Android fire this once,
 * early, and only ever hand the deferred prompt to whichever listener was
 * already attached — so it is captured in `main.tsx`, before the Settings
 * screen (which is the only place that ever calls `promptInstall()`) has
 * necessarily mounted. iOS Safari never fires it at all; there the "This
 * device" screen falls back to the manual "Share → Add to Home Screen"
 * hint instead of a button.
 */

type BeforeInstallPromptEvent = Event & {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>;
};

let deferredEvent: BeforeInstallPromptEvent | null = null;
let installed = false;
const listeners = new Set<() => void>();

function notify() {
  for (const listener of listeners) listener();
}

/** Call once, as early as possible (main.tsx) — a listener attached after
 * the event already fired never sees it. */
export function captureInstallPrompt(): void {
  if (typeof window === 'undefined') return;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredEvent = e as BeforeInstallPromptEvent;
    notify();
  });
  window.addEventListener('appinstalled', () => {
    installed = true;
    deferredEvent = null;
    notify();
  });
  // Already running as the installed app (this launch, or a previous
  // install the browser remembers) — no point ever offering the button.
  if (window.matchMedia?.('(display-mode: standalone)').matches || (navigator as Navigator & { standalone?: boolean }).standalone) {
    installed = true;
  }
}

export function canInstall(): boolean {
  return !installed && deferredEvent !== null;
}

export function isInstalled(): boolean {
  return installed;
}

/** Triggers the native install dialog. Resolves to whether the person
 * accepted — the prompt event is single-use either way, so the button
 * that called this should hide itself once it resolves regardless. */
export async function promptInstall(): Promise<boolean> {
  const event = deferredEvent;
  if (!event) return false;
  deferredEvent = null;
  notify();
  await event.prompt();
  const { outcome } = await event.userChoice;
  return outcome === 'accepted';
}

export function subscribeInstallPrompt(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** iOS Safari (and any other browser that never fires
 * `beforeinstallprompt`): true when it's worth showing the manual "Share →
 * Add to Home Screen" hint instead of a button — a browser tab, not
 * already installed, on a platform with no native prompt to offer. */
export function needsManualInstallHint(): boolean {
  if (typeof navigator === 'undefined' || installed || canInstall()) return false;
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) && !(window as unknown as { MSStream?: unknown }).MSStream;
  const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
  return isIOS && isSafari;
}
