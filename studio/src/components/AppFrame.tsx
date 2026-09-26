import { useEffect, useState } from 'react';
import { t } from '../i18n';

/** A snippet becomes a whole page; a whole page stays as it is. */
function runnableDoc(code: string): string {
  if (/<html[\s>]/i.test(code) || /<!doctype/i.test(code)) return code;
  return '<!doctype html><html><head><meta charset="utf-8"></head><body>' + code + '</body></html>';
}

/**
 * A runnable block's frame. The page it loads (`/api/sandbox/app`) is served
 * with its own content policy: its inline code may run, nothing may load from
 * the network, and it has an opaque origin, so a generated app works offline
 * and cannot read Faustus or send anything anywhere. (As `srcdoc` the frame
 * inherited Studio's own policy and no script ever ran.) The runner says
 * when it is ready; the app's HTML is posted to it once.
 */
/** Cheap content key: a new text remounts the frame, which asks again. */
function keyOf(code: string): string {
  let h = 5381;
  for (let i = 0; i < code.length; i++) h = ((h << 5) + h + code.charCodeAt(i)) | 0;
  return `${code.length}:${h}`;
}

export function AppFrame({ code, className = 'fs-rich__app', title }: { code: string; className?: string; title?: string }) {
  const [frame, setFrame] = useState<HTMLIFrameElement | null>(null);
  useEffect(() => {
    if (!frame) return;
    const onMessage = (e: MessageEvent) => {
      if (e.source !== frame.contentWindow || !(e.data && e.data.faustusAppReady)) return;
      frame.contentWindow?.postMessage({ faustusApp: runnableDoc(code) }, '*');
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [frame, code]);
  return <iframe key={keyOf(code)} ref={setFrame} className={className} sandbox="allow-scripts allow-modals allow-forms" src="/api/sandbox/app" title={title ?? t('App preview')} />;
}

