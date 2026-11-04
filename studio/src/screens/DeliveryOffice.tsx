import { Briefcase, Check, FileText, Link2, Plus, Search, Wallet } from 'lucide-react';
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
  const [lookup, setLookup] = useState('');
  const [found, setFound] = useState<{ people: api.PersonCandidate[]; error?: string } | null>(null);
  const [payLabel, setPayLabel] = useState('');
  const [payAmount, setPayAmount] = useState('');
  const [payDue, setPayDue] = useState('');
  const [meetingList, setMeetingList] = useState<{ id: string; title: string; date: string }[]>([]);
  const [meetingId, setMeetingId] = useState('');
  const [meetingText, setMeetingText] = useState('');
  const [meetingSource, setMeetingSource] = useState('');
  useEffect(() => { api.recentMeetings().then(setMeetingList).catch(() => setMeetingList([])); }, []);
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

      <section className="fs-dlv__block" aria-label={t('Meetings')} data-testid="office-meetings">
        <h3 className="fs-dlv__h">{t('Meetings')}</h3>
        <p className="fs-dlv__muted">{t('Each statement of a meeting is sorted into decision, promise, requirement, tentative proposal or question, with who said it and when. «Podríamos», «could» and similar wording is never taken as a commitment; nothing goes into the record until you promote it.')}</p>
        <div className="fs-dlv__row">
          <select className="fs-field fs-dlv__grow" value={meetingId} aria-label={t('Faustus meeting')} onChange={(e) => setMeetingId(e.target.value)} data-testid="office-meeting-pick">
            <option value="">{meetingList.length ? t('Faustus meeting…') : t('No Faustus meetings yet')}</option>
            {meetingList.map((m) => <option key={m.id} value={m.id}>{m.title}{m.date ? ` · ${m.date.slice(0, 10)}` : ''}</option>)}
          </select>
          <Button size="sm" icon={Plus} label={t('Import meeting')} disabled={!meetingId} testId="office-meeting-import-id"
            onClick={() => act('import_meeting', { meeting_id: meetingId }, () => setMeetingId(''))} />
        </div>
        <div className="fs-dlv__row">
          <textarea className="fs-field fs-dlv__grow" rows={3} value={meetingText} onChange={(e) => setMeetingText(e.target.value)} placeholder={t('Or paste notes or a transcript: one statement per line, «[00:12:30] Name: text»')} data-testid="office-meeting-text" />
          <input className="fs-field" placeholder={t('Source')} value={meetingSource} onChange={(e) => setMeetingSource(e.target.value)} data-testid="office-meeting-source" />
          <Button size="sm" icon={Plus} label={t('Import text')} disabled={!meetingText.trim()} testId="office-meeting-import"
            onClick={() => act('import_meeting', { text: meetingText, source: meetingSource }, () => { setMeetingText(''); setMeetingSource(''); })} />
        </div>
        {(st.meeting_open_questions ?? []).length > 0 && (
          <div data-testid="office-meeting-questions">
            <strong>{t('Open questions from meetings')}</strong>
            <ul className="fs-dlv__plain fs-dlv__small">{(st.meeting_open_questions ?? []).map((q) => <li key={q} className="fs-dlv__ask">{q}</li>)}</ul>
          </div>
        )}
        {(c.meetings ?? []).map((m) => (
          <div key={m.id} className="fs-dlv__meeting" data-testid={`office-meeting-${m.id}`}>
            <div className="fs-dlv__row">
              <strong>{m.id} · {m.source}</strong><span className="fs-dlv__muted">{m.date}</span>
              {api.MEETING_KINDS.filter((k) => m.counts[k]).map((k) => <span key={k} className="fs-dlv__pill" data-tone={api.meetingKindTone(k)}>{api.meetingKindLabel(k)} · {m.counts[k]}</span>)}
            </div>
            <table className="fs-dlv__table">
              <thead><tr><th>#</th><th>{t('Kind')}</th><th>{t('When')}</th><th>{t('Who')}</th><th>{t('Statement')}</th><th /></tr></thead>
              <tbody>
                {m.items.map((i) => (
                  <tr key={i.n} data-testid={`office-meeting-${m.id}-${i.n}`}>
                    <td>{i.n}</td>
                    <td><span className="fs-dlv__pill" data-tone={api.meetingKindTone(i.kind)}>{api.meetingKindLabel(i.kind)}</span></td>
                    <td className="fs-dlv__mono">{i.time || `L${i.line}`}</td><td>{i.speaker || '—'}</td><td>{i.text}</td>
                    <td>
                      {i.promoted ? <span className="fs-dlv__pill" data-tone="good">{i.promoted.as === 'milestone' ? t('Milestone') : api.meetingKindLabel(i.promoted.as)} · {i.promoted.ref}</span>
                        : i.firm ? (
                          <div className="fs-dlv__row">
                            <Button size="sm" label={t('Decision')} testId={`office-promote-${m.id}-${i.n}-decision`} onClick={() => act('promote_meeting_item', { meeting: m.id, n: i.n, as: 'decision' })} />
                            <Button size="sm" label={t('Requirement')} testId={`office-promote-${m.id}-${i.n}-requirement`} onClick={() => act('promote_meeting_item', { meeting: m.id, n: i.n, as: 'requirement' })} />
                            <Button size="sm" label={t('Milestone')} testId={`office-promote-${m.id}-${i.n}-milestone`} onClick={() => act('promote_meeting_item', { meeting: m.id, n: i.n, as: 'milestone' })} />
                          </div>
                        ) : i.kind === 'proposal' ? (
                          <span className="fs-dlv__muted">{t('Not a commitment')} <Button size="sm" label={t('Requirement anyway')} testId={`office-promote-${m.id}-${i.n}-force`} onClick={() => act('promote_meeting_item', { meeting: m.id, n: i.n, as: 'requirement', force: true })} /></span>
                        ) : <span className="fs-dlv__muted">{t('Waiting for an answer')}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </section>

      <section className="fs-dlv__block" aria-label={t('People and payments')} data-testid="office-links">
        <h3 className="fs-dlv__h">{t('People and payments')}</h3>
        <p className="fs-dlv__muted">{t('Read live from People and Ledger through the Hub; only their ids are kept here and nothing is written there.')}</p>
        <div className="fs-dlv__row">
          <input className="fs-field fs-dlv__grow" placeholder={t('Name to look up in People')} value={lookup} onChange={(e) => setLookup(e.target.value)} data-testid="office-people-q" />
          <Button size="sm" icon={Search} label={t('Find in People')} disabled={!lookup.trim()} testId="office-people-find"
            onClick={() => { api.peopleLookup(lookup).then(setFound).catch((e) => setError((e as Error).message)); }} />
        </div>
        {found && (found.error ? <p className="fs-dlv__why" data-tone="bad">{found.error}</p> : found.people.length === 0 ? <p className="fs-dlv__muted">{t('Nobody in People matches that name.')}</p> : (
          <ul className="fs-dlv__plain fs-dlv__small" data-testid="office-people-results">
            {found.people.map((p) => <li key={p.id}>{p.name}{p.relation ? ` · ${p.relation}` : ''}{p.score != null ? ` · ${p.score}` : ''} <Button size="sm" icon={Link2} label={t('Link')} onClick={() => act('link_person', { name: p.name, people_id: p.id }, () => setFound(null))} /></li>)}
          </ul>
        ))}
        <table className="fs-dlv__table" data-testid="office-payments">
          <thead><tr><th>{t('Payment')}</th><th>{t('Amount')}</th><th>{t('Due')}</th><th>{t('In Ledger')}</th></tr></thead>
          <tbody>
            {c.payments.map((p) => (
              <tr key={p.label}>
                <td>{p.label}</td><td>{p.amount ?? '—'}</td><td>{p.due || '—'}</td>
                <td>
                  {p.confirmed ? <span className="fs-dlv__pill" data-tone="good">{t('Received')} {p.received_on || ''}</span>
                    : p.ledger_check?.error ? <span className="fs-dlv__muted">{p.ledger_check.error}</span>
                    : p.ledger_check ? ((p.ledger_check.candidates ?? []).length === 0 ? <span className="fs-dlv__muted">{t('No incoming movement of that amount')} ({p.ledger_check.entries_scanned ?? 0} {t('read')})</span> : (
                      <ul className="fs-dlv__plain fs-dlv__small">
                        {(p.ledger_check.candidates ?? []).map((m) => <li key={m.tx_id}>{m.date} · {m.amount} · {m.counterparty} · {m.score} <Button size="sm" icon={Check} label={t('Confirm')} onClick={() => act('confirm_payment', { label: p.label, tx_id: m.tx_id })} /></li>)}
                      </ul>
                    )) : <span className="fs-dlv__muted">{t('Not checked yet')}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="fs-dlv__row">
          <input className="fs-field fs-dlv__grow" placeholder={t('Payment')} value={payLabel} onChange={(e) => setPayLabel(e.target.value)} data-testid="office-pay-label" />
          <input className="fs-field fs-dlv__num" type="number" placeholder={t('Amount')} value={payAmount} onChange={(e) => setPayAmount(e.target.value)} data-testid="office-pay-amount" />
          <input className="fs-field" type="date" value={payDue} onChange={(e) => setPayDue(e.target.value)} aria-label={t('Due')} />
          <Button size="sm" icon={Plus} label={t('Add payment')} disabled={!payLabel.trim()} testId="office-pay-add"
            onClick={() => act('add_payment', { label: payLabel, ...(payAmount ? { amount: Number(payAmount) } : {}), due: payDue }, () => { setPayLabel(''); setPayAmount(''); setPayDue(''); })} />
          <Button size="sm" icon={Wallet} label={t('Check Ledger')} disabled={c.payments.length === 0} testId="office-ledger-check" onClick={() => act('reconcile_payments', {})} />
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
