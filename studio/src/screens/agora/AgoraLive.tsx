import {useCallback, useEffect, useRef} from 'react';

/** Share resolved Studio tokens with the Hub frame, including live theme changes. */
export function AgoraLive({spaceId, className}: {spaceId: string; className?: string}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const shareTheme = useCallback((origin = '*') => {
    const style = getComputedStyle(document.documentElement);
    const names: Record<string, string> = {
      canvas: 'canvas', surface: 'surface-1', sunken: 'surface-2', hover: 'surface-3',
      text: 'text-1', muted: 'text-2', line: 'border', strong: 'border-strong',
      accent: 'brand', focus: 'focus', error: 'danger', 'error-bg': 'surface-2',
    };
    const tokens = Object.fromEntries(Object.entries(names).map(([name, token]) => [name, style.getPropertyValue('--fs-' + token).trim()]));
    tokens.scheme = style.colorScheme;
    frame.current?.contentWindow?.postMessage({type: 'aglive/theme', tokens}, origin);
  }, []);
  useEffect(() => {
    const observer = new MutationObserver(() => shareTheme());
    observer.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme', 'class', 'style']});
    const media = matchMedia('(prefers-color-scheme: light)');
    const changed = () => shareTheme();
    const ready = (event: MessageEvent) => {
      if (event.source === frame.current?.contentWindow && event.data?.type === 'aglive/ready') shareTheme(event.origin);
    };
    media.addEventListener('change', changed);
    window.addEventListener('message', ready);
    return () => {
      observer.disconnect();
      media.removeEventListener('change', changed);
      window.removeEventListener('message', ready);
    };
  }, [shareTheme]);
  return <iframe ref={frame} className={className} title="En directo" onLoad={() => shareTheme()} src={'/api/hoard/agora/open?space_id=' + encodeURIComponent(spaceId) + '&embedded=1&live=1'} />;
}
