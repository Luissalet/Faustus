import { Briefcase, Check, FileText, Link2, Plus } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState } from '../components';
import * as api from '../adapters/delivery';
import { t } from '../i18n';

/**
 * Commissions (Hangar E29, first cut): scope -> milestone -> delivery for one
 * commission. Scope versions are compared by the scope comparator; a
 * deliverable shows the live "ready" of the delivery package it points at.
 * People and Ledger are referenced, never copied.
 */

function NewCommission({ onCreated }: { onCreated: (c: api.Commission) => void }) {
  const [title, setTitle] = useState('');
  const [client, setClient] = useState('');
  const [req, setReq] = useState('');
  const [error, setError] = useState('');
  return (
    <section className="fs-dlv__block" aria-label={t('New commission')} data-testid="office-new">
      <h2 className="fs-dlv__h">{t('New commission')}</h2>
      <label className="fs-dlv__field">{t('Title')}<input className="fs-field" value={title} onChange={(e) => setTitle(e.target.value)} data-testid="office-title" /></label>
      <label className="fs-dlv__field">{t('Client')}<input className="fs-field" value={client} onChange={(e) => setClient(e.target.value)} data-testid="office-client" /></label>
      <label className="fs-dlv__field">{t('First request (optional)')}<textarea className="fs-field" rows={3} value={req} onChange={(e) => setReq(e.target.value)} data-testid="office-request" /></label>
      {error && <p className="fs-dlv__why" data-tone="bad" role="alert">{error}</p>}
      <div><Button variant="primary" icon={Plus} label={t('Create')} disabled={!title.trim()} testId="office-create"
        onClick={() => { api.createCommission({ title, client, request: req }).then(onCreated).catch((e) => setError((e as Error).message)); }} /></div>
    </section>
  );
}

function Detail({ c, packages, onChange }: { c: api.Commission; packages: api.PackageRow[]; onChange: (c: api.Commission) => void }) {
  const [error, setError] = useState('');
  const [scopeKind, setScopeKind] = useState(c.status.agreement_version ? 'change' : 'agreement');
  const [scopeText, setScopeText] = useState('');
  const [msTitle, setMsTitle] = useState('');
  const [msDue, setMsDue] = useState('');
  const [msAmount, setMsAmount] = useState('');
  const [link, setLink] = useState<Record<string, string>>({});
  const [contact, setContact] = useState('');
  const [role, setRole] = useState('');
  const [file, setFile] = useState('');
  const [decision, setDecision] = useState('');
  const [assumptions, setAssumptions] = useState('');
  const [exclusions, setExclusions] = useState('');
  const [internal, setInternal] = useState('');
  const [proposal, setProposal] = useState<api.Proposal | null>(c.proposal ?? null);
  const act = (action: string, body: Record<string, unknown>, after?: () => void) => {
    setError('');
    api.commissionAction(c.id, action, body).then((r) => { onChange(r); after?.(); }).catch((e) => setError((e as Error).message));
  };
  const st = c.status;
  return (
    <div className="fs-dlv__detail" data-testid="office-detail">
      <div className="fs-dlv__title-row">
        <h2 className="fs-dlv__title">{c.title}</h2>
        <select className="fs-field fs-dlv__stage" value={c.stage} aria-label={t('Stage')} onChange={(e) => act('update', { stage: e.target.value })}>
          {api.STAGES.map((s) => <option key={s} value={s}>{api.stageLabel(s)}</option>)}
        </select>
        {st.overdue.length > 0 && <span className="fs-dlv__pill" data-tone="bad">{t('Overdue')}: {st.overdue.join(', ')}</span>}
      </div>
      <p className="fs-dlv__muted">{c.client || t('No client yet')} · {c.created}{c.budget ? ` · ${c.budget.amount} ${c.budget.currency}` : ''}
        {st.next_milestone ? ` · ${t('Next')}: ${st.next_milestone.id} ${st.next_milestone.title} (${st.next_milestone.due})` : ''}</p>
      {error && <p className="fs-dlv__why" data-tone="bad" role="alert">{error}</p>}

      <section className="fs-dlv__block" aria-label={t('Scope')}>
        <h3 className="fs-dlv__h">{t('Scope')}</h3>
        {c.scope.length === 0 && <p className="fs-dlv__muted">{t('No scope written yet.')}</p>}
        <ol className="fs-dlv__plain">
          {c.scope.map((v) => (
            <li key={v.version} data-testid={`office-scope-${v.version}`}>
              <strong>v{v.version} · {v.kind === 'initial' ? t('First request') : v.kind === 'agreement' ? t('Agreement') : t('Change request')}</strong>
              <span className="fs-dlv__muted"> · {v.at}</span>
              <div className="fs-dlv__pre">{v.text}</div>
              {v.comparison && (
                <div className="fs-dlv__row">
                  <span className="fs-dlv__muted">{t('Against')} v{v.comparison.against}:</span>
                  {(Object.entries(v.comparison.counts) as [api.ScopeStatus, number][]).filter(([, n]) => n).map(([s, n]) => (
                    <span key={s} className="fs-dlv__pill" data-tone={api.scopeTone(s)}>{api.scopeLabel(s)} · {n}</span>
                  ))}
                  <a className="fs-dlv__muted" href={`/delivery?view=scope&c=${encodeURIComponent(v.comparison.id)}`}>{v.comparison.id}</a>
                </div>
              )}
              {v.comparison && v.comparison.questions.length > 0 && <ul className="fs-dlv__plain fs-dlv__small">{v.comparison.questions.map((q) => <li key={q} className="fs-dlv__ask">{q}</li>)}</ul>}
            </li>
          ))}
        </ol>
        <div className="fs-dlv__row">
          <select className="fs-field" value={scopeKind} onChange={(e) => setScopeKind(e.target.value)} aria-label={t('Kind')}>
            <option value="initial">{t('First request')}</option><option value="agreement">{t('Agreement')}</option><option value="change">{t('Change request')}</option>
          </select>
          <textarea className="fs-field fs-dlv__grow" rows={3} value={scopeText} onChange={(e) => setScopeText(e.target.value)} placeholder={t('One clause per line')} data-testid="office-scope-text" />
          <Button icon={Plus} label={t('Add version')} disabled={!scopeText.trim()} testId="office-scope-add"
            onClick={() => act('add_scope', { kind: scopeKind, text: scopeText }, () => setScopeText(''))} />
        </div>
      </section>

      <section className="fs-dlv__block" aria-label={t('Milestones')}>
        <h3 className="fs-dlv__h">{t('Milestones and deliveries')}</h3>
        {c.milestones.length > 0 && (
          <table className="fs-dlv__table">
            <thead><tr><th>{t('Milestone')}</th><th>{t('Due')}</th><th>{t('Deliverables')}</th><th /></tr></thead>
            <tbody>
              {c.milestones.map((m) => (
                <tr key={m.id}>
                  <td>{m.id} · {m.title}{m.amount != null ? <div className="fs-dlv__muted">{m.amount} {c.budget?.currency ?? ''}</div> : null}</td>
                  <td>{m.due || '—'}{m.done ? <div><span className="fs-dlv__pill" data-tone="good">{t('Done')} {m.done}</span></div> : st.overdue.includes(m.id) ? <div><span className="fs-dlv__pill" data-tone="bad">{t('Overdue')}</span></div> : null}</td>
                  <td>
                    <ul className="fs-dlv__plain fs-dlv__small">
                      {m.deliverables.map((d) => {
                        const live = st.deliverables.find((x) => x.milestone === m.id && x.title === d.title);
                        return <li key={d.title}>{d.title} {live?.package_id ? <span className="fs-dlv__pill" data-tone={live.ready ? 'good' : 'bad'}>{live.ready ? t('Ready to deliver') : t('Not ready')}</span> : <span className="fs-dlv__muted">({t('no package')})</span>}</li>;
                      })}
                    </ul>
                    <div className="fs-dlv__row">
                      <select className="fs-field" value={link[m.id] ?? ''} aria-label={t('Delivery package')} onChange={(e) => setLink({ ...link, [m.id]: e.target.value })} data-testid={`office-link-${m.id}`}>
                        <option value="">{t('Delivery package…')}</option>
                        {packages.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.ready ? t('Ready to deliver') : t('Not ready')}</option>)}
                      </select>
                      <Button size="sm" icon={Link2} label={t('Link')} disabled={!link[m.id]} testId={`office-link-btn-${m.id}`}
                        onClick={() => act('link_package', { milestone: m.id, package_id: link[m.id] })} />
                    </div>
                  </td>
                  <td>{!m.done && <Button size="sm" icon={Check} label={t('Mark done')} testId={`office-done-${m.id}`} onClick={() => act('update_milestone', { milestone: m.id, done: true })} />}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="fs-dlv__row">
          <input className="fs-field fs-dlv__grow" placeholder={t('Milestone')} value={msTitle} onChange={(e) => setMsTitle(e.target.value)} data-testid="office-ms-title" />
          <input className="fs-field" type="date" value={msDue} onChange={(e) => setMsDue(e.target.value)} aria-label={t('Due')} data-testid="office-ms-due" />
          <input className="fs-field fs-dlv__num" type="number" placeholder={t('Amount')} value={msAmount} onChange={(e) => setMsAmount(e.target.value)} />
          <Button icon={Plus} label={t('Add milestone')} disabled={!msTitle.trim()} testId="office-ms-add"
            onClick={() => act('add_milestone', { title: msTitle, due: msDue, ...(msAmount ? { amount: Number(msAmount) } : {}) }, () => { setMsTitle(''); setMsDue(''); setMsAmount(''); })} />
        </div>
      </section>

      <section className="fs-dlv__block" aria-label={t('People, files and decisions')}>
        <h3 className="fs-dlv__h">{t('People, files and decisions')}</h3>
        <ul className="fs-dlv__plain fs-dlv__small">
          {c.contacts.map((p) => <li key={p.name}>{p.name}{p.role ? ` · ${p.role}` : ''}{p.people_id ? ` · People ${p.people_id}` : ''}</li>)}
          {c.files.map((f) => <li key={f.path} className="fs-dlv__mono">{f.role}: {f.path}</li>)}
          {c.decisions.map((d) => <li key={d.date + d.text}>{d.date} · {d.text}</li>)}
        </ul>
        <div className="fs-dlv__row">
          <input className="fs-field" placeholder={t('Name')} value={contact} onChange={(e) => setContact(e.target.value)} />
          <input className="fs-field" placeholder={t('Role')} value={role} onChange={(e) => setRole(e.target.value)} />
          <Button size="sm" icon={Plus} label={t('Add contact')} disabled={!contact.trim()} onClick={() => act('add_contact', { name: contact, role }, () => { setContact(''); setRole(''); })} />
        </div>
        <div className="fs-dlv__row">
          <input className="fs-field fs-dlv__grow" placeholder={t('Full path of the file')} value={file} onChange={(e) => setFile(e.target.value)} />
          <Button size="sm" icon={Plus} label={t('Add file')} disabled={!file.trim()} onClick={() => act('add_file', { path: file }, () => setFile(''))} />
        </div>
        <div className="fs-dlv__row">
          <input className="fs-field fs-dlv__grow" placeholder={t('Decision')} value={decision} onChange={(e) => setDecision(e.target.value)} />
          <Button size="sm" icon={Plus} label={t('Add decision')} disabled={!decision.trim()} onClick={() => act('add_decision', { text: decision }, () => setDecision(''))} />
        </div>
      </section>

      <section className="fs-dlv__block" aria-label={t('Proposal')} data-testid="office-proposal">
        <h3 className="fs-dlv__h">{t('Proposal')}</h3>
        <p className="fs-dlv__muted">{t('Drafted from this record: each point says whether it comes from the record, is an assumption of this proposal or is still an open question. Internal names, local paths and e-mails that are not the client\'s are masked and listed.')}</p>
        <label className="fs-dlv__field">{t('Assumptions (one per line)')}<textarea className="fs-field" rows={2} value={assumptions} onChange={(e) => setAssumptions(e.target.value)} data-testid="office-assumptions" /></label>
        <label className="fs-dlv__field">{t('Out of scope (one per line)')}<textarea className="fs-field" rows={2} value={exclusions} onChange={(e) => setExclusions(e.target.value)} data-testid="office-exclusions" /></label>
        <label className="fs-dlv__field">{t('Other words to keep out (one per line)')}<textarea className="fs-field" rows={2} value={internal} onChange={(e) => setInternal(e.target.value)} /></label>
        <div><Button variant="primary" icon={FileText} label={t('Draft proposal')} testId="office-proposal-btn"
          onClick={() => { setError(''); api.commissionAction(c.id, 'proposal', { assumptions, exclusions, internal_terms: internal }).then((r) => { setProposal(r.proposal ?? null); onChange(r); }).catch((e) => setError((e as Error).message)); }} /></div>
        {(c.proposals ?? []).length > 0 && (
          <ul className="fs-dlv__plain fs-dlv__small">
            {(c.proposals ?? []).map((p) => (
              <li key={p.name}>{p.name} · {p.date} · {p.counts.verified ?? 0} {t('verified')}, {p.counts.assumption ?? 0} {t('assumptions')}, {p.counts.question ?? 0} {t('questions')}{p.masked ? ` · ${p.masked} ${t('masked')}` : ''}
                {' '}<a href={api.proposalHref(c.id, p.name, 'docx')} download>.docx</a> · <a href={api.proposalHref(c.id, p.name, 'md')} target="_blank" rel="noreferrer">.md</a></li>
            ))}
          </ul>
        )}
        {proposal && (
          <div className="fs-dlv__proposal" data-testid="office-proposal-preview">
            <strong>{proposal.title}</strong>
            {proposal.sections.map((s) => (
              <div key={s.title}>
                <div className="fs-dlv__h">{s.title}</div>
                {s.items.length === 0 ? <p className="fs-dlv__muted">{t('No data in the record.')}</p> : (
                  <ul className="fs-dlv__plain fs-dlv__small">
                    {s.items.map((it, i) => <li key={i}>{it.text} <span className="fs-dlv__pill" data-tone={it.origin === 'verified' ? 'good' : it.origin === 'question' ? 'warn' : 'unknown'}>{api.originLabel(it.origin)}</span></li>)}
                  </ul>
                )}
              </div>
            ))}
            {proposal.leaks.length > 0 && <p className="fs-dlv__muted">{t('Masked')}: {proposal.leaks.map((l) => `${l.kind} «${l.value}»`).join(' · ')}</p>}
          </div>
        )}
      </section>

      <section className="fs-dlv__block" aria-label={t('Timeline')}>
        <h3 className="fs-dlv__h">{t('Timeline')}</h3>
        <ol className="fs-dlv__plain fs-dlv__small">{[...c.events].reverse().slice(0, 30).map((e, i) => <li key={i}><span className="fs-dlv__muted">{e.at}</span> · {e.text}</li>)}</ol>
      </section>
    </div>
  );
}

export function OfficePanel() {
  const [rows, setRows] = useState<api.CommissionRow[]>([]);
  const [open, setOpen] = useState<api.Commission | null>(null);
  const [creating, setCreating] = useState(false);
  const [packages, setPackages] = useState<api.PackageRow[]>([]);
  const reload = useCallback(() => {
    api.listCommissions().then(setRows).catch(() => undefined);
    api.listPackages().then(setPackages).catch(() => undefined);
  }, []);
  useEffect(() => { reload(); }, [reload]);
  return (
    <div className="fs-dlv__layout" data-testid="office" data-single={rows.length === 0 || undefined}>
      <div className="fs-dlv__detail">
        <div><Button icon={Plus} label={t('New commission')} onClick={() => { setCreating(true); setOpen(null); }} testId="office-new-btn" /></div>
        {rows.length > 0 && (
          <ul className="fs-dlv__list" aria-label={t('Commissions')}>
            {rows.map((r) => (
              <li key={r.id}>
                <button type="button" className="fs-dlv__item" data-active={open?.id === r.id} onClick={() => { setCreating(false); void api.loadCommission(r.id).then(setOpen); }}>
                  <span className="fs-dlv__item-title">{r.title}</span>
                  <span className="fs-dlv__muted">{r.client || '—'} · {api.stageLabel(r.stage)}{r.next_due ? ` · ${t('Next')} ${r.next_due}` : ''}</span>
                  {r.overdue > 0 && <span className="fs-dlv__pill" data-tone="bad">{t('Overdue')} · {r.overdue}</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        {creating && <NewCommission onCreated={(c) => { setCreating(false); setOpen(c); reload(); }} />}
        {!creating && open && <Detail key={open.id + open.updated} c={open} packages={packages} onChange={(c) => { setOpen(c); reload(); }} />}
        {!creating && !open && <EmptyState icon={Briefcase} title={t('No commission open')} body={t('A commission keeps its scope versions, milestones, deliveries, contacts, files and decisions together.')} />}
      </div>
    </div>
  );
}
