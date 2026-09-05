import { Check, Copy } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { IconButton, Toast } from '../../components';
import { isLocal, selectServer, serverKey, serverLabel, useCookbookState, type Server } from '../../adapters/cookbook';
import { t } from '../../i18n';

/** The one toast the whole screen shares. */
export function useSay(): [string | null, (m: string) => void] {
  const [notice, setNotice] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const say = useCallback((m: string) => {
    setNotice(m);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setNotice(null), 3200);
  }, []);
  return [notice, say];
}

export function Notice({ text }: { text: string | null }) {
  return text ? (
    <Toast>
      <Check size={12} aria-hidden="true" /> {text}
    </Toast>
  ) : null;
}

/** Which server a tab works against: Local plus every saved remote. */
export function ServerPicker({ compact = false }: { compact?: boolean }) {
  const { env } = useCookbookState();
  const current = env.remoteServerKey || (env.remoteHost ? env.servers.find((s) => s.host === env.remoteHost) && serverKey(env.servers.find((s) => s.host === env.remoteHost)!) : 'local') || 'local';
  if (env.servers.length <= 1 && !compact) {
    return <span className="fs-ck__target">{t('Local')} · {env.hostPlatform || t('this machine')}</span>;
  }
  return (
    <label className="fs-ck__picker">
      <span className="fs-ck__label">{t('Server')}</span>
      <select className="fs-field" value={current} onChange={(e) => selectServer(e.target.value)} aria-label={t('Server')} data-testid="cookbook-server">
        {env.servers.map((s) => (
          <option key={serverKey(s)} value={serverKey(s)}>
            {serverLabel(s)}
            {s.platform ? ` · ${s.platform}` : ''}
          </option>
        ))}
      </select>
    </label>
  );
}

export function useSelectedServer(): Server | null {
  const { env } = useCookbookState();
  const s = env.servers.find((x) => serverKey(x) === env.remoteServerKey) ?? (env.remoteHost ? env.servers.find((x) => x.host === env.remoteHost) : null) ?? null;
  return s && !isLocal(s) ? s : (env.servers.find(isLocal) ?? null);
}

export function CopyButton({ text, label, say }: { text: string; label?: string; say: (m: string) => void }) {
  return <IconButton icon={Copy} size="sm" label={label ?? t('Copy')} onClick={() => void navigator.clipboard.writeText(text).then(() => say(t('Copied')))} />;
}

/** A labelled control in the launch and server forms. */
export function Field({ label, hint, children, wide = false }: { label: string; hint?: string; children: ReactNode; wide?: boolean }) {
  return (
    <label className="fs-ck__field" data-wide={wide || undefined} title={hint}>
      <span className="fs-ck__label">{label}</span>
      {children}
    </label>
  );
}

export function Switch({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string }) {
  return (
    <label className="fs-switch" title={hint}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

/** Relative "3 min" style uptime that ticks while the task is live. */
export function Uptime({ since, live }: { since: number; live: boolean }) {
  const [, force] = useState(0);
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => force((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [live]);
  const s = Math.max(0, Math.round((Date.now() - since) / 1000));
  const text = s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` : `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
  return <span className="fs-ck__uptime">{text}</span>;
}

export const FIT_LABEL: Record<string, string> = { perfect: 'Perfect', good: 'Good', marginal: 'Marginal', too_tight: 'Tight', no_fit: 'No fit' };
