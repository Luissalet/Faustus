import { useCallback, useEffect, useState } from 'react';
import { GitCompare, PenLine } from 'lucide-react';
import { Button } from '../../components';
import {
  addCanvasDecision, canvasDiff, canvasHistory, listCanvases,
  type CanvasChange, type CanvasDiff, type CanvasHistory, type CanvasRow,
} from '../../adapters/designCanvas';
import { relativeTime } from '../../adapters/home';
import { t, tn } from '../../i18n';
import './design-canvas.css';

/**
 * Darwin #989: the design canvases filed for this project (OBJ-30), each with
 * its numbered versions, a semantic diff between any two of them (elements
 * added, removed, reworded, moved to another section, reordered — not a JSON
 * diff) and the decision log: who decided what, why, on which version.
 */

const DIMENSIONS: Record<string, string> = {
  requirements: 'Requirements', entities: 'Entities', approach: 'Approach', structure: 'Structure',
  operations: 'Operations', norms: 'Norms', safeguards: 'Safeguards',
};
const KIND_LABEL: Record<CanvasChange['kind'], string> = {
  added: 'added', removed: 'removed', changed: 'changed', moved: 'moved', reordered: 'reordered',
};

function ChangeRow({ c }: { c: CanvasChange }) {
  const dim = t(DIMENSIONS[c.dimension ?? ''] ?? c.dimension ?? '');
  return (
    <li className="fs-dc__change" data-kind={c.kind} data-testid="design-canvas-change">
      <span className="fs-dc__kind">{t(KIND_LABEL[c.kind])}</span>
      {c.kind === 'moved' ? (
        <span className="fs-dc__dim">{t(DIMENSIONS[c.from_dimension ?? ''] ?? '')} → {t(DIMENSIONS[c.to_dimension ?? ''] ?? '')}</span>
      ) : (
        <span className="fs-dc__dim">{dim}{c.kind === 'reordered' ? ` · #${(c.from_index ?? 0) + 1} → #${(c.to_index ?? 0) + 1}` : ''}</span>
      )}
      {c.kind === 'changed' ? (
        <span className="fs-dc__text"><del>{c.before}</del><ins>{c.after}</ins></span>
      ) : (
        <span className="fs-dc__text">{c.text}</span>
      )}
    </li>
  );
}

function CanvasDetail({ id, onChanged }: { id: string; onChanged: () => void }) {
  const [hist, setHist] = useState<CanvasHistory | null>(null);
  const [from, setFrom] = useState(0);
  const [to, setTo] = useState(0);
  const [diff, setDiff] = useState<CanvasDiff | null>(null);
  const [diffError, setDiffError] = useState('');
  const [decision, setDecision] = useState('');
  const [why, setWhy] = useState('');
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    const next = await canvasHistory(id);
    setHist(next);
    const latest = next.versions[next.versions.length - 1]?.version ?? 0;
    setTo((v) => v || latest);
    setFrom((v) => v || Math.max(0, latest - 1));
  }, [id]);
  useEffect(() => { void reload().catch(() => setHist(null)); }, [reload]);
  useEffect(() => {
    if (!from || !to || from === to) { setDiff(null); return; }
    const controller = new AbortController();
    setDiffError('');
    canvasDiff(id, from, to, controller.signal).then(setDiff).catch((e: Error) => { setDiff(null); setDiffError(e.message); });
    return () => controller.abort();
  }, [id, from, to, hist]);

  const log = async () => {
    if (!decision.trim()) return;
    setSaving(true);
    try {
      await addCanvasDecision(id, { decision: decision.trim(), why: why.trim(), version: to || undefined });
      setDecision(''); setWhy('');
      await reload();
      onChanged();
    } finally {
      setSaving(false);
    }
  };
  if (!hist) return <p className="fs-pj__note">{t('Loading…')}</p>;
  const options = hist.versions.map((v) => <option key={v.version} value={v.version}>v{v.version} · {v.author}</option>);
  return (
    <div className="fs-dc__detail" data-testid="design-canvas-detail">
      <ol className="fs-dc__versions">
        {hist.versions.map((v) => (
          <li key={v.version}>
            <strong>v{v.version}</strong> <span className="fs-pj__note">{v.author} · {relativeTime(v.ts)}</span>
            {v.summary && <span className="fs-dc__summary">{v.summary}</span>}
          </li>
        ))}
      </ol>
      {hist.versions.length > 1 ? (
        <div className="fs-dc__compare">
          <GitCompare size={14} aria-hidden="true" />
          <label>{t('From')} <select value={from} onChange={(e) => setFrom(Number(e.target.value))} data-testid="design-canvas-from">{options}</select></label>
          <label>{t('To')} <select value={to} onChange={(e) => setTo(Number(e.target.value))} data-testid="design-canvas-to">{options}</select></label>
          {diff && <span className="fs-pj__note">{Object.entries(diff.counts).filter(([, n]) => n).map(([k, n]) => `${n} ${t(k)}`).join(' · ') || t('same design')}</span>}
        </div>
      ) : (
        <p className="fs-pj__note">{t('Only one version so far: the diff appears when the canvas is filed again.')}</p>
      )}
      {diffError && <p className="fs-pj__note">{diffError}</p>}
      {diff && diff.changes.length > 0 && <ul className="fs-dc__changes">{diff.changes.map((c, i) => <ChangeRow key={i} c={c} />)}</ul>}
      <h4 className="fs-dc__h">{t('Decision log')}</h4>
      {hist.decisions.length === 0 && <p className="fs-pj__note">{t('No decision logged yet.')}</p>}
      <ul className="fs-dc__decisions" data-testid="design-canvas-decisions">
        {hist.decisions.map((d) => (
          <li key={d.id}><strong>v{d.version}</strong> · {d.author} · {d.decision}{d.why ? <span className="fs-pj__note"> — {t('why')}: {d.why}</span> : null}</li>
        ))}
      </ul>
      <form className="fs-dc__form" onSubmit={(e) => { e.preventDefault(); void log(); }}>
        <input value={decision} onChange={(e) => setDecision(e.target.value)} placeholder={t('What was decided')} aria-label={t('What was decided')} data-testid="design-canvas-decision" />
        <input value={why} onChange={(e) => setWhy(e.target.value)} placeholder={t('Why')} aria-label={t('Why')} />
        <Button size="sm" variant="primary" type="submit" icon={PenLine} label={t('Log on v{n}', { n: to || hist.versions[hist.versions.length - 1]?.version || 1 })} loading={saving} disabled={!decision.trim()} />
      </form>
    </div>
  );
}

/** Project overview: this project's design canvases, their versions, diffs and decisions. */
export function ProjectDesignCanvases({ projectId }: { projectId: string }) {
  const [rows, setRows] = useState<CanvasRow[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const reload = useCallback(() => {
    listCanvases(projectId).then(setRows).catch(() => setRows([]));
  }, [projectId]);
  useEffect(() => { reload(); }, [reload]);
  return (
    <section className="fs-panel fs-dc" data-testid="design-canvases">
      <div className="fs-pj__card-head">
        <h3>{t('Design canvases')}</h3>
        <span className="fs-pj__note">{rows ? tn(rows.length, '{n} canvas', '{n} canvases') : t('Loading…')}</span>
      </div>
      {rows && rows.length === 0 && (
        <p className="fs-pj__note">{t('No design canvas filed yet. The agent files one with design_canvas before a larger change; every new filing becomes a version you can compare here.')}</p>
      )}
      {rows && rows.length > 0 && (
        <ul className="fs-dc__list">
          {rows.map((r) => (
            <li key={r.id} data-open={open === r.id || undefined}>
              <button type="button" className="fs-dc__row" onClick={() => setOpen(open === r.id ? null : r.id)} aria-expanded={open === r.id} data-testid="design-canvas-row">
                <strong>{r.name}</strong>
                <span className="fs-pj__note">v{r.latest} · {tn(r.decisions, '{n} decision', '{n} decisions')}{r.updated ? ` · ${relativeTime(r.updated)}` : ''}</span>
              </button>
              {open === r.id && <CanvasDetail id={r.id} onChanged={reload} />}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
