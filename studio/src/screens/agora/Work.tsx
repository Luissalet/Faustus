import {
  Bot, Check, ChevronRight, FilePen, FileText, Globe, Plug, Repeat, Search, SquareTerminal, Wrench, X, type LucideIcon,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { getMessageWork, type MessageRoutine, type MessageWork, type TaskLive, type WorkTool } from '../../adapters/agora';
import { t } from '../../i18n';

/** What an agent did for a message (its tools, in order) and the routine that wrote it, and how far a task's owner is in its turn. */

const ICONS: [RegExp, LucideIcon][] = [
  [/^mcp__|^mcp:|^hub_|^agora_/i, Plug],
  [/bash|shell|exec|powershell|terminal|command|python/i, SquareTerminal],
  [/edit|write|patch|replace|create|notebook/i, FilePen],
  [/read|view|open|cat/i, FileText],
  [/grep|glob|search|find|ls|list/i, Search],
  [/web|fetch|browser|playwright|http|url/i, Globe],
  [/agent|task|spawn|sub/i, Bot],
];
export const iconOf = (name: string): LucideIcon => ICONS.find(([pattern]) => pattern.test(name))?.[1] ?? Wrench;

/** «45 s», «12 min», «1 h 5 min»; empty when nothing was measured. */
export function span(seconds: number | null | undefined): string {
  const s = Math.round(Number(seconds));
  if (!Number.isFinite(s) || s <= 0) return '';
  if (s < 60) return t('{n} s', { n: s });
  if (s < 3600) return t('{n} min', { n: Math.round(s / 60) });
  const minutes = Math.round((s % 3600) / 60);
  return minutes ? t('{h} h {m} min', { h: Math.floor(s / 3600), m: minutes }) : t('{n} h', { n: Math.floor(s / 3600) });
}
const millis = (ms: number | null | undefined) => (ms == null ? '' : ms < 1000 ? t('{n} ms', { n: ms }) : span(ms / 1000) || t('{n} ms', { n: ms }));

const usedLine = (work: MessageWork) => (work.count === 1 ? t('Used 1 tool') : t('Used {n} tools', { n: work.count }));

/** The routine behind a message, next to its author. */
export function RoutineTag({ routine }: { routine: MessageRoutine | null | undefined }) {
  if (!routine || typeof routine.name !== 'string' || !routine.name) return null;
  return (
    <span className="fs-agx-routine" data-testid="agora-routine" data-routine={routine.id} title={routine.schedule || undefined}>
      <Repeat size={11} aria-hidden="true" />{t('Routine “{name}”', { name: routine.name })}
    </span>
  );
}

function ToolRow({ tool }: { tool: WorkTool }) {
  const Icon = iconOf(tool.name);
  return (
    <li className="fs-agx-work__tool" data-testid="agora-work-tool" data-ok={tool.ok === null ? undefined : String(tool.ok)}>
      <Icon size={14} aria-hidden="true" />
      <code>{tool.name}</code>
      {tool.summary && <span className="fs-agx-work__summary" title={tool.summary}>{tool.summary}</span>}
      <span className="fs-agx-work__result">
        {millis(tool.ms) && <span>{millis(tool.ms)}</span>}
        {tool.ok === true && <Check size={13} aria-label={t('Worked')} />}
        {tool.ok === false && <X size={13} aria-label={t('Failed')} />}
      </span>
    </li>
  );
}

/** «Used 5 tools · 12 min» under an agent's message, folded: the icons of what it used, and the list in order when opened. */
export function WorkCard({ work, space, messageId }: { work: MessageWork | null | undefined; space: string; messageId: number }) {
  const [open, setOpen] = useState(false);
  const [whole, setWhole] = useState<WorkTool[] | null>(null);
  const [failed, setFailed] = useState(false);
  const shown = work && Array.isArray(work.tools) ? work.tools : [];
  const more = work?.more ?? 0;
  // A page carries the first tools only: opening a long one asks for the rest.
  useEffect(() => {
    if (!open || !more || whole) return;
    const controller = new AbortController();
    getMessageWork(space, messageId, controller.signal).then((answer) => {
      if (Array.isArray(answer.message?.work?.tools)) setWhole(answer.message.work.tools);
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [open, more, whole, space, messageId]);
  if (!work || typeof work.count !== 'number' || work.count < 1) return null;
  const list = whole ?? shown;
  const icons = [...new Set(shown.map((tool) => tool.name))].slice(0, 6);
  const lasted = span(work.duration_s);
  const id = 'agx-work-' + messageId;
  return (
    <div className="fs-agx-work" data-testid="agora-work" data-open={open || undefined} data-failed={work.failed > 0 || undefined} data-source={work.source}>
      <button type="button" className="fs-agx-work__toggle" aria-expanded={open} aria-controls={id} data-testid="agora-work-toggle" onClick={() => setOpen((v) => !v)}>
        <ChevronRight size={13} className="fs-agx-work__chevron" aria-hidden="true" />
        <span className="fs-agx-work__icons" aria-hidden="true">
          {icons.map((name) => { const Icon = iconOf(name); return <Icon key={name} size={13} />; })}
        </span>
        <span data-testid="agora-work-line">{usedLine(work)}{lasted ? ' · ' + lasted : ''}</span>
        {work.failed > 0 && <span className="fs-agx-work__failed" data-testid="agora-work-failed">{t('{n} failed', { n: work.failed })}</span>}
      </button>
      {open && (
        <div id={id} className="fs-agx-work__body" data-testid="agora-work-list">
          <ol>{list.map((tool, index) => <ToolRow key={index} tool={tool} />)}</ol>
          {more > 0 && !whole && !failed && <p className="fs-muted" aria-live="polite">{t('Loading…')}</p>}
          {more > 0 && !whole && failed && <p className="fs-muted">{t('{n} more tools are not shown.', { n: more })}</p>}
        </div>
      )}
    </div>
  );
}

/** «5 steps · 2 min» on the card of a task being worked on, with a bar of the calls that have finished. Nothing before the first call. */
export function LiveSteps({ live, now }: { live: TaskLive | null | undefined; now: number }) {
  if (!live || !(live.steps > 0)) return null;
  const done = Math.min(Math.max(live.done, 0), live.steps);
  const seconds = live.since ? Math.max(0, now / 1000 - live.since) : live.elapsed_s;
  const lasted = span(seconds) || t('under a minute');
  return (
    <span className="fs-agx-steps" data-testid="agora-live-steps" title={live.tool ? t('Using {tool}', { tool: live.tool }) : undefined}>
      <span className="fs-agx-steps__bar" role="progressbar" aria-label={t('Steps finished')} aria-valuemin={0} aria-valuemax={live.steps} aria-valuenow={done}>
        <i style={{ inlineSize: Math.round((done / live.steps) * 100) + '%' }} />
      </span>
      <span>{live.steps === 1 ? t('1 step') : t('{n} steps', { n: live.steps })} · {lasted}</span>
    </span>
  );
}
