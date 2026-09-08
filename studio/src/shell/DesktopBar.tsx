import { useEffect, useState } from 'react';
import { Minus, Square, Copy, Maximize, Minimize, X } from 'lucide-react';
import { t, useLang } from '../i18n';
import './desktop-bar.css';

type WindowState = { maximized: boolean; fullscreen: boolean };
type WindowAction = 'state' | 'minimize' | 'maximize' | 'fullscreen' | 'close';
declare global {
  interface Window {
    faustusWindow?: {
      command(action: WindowAction): Promise<WindowState>;
      subscribe(callback: (state: WindowState) => void): () => void;
    };
  }
}

export function DesktopBar() {
  useLang();
  const bridge = window.faustusWindow;
  const [state, setState] = useState<WindowState>({ maximized: false, fullscreen: false });
  useEffect(() => {
    if (!bridge) return;
    document.documentElement.dataset.desktopWindow = 'on';
    const unsubscribe = bridge.subscribe(setState);
    void bridge.command('state').then(setState);
    return () => { unsubscribe(); delete document.documentElement.dataset.desktopWindow; };
  }, [bridge]);
  if (!bridge) return null;
  const action = (value: WindowAction) => void bridge.command(value).then(setState);
  const Restore = state.maximized ? Copy : Square;
  const Fullscreen = state.fullscreen ? Minimize : Maximize;
  return <header className="fs-app fs-desktop-bar" aria-label={t('Window controls')}>
    <div className="fs-desktop-bar__drag"><span>Faustus</span></div>
    <button title={t('Minimize window')} aria-label={t('Minimize window')} onClick={() => action('minimize')}><Minus size={15} /></button>
    <button title={t(state.maximized ? 'Restore window' : 'Maximize window')} aria-label={t(state.maximized ? 'Restore window' : 'Maximize window')} onClick={() => action('maximize')}><Restore size={14} /></button>
    <button title={t(state.fullscreen ? 'Exit full screen' : 'Full screen')} aria-label={t(state.fullscreen ? 'Exit full screen' : 'Full screen')} onClick={() => action('fullscreen')}><Fullscreen size={15} /></button>
    <button className="fs-desktop-bar__close" title={t('Close window')} aria-label={t('Close window')} onClick={() => action('close')}><X size={17} /></button>
  </header>;
}
