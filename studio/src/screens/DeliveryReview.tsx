import { Check, FileSearch, GitCompare, MessageSquarePlus, PackageCheck, Plus, Upload } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState } from '../components';
import * as api from '../adapters/delivery';
import { t } from '../i18n';
import { DeleteItem } from './DeliveryDelete';

/**
 * Review desk (Hangar E32): one deliverable, its versions as snapshots, comments
 * anchored to the version they were made on, a state per correction and the one
 * version that was delivered.
 */

function NewDesk({ onCreated }: { onCreated: (d: api.ReviewDesk) => void }) {
  const [title, setTitle] = useState('');
  const [path, setPath] = useState('');
  const [error, setError] = useState('');
  return (
    <section className="fs-dlv__block" aria-label={t('New review')} data-testid="review-new">
      <h3 className="fs-dlv__h">{t('New review')}</h3>
      <input className="fs-field" placeholder={t('Deliverable')} value={title} onChange={(e) => setTitle(e.target.value)} data-testid="review-new-title" />
      <input className="fs-field" placeholder={t('Full path of the first version (optional)')} value={path} onChange={(e) => setPath(e.target.value)} data-testid="review-new-path" />
      {error && <p className="fs-dlv__why" data-tone="bad" role="alert">{error}</p>}
      <div><Button variant="primary" icon={Plus} label={t('Create')} disabled={!title.trim()} testId="review-new-create"
        onClick={() => { setError(''); api.createDesk({ title, path }).then(onCreated).catch((e) => setError((e as Error).message)); }} /></div>
    </section>
  );
}

function DeskDetail({ d, onChange }: { d: api.ReviewDesk; onChange: (d: api.ReviewDesk) => void }) {
  const [error, setError] = useState('');
  const [path, setPath] = useState('');
  const [note, setNote] = useState('');
  const [text, setText] = useState('');
  const [version, setVersion] = useState('');
  const [anchor, setAnchor] = useState('');
  const [feedback, setFeedback] = useState('');
  const [cmp, setCmp] = useState<api.DeskComparison | null>(null);
  const [a, setA] = useState('');
  const [b, setB] = useState('');
  const st = d.status;
  const act = (action: string, body: Record<string, unknown>, after?: () => void) => {
    setError('');
    api.deskAction(d.id, action, body).then((r) => { onChange(r); after?.(); }).catch((e) => setError((e as Error).message));
  };
  return (
    <div className="fs-dlv__detail" data-testid="review-detail">
      <div className="fs-dlv__title-row">
        <h2 className="fs-dlv__title">{d.title}</h2>
        {st.deliverable ? <span className="fs-dlv__pill" data-tone={st.delivered_check?.snapshot_matches === false ? 'bad' : 'good'} data-testid="review-deliverable">{t('Delivered')}: {st.deliverable}</span>
          : <span className="fs-dlv__pill" data-tone="unknown">{t('Not delivered yet')}</span>}
      </div>
      {st.delivered_check && st.delivered_check.source_still_matches === false && <p className="fs-dlv__why" data-tone="warn">{t('The source file changed after delivery; the delivered snapshot is kept.')}</p>}
      {st.delivered_check && st.delivered_check.newer_versions.length > 0 && <p className="fs-dlv__why" data-tone="warn">{t('Newer versions exist after the delivered one')}: {st.delivered_check.newer_versions.map((n) => `v${n}`).join(', ')}</p>}
      {error && <p className="fs-dlv__why" data-tone="bad" role="alert">{error}</p>}

      <section className="fs-dlv__block" aria-label={t('Versions')}>
        <h3 className="fs-dlv__h">{t('Versions')}</h3>
        <table className="fs-dlv__table" data-testid="review-versions">
          <thead><tr><th>#</th><th>{t('File')}</th><th>{t('Reopened')}</th><th>{t('Comments')}</th><th>sha256</th><th /></tr></thead>
          <tbody>
            {d.versions.map((v) => {
              const mine = d.comments.filter((c) => c.version === v.n);
              const open = mine.filter((c) => c.status === 'open' || c.status === 'in_progress').length;
              return (
                <tr key={v.n} data-testid={`review-v${v.n}`}>
                  <td>v{v.n}{v.n === st.latest ? <div><span className="fs-dlv__pill" data-tone="good">{t('Latest')}</span></div> : null}</td>
                  <td>{v.name}<div className="fs-dlv__muted">{v.at}{v.note ? ` · ${v.note}` : ''}</div></td>
                  <td><span className="fs-dlv__pill" data-tone={v.reopen?.status === 'verified' ? 'good' : v.reopen?.status === 'failed' ? 'bad' : 'unknown'}>{v.reopen?.status ? api.statusLabel(v.reopen.status) : '—'}</span></td>
                  <td>{mine.length}{open ? ` · ${open} ${t('still open')}` : ''}</td>
                  <td className="fs-dlv__mono">{v.sha256.slice(0, 12)}</td>
                  <td>{v.snapshot ? <a href={api.deskVersionHref(d.id, v.n)} download>{t('Download')}</a> : <span className="fs-dlv__muted">{t('too large to keep')}</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="fs-dlv__row">
          <input className="fs-field fs-dlv__grow" placeholder={t('Full path of the new version')} value={path} onChange={(e) => setPath(e.target.value)} data-testid="review-path" />
          <input className="fs-field" placeholder={t('Note')} value={note} onChange={(e) => setNote(e.target.value)} />
          <Button icon={Upload} label={t('Add version')} disabled={!path.trim()} testId="review-add-version" onClick={() => act('add_version', { path, note }, () => { setPath(''); setNote(''); })} />
        </div>
        {d.versions.length > 1 && (
          <div className="fs-dlv__row">
            <select className="fs-field" value={a} onChange={(e) => setA(e.target.value)} aria-label={t('Compare from')}><option value="">{t('Previous version')}</option>{d.versions.map((v) => <option key={v.n} value={v.n}>v{v.n}</option>)}</select>
            <select className="fs-field" value={b} onChange={(e) => setB(e.target.value)} aria-label={t('Compare to')}><option value="">{t('Latest')}</option>{d.versions.map((v) => <option key={v.n} value={v.n}>v{v.n}</option>)}</select>
            <Button size="sm" icon={GitCompare} label={t('Compare')} testId="review-compare" onClick={() => { api.compareDesk(d.id, a, b).then(setCmp).catch((e) => setError((e as Error).message)); }} />
          </div>
        )}
        {cmp && (
          <div className="fs-dlv__block" data-testid="review-comparison">
            <strong>v{cmp.a} → v{cmp.b}</strong> <span className="fs-dlv__muted">{cmp.same_content ? t('same content') : `${cmp.size_delta >= 0 ? '+' : ''}${cmp.size_delta} B`}</span>
            {Object.keys(cmp.facts).length > 0 && <ul className="fs-dlv__plain fs-dlv__small">{Object.entries(cmp.facts).map(([k, [x, y]]) => <li key={k}>{k}: {String(x)} → {String(y)}</li>)}</ul>}
            {cmp.text ? <pre className="fs-dlv__pre fs-dlv__diff">{cmp.text.diff.join('\n') || t('No text differences')}</pre> : <p className="fs-dlv__muted">{t('No text reader for this format; reader facts and hash only.')}</p>}
            {cmp.comments_on_a.length > 0 && <p className="fs-dlv__muted">{t('Comments made on')} v{cmp.a}: {cmp.comments_on_a.map((c) => `${c.id} (${api.commentStateLabel(c.status)})`).join(' · ')}</p>}
          </div>
        )}
      </section>

      <section className="fs-dlv__block" aria-label={t('Comments')} data-testid="review-comments">
        <h3 className="fs-dlv__h">{t('Comments')}</h3>
        <p className="fs-dlv__muted">{t('A comment stays on the version it was made on. Anchors: p3 or p3@x,y,w,h, t=00:01:10-00:01:25, Sheet!B4, obj:Name, L10-20, sym:name; «página 3», «min 1:10», «celda B4» or «pieza Tapa» in the text also work.')}</p>
        {st.open_from_older.length > 0 && <p className="fs-dlv__why" data-tone="warn" data-testid="review-older">{t('Open comments made on an older version, to check in the latest')}: {st.open_from_older.join(', ')}</p>}
        <table className="fs-dlv__table">
          <thead><tr><th>#</th><th>{t('Version')}</th><th>{t('Where')}</th><th>{t('Remark')}</th><th>{t('State')}</th></tr></thead>
          <tbody>
            {d.comments.map((c) => (
              <tr key={c.id} data-testid={`review-${c.id}`}>
                <td>{c.id}</td>
                <td>v{c.version}{c.about_older ? <div><span className="fs-dlv__pill" data-tone="warn">{t('older version')}</span></div> : null}<div className="fs-dlv__muted fs-dlv__small">{api.versionFromLabel(c.version_from)}</div></td>
                <td className="fs-dlv__mono" title={c.ref}>{c.anchor?.label ?? '—'}</td>
                <td>{c.text}{c.author ? <div className="fs-dlv__muted">{c.author}</div> : null}</td>
                <td>
                  <select className="fs-field" value={c.status} aria-label={t('State')} data-testid={`review-state-${c.id}`} onChange={(e) => act('set_status', { comment: c.id, status: e.target.value })}>
                    {api.COMMENT_STATES.map((s) => <option key={s} value={s}>{api.commentStateLabel(s)}</option>)}
                  </select>
                  {c.fixed_in ? <div className="fs-dlv__muted">{t('fixed in')} v{c.fixed_in}</div> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="fs-dlv__row">
          <input className="fs-field fs-dlv__grow" placeholder={t('Remark')} value={text} onChange={(e) => setText(e.target.value)} data-testid="review-comment-text" />
          <select className="fs-field" value={version} onChange={(e) => setVersion(e.target.value)} aria-label={t('Version')}><option value="">{t('Latest or named')}</option>{d.versions.map((v) => <option key={v.n} value={v.n}>v{v.n}</option>)}</select>
          <input className="fs-field" placeholder={t('Anchor')} value={anchor} onChange={(e) => setAnchor(e.target.value)} data-testid="review-comment-anchor" />
          <Button size="sm" icon={MessageSquarePlus} label={t('Add comment')} disabled={!text.trim() || d.versions.length === 0} testId="review-comment-add"
            onClick={() => act('comment', { text, anchor, ...(version ? { version: Number(version) } : {}) }, () => { setText(''); setAnchor(''); })} />
        </div>
        <div className="fs-dlv__row">
          <textarea className="fs-field fs-dlv__grow" rows={3} value={feedback} onChange={(e) => setFeedback(e.target.value)} placeholder={t('Paste client feedback: one comment per line; a line such as «Sobre la v1:» applies to the lines below')} data-testid="review-feedback" />
          <Button size="sm" icon={FileSearch} label={t('Import feedback')} disabled={!feedback.trim() || d.versions.length === 0} testId="review-feedback-import" onClick={() => act('import_feedback', { text: feedback }, () => setFeedback(''))} />
        </div>
      </section>

      <section className="fs-dlv__block" aria-label={t('Deliver')}>
        <h3 className="fs-dlv__h">{t('Deliver')}</h3>
        <p className="fs-dlv__muted">{t('Names the one version to hand over. It is refused while comments are open unless you deliver anyway.')}</p>
        <div className="fs-dlv__row">
          <Button variant="primary" icon={PackageCheck} label={`${t('Deliver')} v${st.latest ?? ''}`} disabled={!st.latest} testId="review-deliver" onClick={() => act('deliver', {})} />
          <Button size="sm" icon={Check} label={t('Deliver anyway')} disabled={!st.latest} testId="review-deliver-force" onClick={() => act('deliver', { force: true })} />
        </div>
      </section>

      <section className="fs-dlv__block" aria-label={t('Timeline')}>
        <h3 className="fs-dlv__h">{t('Timeline')}</h3>
        <ol className="fs-dlv__plain fs-dlv__small">{[...d.events].reverse().slice(0, 30).map((e, i) => <li key={i}><span className="fs-dlv__muted">{e.at}</span> · {e.text}</li>)}</ol>
      </section>
    </div>
  );
}

export function ReviewPanel() {
  const [rows, setRows] = useState<api.DeskRow[]>([]);
  const [open, setOpen] = useState<api.ReviewDesk | null>(null);
  const [creating, setCreating] = useState(false);
  const reload = useCallback(() => { api.listDesks().then(setRows).catch(() => undefined); }, []);
  useEffect(() => { reload(); }, [reload]);
  return (
    <div className="fs-dlv__layout" data-testid="review" data-single={rows.length === 0 || undefined}>
      <div className="fs-dlv__detail">
        <div><Button icon={Plus} label={t('New review')} onClick={() => { setCreating(true); setOpen(null); }} testId="review-new-btn" /></div>
        {rows.length > 0 && (
          <ul className="fs-dlv__list" aria-label={t('Reviews')}>
            {rows.map((r) => (
              <li className="fs-dlv__li" key={r.id}>
                <button type="button" className="fs-dlv__item" data-active={open?.id === r.id} onClick={() => { setCreating(false); void api.loadDesk(r.id).then(setOpen); }}>
                  <span className="fs-dlv__item-title">{r.title}</span>
                  <span className="fs-dlv__muted">{r.versions} {t('versions')}{r.open ? ` · ${r.open} ${t('still open')}` : ''}{r.delivered ? ` · ${t('Delivered')} v${r.delivered}` : ''}</span>
                </button>
                <DeleteItem label={r.title} confirm={t('Delete the review "{name}" with all its versions and comments? The snapshots kept by Faustus are removed; the original files are not touched.', { name: r.title })} onDelete={async () => { await api.deleteDesk(r.id); if (open?.id === r.id) setOpen(null); reload(); }} testId={`review-delete-${r.id}`} />
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        {creating && <NewDesk onCreated={(d) => { setCreating(false); setOpen(d); reload(); }} />}
        {!creating && open && <DeskDetail key={open.id + open.updated} d={open} onChange={(d) => { setOpen(d); reload(); }} />}
        {!creating && !open && <EmptyState icon={FileSearch} title={t('No review open')} body={t('A review keeps every version of a deliverable, the comments on each one and the version that was delivered.')} />}
      </div>
    </div>
  );
}
