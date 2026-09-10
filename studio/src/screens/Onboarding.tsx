import { CheckCircle2, Compass } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { EmptyState, Skeleton } from '../components';
import { getJson } from '../adapters/api';
import { t } from '../i18n';

/**
 * First run (SET-01).
 *
 * A brand-new install has exactly one useful question on this screen: "what
 * is the next correct thing to do" — never a button that lands on an empty
 * picker. `GET /api/setup/status` (this lote) walks the same checks
 * `src/doctor.py` already runs — no provider, no model, no data folder, no
 * admin account — in the order that actually unblocks the next one (an
 * admin account before a provider before a model: picking a model before
 * there is a provider to pick one FROM is exactly the dead end this screen
 * exists to remove) and names ONE next action.
 *
 * No route is registered for this screen by this lote (`studio/src/shell/AppShell.tsx`
 * is not in this lote's PROPIOS) — see the batch's final report for the
 * one-line change AppShell.tsx needs to reach it at `/setup`.
 */

interface SetupFinding {
  area: string;
  name: string;
  state: string;
  detail: string;
  fix: string;
}
interface SetupStatus {
  blocked: boolean;
  finding: SetupFinding | null;
  action: { label: string; route: string } | null;
}

export default function OnboardingScreen() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    getJson<SetupStatus>('/api/setup/status')
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch((e: Error) => {
        if (!cancelled) setErr(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (err) {
    return (
      <EmptyState
        tone="error"
        icon={Compass}
        title={t('Could not check what is set up yet')}
        body={err}
        primaryAction={{ label: t('Retry'), onClick: () => window.location.reload() }}
      />
    );
  }

  if (!status) {
    return <Skeleton label={t('Checking what is set up')} count={4} />;
  }

  if (!status.blocked || !status.action) {
    return (
      <EmptyState
        icon={CheckCircle2}
        title={t('Faustus is ready')}
        body={t('An admin account, a model provider and the data folder are all in place. Start a chat, or open Diagnostics for the full picture.')}
        primaryAction={{ label: t('Go to Home'), onClick: () => navigate('/') }}
        secondaryAction={{ label: t('Open Diagnostics'), onClick: () => navigate('/settings') }}
      />
    );
  }

  return (
    <EmptyState
      icon={Compass}
      title={t('One thing left before Faustus is ready')}
      body={status.finding?.detail ?? status.action.label}
      primaryAction={{
        label: status.action.label,
        onClick: () => navigate(status.action!.route),
      }}
    />
  );
}
