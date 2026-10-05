import { useEffect, useState, useRef } from 'react';
import { Check, Copy, Pause, Play, RotateCcw, X } from 'lucide-react';
import { t, useLang } from '../../i18n';
import { parseQuickTool, type QuickTool } from './quick-tools';
import './quick-tools.css';

interface Props { draft: string; onInsert: (text: string) => void; onNotice: (text: string, tone?: 'warning' | 'info' | 'danger') => void }

export function QuickTools({ draft, onInsert, onNotice }: Props) {
  const lang = useLang();
  const [tool, setTool] = useState<QuickTool | null>(null);
  const [parsedDraft, setParsedDraft] = useState('');
  const [dismissed, setDismissed] = useState('');
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  const [timer, setTimer] = useState<{ total: number; remaining: number; running: boolean } | null>(null);
  const deadline = useRef(0);
  const finished = useRef(false);
  const notice = useRef(onNotice);
  notice.current = onNotice;
  useEffect(() => {
    const pending = setTimeout(() => { setTool(parseQuickTool(draft)); setParsedDraft(draft); }, 180);
    return () => clearTimeout(pending);
  }, [draft]);
  useEffect(() => {
    if (!timer?.running) return;
    const interval = setInterval(() => {
      const remaining = Math.max(0, deadline.current - performance.now());
      setTimer(current => current ? { ...current, remaining, running: remaining > 0 } : null);
      if (remaining === 0 && !finished.current) {
        finished.current = true;
        notice.current(t('Timer finished'), 'info');
      }
    }, 250);
    return () => clearInterval(interval);
  }, [timer?.running]);
  const format = (value: number) => value.toLocaleString(lang === 'es' ? 'es-ES' : 'en-US', { maximumFractionDigits: 8 });
  const active = timer ?? (tool?.kind === 'timer' ? { total: tool.seconds * 1000, remaining: tool.seconds * 1000, running: false } : null);
  if ((!tool || dismissed === draft || parsedDraft !== draft) && !timer) return null;
  const kind = timer ? 'timer' : tool!.kind;
  const close = () => { setDismissed(draft); setTimer(null); };
  const copy = async (text: string) => {
    try { await navigator.clipboard.writeText(text); onNotice(t('Copied'), 'info'); }
    catch { onNotice(t('Copy is unavailable. Use the result in your message.'), 'warning'); }
  };
  let result = '';
  if (tool?.kind === 'calculation' && tool.value !== undefined) result = format(tool.value);
  if (tool?.kind === 'conversion') result = `${format(tool.value)} ${tool.from} = ${format(tool.result)} ${tool.to}`;
  if (tool?.kind === 'split') {
    const money = (cents: number) => `${(cents / 100).toFixed(2)} ${tool.currency}`;
    result = tool.extra ? `${tool.extra} × ${money(tool.base + 1)} + ${tool.people - tool.extra} × ${money(tool.base)}` : `${tool.people} × ${money(tool.base)}`;
  }
  if (tool?.kind === 'color') result = tool.hex;
  if (tool?.kind === 'list') result = tool.items.map((item, index) => `- [${checks[`${index}:${item}`] ? 'x' : ' '}] ${item}`).join('\n');
  const seconds = Math.ceil((active?.remaining ?? 0) / 1000);
  return <section className="fs-quick-tool" aria-label={t('Quick tools')} data-testid="quick-tool" data-kind={kind}>
    <div className="fs-quick-tool__head"><strong>{t({ calculation: 'Calculation', conversion: 'Unit conversion', timer: 'Timer', list: 'Checklist', color: 'Color', split: 'Split equally' }[kind])}</strong>
      <button type="button" className="fs-quick-tool__icon" onClick={close} aria-label={t('Close quick tool')}><X size={14} /></button></div>
    {kind === 'timer' && active ? <>
      <div className="fs-quick-tool__timer"><output role="timer" aria-live="off">{Math.floor(seconds / 3600) > 0 ? `${Math.floor(seconds / 3600)}:` : ''}{String(Math.floor(seconds % 3600 / 60)).padStart(2, '0')}:{String(seconds % 60).padStart(2, '0')}</output>
        <button type="button" onClick={() => {
          if (active.running) setTimer({ ...active, remaining: Math.max(0, deadline.current - performance.now()), running: false });
          else { const remaining = active.remaining || active.total; deadline.current = performance.now() + remaining; finished.current = false; setTimer({ ...active, remaining, running: true }); }
        }}>{active.running ? <Pause size={14} /> : <Play size={14} />}{t(active.running ? 'Pause' : 'Start')}</button>
        <button type="button" onClick={() => { finished.current = false; setTimer({ ...active, remaining: active.total, running: false }); }} aria-label={t('Reset timer')}><RotateCcw size={14} /></button></div>
      <p className="fs-quick-tool__hint">{t('Runs while this view is open.')}</p>
    </> : tool?.kind === 'list' ? <ul className="fs-quick-tool__list">{tool.items.map((item, index) => {
      const key = `${index}:${item}`;
      return <li key={key}><label><input type="checkbox" checked={!!checks[key]} onChange={event => setChecks(old => ({ ...old, [key]: event.target.checked }))} /><span>{item}</span></label></li>;
    })}</ul> : tool?.kind === 'color' ? <div className="fs-quick-tool__color">
      <input type="color" value={tool.hex} onChange={event => onInsert(`color ${event.target.value}`)} aria-label={t('Choose color')} />
      <output style={{ background: tool.hex, color: tool.ink }}>{tool.hex.toUpperCase()}</output>
      <span>{t('Text contrast')}: {tool.contrast.toFixed(2)}:1</span></div>
      : tool?.kind === 'calculation' && tool.error ? <p role="status">{t(tool.error)}</p>
      : <output className="fs-quick-tool__result">{result}</output>}
    {kind !== 'timer' && result && <div className="fs-quick-tool__actions">
      <button type="button" onClick={() => onInsert(result)}><Check size={14} />{t('Use in message')}</button>
      <button type="button" onClick={() => void copy(result)}><Copy size={14} />{t('Copy')}</button>
    </div>}
  </section>;
}
