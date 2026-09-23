import { AlertTriangle, Check, Download, GitMerge, Plus, Sparkles, Upload, X, Zap } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  addInstinct,
  confirmInstinct,
  contradictInstinct,
  deleteInstinct,
  DOMAINS,
  downloadJson,
  evolveInstincts,
  exportInstincts,
  importInstincts,
  instinctsStatus,
  listInstincts,
  promoteInstincts,
  retireInstinct,
  type EvolveCluster,
  type Instinct,
  type InstinctDomain,
  type InstinctsStatus,
  type PromotionCandidate,
} from '../../adapters/instincts';
import { t, tn } from '../../i18n';
import '../settings.css';

/**
 * Instincts (`src/instincts.py`) — small "when X, do Y" patterns the
 * assistant noticed from a correction, or that were added by hand. Grouped
 * by scope (project-scoped ones the assistant is still building confidence
 * in vs. globally-confirmed ones), each with the evidence behind it.
 */

const DOMAIN_LABEL: Record<InstinctDomain, string> = {
  'code-style': 'Code style',
  workflow: 'Workflow',
  testing: 'Testing',
  tooling: 'Tooling',
  communication: 'Communication',
  debugging: 'Debugging',
  other: 'Other',
};

function ConfidenceBar({ pct }: { pct: number }) {
  const tone = pct >= 80 ? 'ok' : pct >= 50 ? 'warn' : 'bad';
  return (
    <span className="fs-set__help" data-tone={tone} title={t('Effective confidence — decays over time without new evidence.')}>
      <span style={{ display: 'inline-block', width: 60, height: 6, background: 'var(--fs-border)', borderRadius: 3, overflow: 'hidden', verticalAlign: 'middle', marginInlineEnd: 6 }}>
        <span style={{ display: 'block', height: '100%', width: `${Math.max(2, Math.min(100, pct))}%`, background: 'currentColor' }} />
      </span>
      {pct}%
    </span>
  );
}

function InstinctRow({ inst, busy, onConfirm, onContradict, onRetire, onDelete }: {
  inst: Instinct;
  busy: string;
  onConfirm: () => void;
  onContradict: () => void;
  onRetire: () => void;
  onDelete: () => void;
}) {
  const pct = Math.round((inst.effective_confidence ?? inst.confidence) * 100);
  return (
    <li className="fs-set__row" data-testid={`instinct-row-${inst.id}`}>
      <span className="fs-tools__text" style={{ flex: 1 }}>
        <strong>{inst.trigger}</strong> <span className="fs-set__help">→ {inst.action}</span>
        <span className="fs-set__help">
          <span className="fs-chip" data-on style={{ marginInlineEnd: 6 }}>{t(DOMAIN_LABEL[inst.domain] ?? inst.domain)}</span>
          <ConfidenceBar pct={pct} />
          {' · '}{tn(inst.evidence.length, '{n} piece of evidence', '{n} pieces of evidence')}
          {inst.project_name ? ` · ${inst.project_name}` : ''}
        </span>
      </span>
      <span className="fs-set__row-actions">
        <Button variant="ghost" size="sm" icon={Check} label={t('Confirm')} loading={busy === `confirm:${inst.id}`} onClick={onConfirm} />
        <Button variant="ghost" size="sm" icon={X} label={t('Contradict')} loading={busy === `contradict:${inst.id}`} onClick={onContradict} />
        <Button variant="ghost" size="sm" label={t('Retire')} loading={busy === `retire:${inst.id}`} onClick={onRetire} />
        <Button variant="danger" size="sm" label={t('Delete')} loading={busy === `delete:${inst.id}`} onClick={onDelete} />
      </span>
    </li>
  );
}

function AddInstinctForm({ onAdd, busy }: { onAdd: (trigger: string, action: string, domain: InstinctDomain) => Promise<void>; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const [trigger, setTrigger] = useState('');
  const [action, setAction] = useState('');
  const [domain, setDomain] = useState<InstinctDomain>('other');
  if (!open) {
    return <Button variant="secondary" size="sm" icon={Plus} label={t('Add instinct')} onClick={() => setOpen(true)} />;
  }
  return (
    <div className="fs-set__form" data-testid="instinct-add-form">
      <div className="fs-set__grid2">
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="inst-trigger">{t('When (trigger)')}</label>
          <input id="inst-trigger" className="fs-field" value={trigger} onChange={(e) => setTrigger(e.target.value)} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="inst-action">{t('Do (action)')}</label>
          <input id="inst-action" className="fs-field" value={action} onChange={(e) => setAction(e.target.value)} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="inst-domain">{t('Domain')}</label>
          <select id="inst-domain" className="fs-field" value={domain} onChange={(e) => setDomain(e.target.value as InstinctDomain)}>
            {DOMAINS.map((d) => (
              <option key={d} value={d}>{t(DOMAIN_LABEL[d])}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="fs-set__row-actions">
        <Button
          variant="primary"
          size="sm"
          label={t('Add')}
          loading={busy}
          disabled={busy || !trigger.trim() || !action.trim()}
          onClick={() => void onAdd(trigger.trim(), action.trim(), domain).then(() => { setTrigger(''); setAction(''); setOpen(false); })}
        />
        <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setOpen(false)} />
      </div>
    </div>
  );
}

export function InstinctsPanel({ say }: { say: (t: string) => void }) {
  const [items, setItems] = useState<Instinct[] | null>(null);
  const [status, setStatus] = useState<InstinctsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState('');

  const [promotePreview, setPromotePreview] = useState<PromotionCandidate[] | null>(null);
  const [evolvePreview, setEvolvePreview] = useState<EvolveCluster[] | null>(null);
  const [importText, setImportText] = useState('');
  const [importOpen, setImportOpen] = useState(false);

  const reload = async () => {
    try {
      const [list, st] = await Promise.all([listInstincts(), instinctsStatus()]);
      setItems(list);
      setStatus(st);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => {
    void reload();
  }, []);

  const grouped = useMemo(() => {
    const project = (items ?? []).filter((i) => i.scope === 'project');
    const global = (items ?? []).filter((i) => i.scope === 'global');
    return { project, global };
  }, [items]);

  const run = async (key: string, fn: () => Promise<unknown>, okMsg?: string) => {
    setBusy(key);
    try {
      await fn();
      await reload();
      if (okMsg) say(okMsg);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const previewPromote = async () => {
    setBusy('promote-preview');
    try {
      const out = await promoteInstincts({ dryRun: true });
      setPromotePreview(out.candidates ?? []);
      if (!(out.candidates ?? []).length) say(t('Nothing is eligible to promote yet.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const applyPromote = () => run('promote-apply', async () => {
    const out = await promoteInstincts({ dryRun: false });
    setPromotePreview(null);
    say(tn((out.promoted ?? []).length, 'Promoted {n} instinct to global', 'Promoted {n} instincts to global'));
  });

  const previewEvolve = async () => {
    setBusy('evolve-preview');
    try {
      const out = await evolveInstincts({ generate: false });
      setEvolvePreview(out.clusters);
      if (!out.clusters.length) say(t('No cluster is big enough yet.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const applyEvolve = () => run('evolve-apply', async () => {
    const out = await evolveInstincts({ generate: true });
    setEvolvePreview(null);
    say(tn(out.created_skill_ids.length, 'Drafted {n} skill', 'Drafted {n} skills'));
  });

  const doExport = async () => {
    try {
      const text = await exportInstincts();
      downloadJson('instincts.json', text);
    } catch (e) {
      say((e as Error).message);
    }
  };

  const doImport = () => run('import', async () => {
    const out = await importInstincts(importText);
    setImportText('');
    setImportOpen(false);
    say(t('Imported {n}', { n: out.imported ?? 0 }));
  });

  if (error && !items) {
    return <EmptyState icon={Sparkles} title={t('Instincts could not be loaded')} body={error} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />;
  }

  return (
    <div className="fs-set__section" data-testid="skills-instincts">
      <header className="fs-set__section-head">
        <div>
          <p className="fs-prose">
            {t('Patterns the assistant noticed from your corrections, or added by hand. Confirming one raises its confidence; contradicting it lowers it. Project-scoped ones seen in enough projects can be promoted to global.')}
          </p>
        </div>
        <AddInstinctForm busy={busy === 'add'} onAdd={(trigger, action, domain) => run('add', () => addInstinct({ trigger, action, domain }), t('Instinct added.'))} />
      </header>

      {status && (
        <div className="fs-set__card">
          <p className="fs-set__help">
            {tn(status.total, '{n} active instinct', '{n} active instincts')}
            {' · '}
            {Object.entries(status.by_scope).map(([k, v]) => `${k}: ${v}`).join(' · ')}
            {' · '}
            {Object.entries(status.by_domain).map(([k, v]) => `${t(DOMAIN_LABEL[k as InstinctDomain] ?? k)}: ${v}`).join(' · ')}
          </p>
        </div>
      )}

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Project-scoped')}</h3>
        {items === null ? (
          <Skeleton label={t('Loading')} count={2} height="44px" />
        ) : grouped.project.length === 0 ? (
          <p className="fs-set__help">{t('No project-scoped instincts yet.')}</p>
        ) : (
          <ul className="fs-set__list">
            {grouped.project.map((inst) => (
              <InstinctRow
                key={inst.id}
                inst={inst}
                busy={busy}
                onConfirm={() => run(`confirm:${inst.id}`, () => confirmInstinct(inst.id))}
                onContradict={() => run(`contradict:${inst.id}`, () => contradictInstinct(inst.id))}
                onRetire={() => run(`retire:${inst.id}`, () => retireInstinct(inst.id))}
                onDelete={() => run(`delete:${inst.id}`, () => deleteInstinct(inst.id))}
              />
            ))}
          </ul>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Global')}</h3>
        {items === null ? null : grouped.global.length === 0 ? (
          <p className="fs-set__help">{t('No global instincts yet.')}</p>
        ) : (
          <ul className="fs-set__list">
            {grouped.global.map((inst) => (
              <InstinctRow
                key={inst.id}
                inst={inst}
                busy={busy}
                onConfirm={() => run(`confirm:${inst.id}`, () => confirmInstinct(inst.id))}
                onContradict={() => run(`contradict:${inst.id}`, () => contradictInstinct(inst.id))}
                onRetire={() => run(`retire:${inst.id}`, () => retireInstinct(inst.id))}
                onDelete={() => run(`delete:${inst.id}`, () => deleteInstinct(inst.id))}
              />
            ))}
          </ul>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title fs-tools__cat">
          <span><GitMerge size={14} aria-hidden="true" /> {t('Promote eligible')}</span>
          <Button variant="secondary" size="sm" label={t('Preview')} loading={busy === 'promote-preview'} onClick={() => void previewPromote()} />
        </h3>
        <p className="fs-set__help">{t('Merges the same project-scoped pattern seen in enough projects, with high enough confidence, into one global instinct.')}</p>
        {promotePreview && (
          promotePreview.length === 0 ? (
            <p className="fs-set__help">{t('Nothing eligible.')}</p>
          ) : (
            <>
              <ul className="fs-set__list">
                {promotePreview.map((c) => (
                  <li key={c.base} className="fs-set__row">
                    <span className="fs-tools__text">
                      <strong>{c.base}</strong>
                      <span className="fs-set__help">{t('{n} projects · mean confidence {pct}%', { n: c.projects.length, pct: Math.round(c.mean_confidence * 100) })}</span>
                    </span>
                  </li>
                ))}
              </ul>
              <Button variant="primary" size="sm" label={t('Promote all eligible')} loading={busy === 'promote-apply'} onClick={() => void applyPromote()} />
            </>
          )
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title fs-tools__cat">
          <span><Zap size={14} aria-hidden="true" /> {t('Evolve → draft skill')}</span>
          <Button variant="secondary" size="sm" label={t('Preview clusters')} loading={busy === 'evolve-preview'} onClick={() => void previewEvolve()} />
        </h3>
        <p className="fs-set__help">{t('Groups instincts that share keywords and suggests a skill, command or agent to write from them. Generating only ever creates drafts.')}</p>
        {evolvePreview && (
          evolvePreview.length === 0 ? (
            <p className="fs-set__help">{t('No cluster is big enough yet.')}</p>
          ) : (
            <>
              <ul className="fs-set__list">
                {evolvePreview.map((c, i) => (
                  <li key={i} className="fs-set__row">
                    <span className="fs-tools__text">
                      <strong>{c.title || c.keywords.slice(0, 4).join(', ')}</strong>
                      <span className="fs-set__help">
                        {tn(c.instinct_ids.length, '{n} instinct', '{n} instincts')} · {t('suggested')}: {c.suggested} · {Math.round(c.mean_confidence * 100)}%
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
              <Button variant="primary" size="sm" icon={Sparkles} label={t('Generate drafts')} loading={busy === 'evolve-apply'} onClick={() => void applyEvolve()} />
            </>
          )
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Export / import')}</h3>
        <div className="fs-set__row-actions">
          <Button variant="secondary" size="sm" icon={Download} label={t('Export')} onClick={() => void doExport()} />
          <Button variant="secondary" size="sm" icon={Upload} label={t('Import')} onClick={() => setImportOpen((v) => !v)} />
        </div>
        {importOpen && (
          <div className="fs-set__form">
            <textarea className="fs-field fs-set__pre" rows={4} placeholder={t('Paste exported JSON…')} value={importText} onChange={(e) => setImportText(e.target.value)} />
            <Button variant="primary" size="sm" label={t('Import')} loading={busy === 'import'} disabled={!importText.trim()} onClick={() => void doImport()} />
          </div>
        )}
      </div>
      {status && status.pending_promotions.length > 0 && (
        <p className="fs-set__help" data-tone="warn">
          <AlertTriangle size={12} aria-hidden="true" /> {tn(status.pending_promotions.length, '{n} pattern is eligible for promotion.', '{n} patterns are eligible for promotion.')}
        </p>
      )}
    </div>
  );
}
