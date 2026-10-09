import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { coordinationState, getSessionCoordination, type CoordinationView } from '../../adapters/coordination';
import { t, tn } from '../../i18n';
import '../project/coordination.css';

export function CoordinationStatus({ sessionId, projectId, busy }: { sessionId: string | null; projectId?: string; busy: boolean }) {
  const [view, setView] = useState<CoordinationView | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setView(null); setFailed(false);
    if (!sessionId || !projectId) return () => controller.abort();
    let reading = false;
    const load = async () => {
      if (reading || cancelled) return;
      reading = true;
      try {
        const result = await getSessionCoordination(sessionId, controller.signal);
        if (!cancelled) { setView(result); setFailed(false); }
      } catch { if (!cancelled) setFailed(true); }
      finally { reading = false; }
    };
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load(); }, 15000);
    const focused = () => { void load(); };
    window.addEventListener('focus', focused);
    return () => { cancelled = true; controller.abort(); window.clearInterval(timer); window.removeEventListener('focus', focused); };
  }, [sessionId, projectId, busy]);
  if (!projectId) return null;
  const state = view ? coordinationState(view) : null;
  const labels = {
    not_configured: 'Agora coordination is not configured', disabled: 'Agora coordination is disabled',
    unavailable: 'Agora coordination is unavailable', prepared: 'Project coordination is saved', available: 'Agora coordination is available',
  } as const;
  return <div className="fs-coordination-status" data-testid="studio-coordination" data-state={failed ? 'unavailable' : state ?? 'unknown'}>
    <span>{t('Faustus')}{' · '}{failed ? t('Coordination status could not be checked') : state ? t(labels[state]) : t('Checking project coordination…')}</span>
    {view?.pending && view.pending.length > 0 && <span>{tn(view.pending.length, '{n} coordination message pending', '{n} coordination messages pending')}</span>}
    <Link to={`/projects/${encodeURIComponent(projectId)}`}>{t('Project configuration')}</Link>
  </div>;
}
