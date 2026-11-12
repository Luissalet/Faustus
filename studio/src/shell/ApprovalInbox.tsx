import { BellRing, Bot, Check, CheckCheck, ChevronDown, Clock3, MessageSquare, Workflow, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { NavLink } from 'react-router';
import { getJson } from '../adapters/api';
import { Button } from '../components/Button';
import { t, useLang } from '../i18n';
import './approval-inbox.css';

interface Card {
  code: string; title: string; preview: string; level: string; age_seconds: number;
  session_id: string; origin: { kind: string; name?: string };
}
interface Rule { pattern: string; title: string; preview: string; granted: boolean }
interface Inbox { pending: Card[]; rules: Rule[] }

function actionSummary(title: string, preview: string) {
  let detail = preview;
  try {
    const input: unknown = JSON.parse(preview);
    if (input && typeof input === 'object' && !Array.isArray(input)) {
      detail = Object.entries(input).slice(0, 3).map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ');
    }
  } catch { /* Shell commands and plain-text plans already read as actions. */ }
  detail = detail.replace(/\s+/g, ' ').trim();
  if (detail.length > 180) detail = detail.slice(0, 177) + '…';
  return t(['bash', 'python', 'powershell'].includes(title) ? 'Run {tool}: {action}' : 'Use {tool}: {action}', { tool: title, action: detail || title });
}

export function ApprovalBell({ tab = false }: { tab?: boolean } = {}) {
  const [count, setCount] = useState(0);
  useLang();
  useEffect(() => {
    const read = () => { if (!document.hidden) void getJson<Inbox>('/api/approval-inbox').then(data => setCount(data.pending.length)).catch(() => undefined); };
    read(); const timer = window.setInterval(read, 5000);
    return () => window.clearInterval(timer);
  }, []);
  return <NavLink to="/approvals" className={tab ? 'fs-nav__item' : 'fs-nav__tool fs-approval-bell'} data-testid="approval-inbox-bell" title={t('Approval inbox')} aria-label={t('Approval inbox')}>
    <span className={tab ? 'fs-nav__node' : undefined}><BellRing size={15} aria-hidden="true" /></span>{count > 0 && <strong>{count}</strong>}
  </NavLink>;
}

export function ApprovalInbox() {
  useLang();
  const [data, setData] = useState<Inbox>({ pending: [], rules: [] });
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [remember, setRemember] = useState<Record<string, boolean>>({});
  const read = useCallback(async () => {
    try { setData(await getJson<Inbox>('/api/approval-inbox')); setReady(true); setError(''); }
    catch { setError('Could not load the approval inbox.'); }
  }, []);
  useEffect(() => { void read(); const timer = window.setInterval(() => { if (!document.hidden) void read(); }, 5000); return () => window.clearInterval(timer); }, [read]);
  const mutate = async (path: string, method: string, body?: unknown) => {
    setBusy(true); setError('');
    try {
      const response = await fetch('/api/approval-inbox/' + path, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) throw new Error();
      await read();
    } catch { setError('The decision could not be saved. Refresh and try again.'); }
    finally { setBusy(false); }
  };
  const levels: Record<string, string> = { trivial: 'Trivial', low: 'Low', medium: 'Medium', high: 'High' };
  return <section className="fs-approval-inbox" data-testid="approval-inbox">
    <header><h1>{t('Approval inbox')}</h1><p>{t('Pending decisions from conversations, assistants and automations.')}</p></header>
    {error && <div className="fs-approval-inbox__error" role="alert"><p>{t(error)}</p><Button label={t('Retry')} onClick={() => void read()} /></div>}
    {!ready && !error && <p role="status">{t('Loading…')}</p>}
    {ready && data.pending.length === 0 && <div className="fs-approval-inbox__empty" role="status"><CheckCheck size={28} aria-hidden="true" /><h2>{t('No decisions are waiting.')}</h2><p>{t('New requests will appear here. You can keep working in your conversations.')}</p><NavLink to="/studio" className="fs-btn" data-variant="secondary">{t('Open Studio')}</NavLink></div>}
    <ul className="fs-approval-inbox__list">{data.pending.map(card => {
      const OriginIcon = card.origin.kind === 'assistant' ? Bot : card.origin.kind === 'automation' ? Workflow : MessageSquare;
      const originLabel = t(card.origin.kind === 'chat' ? 'Conversation' : card.origin.kind === 'assistant' ? 'Assistant' : card.origin.kind === 'automation' ? 'Automation' : 'Approval');
      return <li key={card.code} className="fs-approval-inbox__card">
      <div className="fs-approval-inbox__meta"><span className="fs-approval-inbox__level" data-level={card.level}>{t(levels[card.level] || card.level)}</span><span className="fs-approval-inbox__age"><Clock3 size={14} aria-hidden="true" />{t('{n} min waiting', { n: Math.floor(card.age_seconds / 60) })}</span><code>{card.code}</code></div>
      <p className="fs-approval-inbox__origin"><OriginIcon size={16} aria-hidden="true" /><span>{originLabel}{card.origin.name && <> · <strong>{card.origin.name}</strong></>}</span></p>
      <h2>{actionSummary(card.title, card.preview)}</h2>
      <details className="fs-approval-inbox__detail"><summary>{t('Full action details')}<ChevronDown size={16} aria-hidden="true" /></summary><pre>{card.preview}</pre></details>
      <label className="fs-approval-inbox__remember"><input type="checkbox" checked={!!remember[card.code]} onChange={event => setRemember(value => ({ ...value, [card.code]: event.target.checked }))} />{t('Remember for this exact pattern')}</label>
      <p className="fs-approval-inbox__scope">{t('Same tool, complete input and workspace. You can edit this answer below.')}</p>
      <div className="fs-approval-inbox__actions"><Button variant="primary" icon={Check} disabled={busy} label={t('Approve')} onClick={() => void mutate(card.code + '/decide', 'POST', { granted: true, remember: !!remember[card.code] })} /><Button icon={X} disabled={busy} label={t('Deny')} onClick={() => void mutate(card.code + '/decide', 'POST', { granted: false, remember: !!remember[card.code] })} />{card.session_id && <NavLink to={'/studio?s=' + encodeURIComponent(card.session_id)}>{t('Open conversation')}</NavLink>}</div>
    </li>})}</ul>
    <details className="fs-approval-inbox__rules"><summary><span>{t('Remembered answers')} <span className="fs-approval-inbox__count">{data.rules.length}</span></span><ChevronDown size={18} aria-hidden="true" /></summary><p>{t('Only exact matches use a remembered answer. Other actions keep their current approval rules.')}</p>
    {!data.rules.length && <p>{t('No remembered answers.')}</p>}
    <ul className="fs-approval-inbox__list">{data.rules.map(rule => <li key={rule.pattern}>
      <h3>{rule.title}</h3><details><summary>{t('Review the proposed action')}</summary><pre>{rule.preview}</pre></details>
      <div className="fs-approval-inbox__actions"><label>{t('Always')} <select aria-label={t('Remembered answer for {tool}', { tool: rule.title })} disabled={busy} value={String(rule.granted)} onChange={event => void mutate('rules/' + rule.pattern, 'PUT', { granted: event.target.value === 'true' })}><option value="true">{t('Approve')}</option><option value="false">{t('Deny')}</option></select></label><Button disabled={busy} label={t('Forget')} onClick={() => void mutate('rules/' + rule.pattern, 'DELETE')} /></div>
    </li>)}</ul></details>
  </section>;
}
