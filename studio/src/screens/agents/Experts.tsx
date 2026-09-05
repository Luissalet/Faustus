import { ArrowLeft, ClipboardCheck, FileUp, Plus, RefreshCw, Search, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type ReactNode } from 'react';
import { Button, Dialog, EmptyState, IconButton, Skeleton, Toast } from '../../components';
import {
  applyAcceptedDeltas,
  corpusUrl,
  createExpert,
  deleteCorpusFile,
  deleteExpert,
  listExperts,
  pageLabel,
  previewBlock,
  readExpert,
  refOf,
  reindexExpert,
  reviewFrom,
  rubricLines,
  searchCorpus,
  sendFeedback,
  updateExpert,
  uploadCorpus,
  type BlockPreview,
  type CorpusSearch,
  type ExpertDetail,
  type ExpertSummary,
  type ReindexResult,
  type ReviewDelta,
  type ReviewResult,
} from '../../adapters/workers';
import { t, tn } from '../../i18n';

/**
 * Expertos (experts.js): a specialist with its own corpus — your books,
 * indexed by page — and corrections that cite the page they came from. The
 * gallery, one expert's profile + corpus + search + "what the model sees",
 * and the review panel where each correction is accepted or rejected over
 * the ORIGINAL text and the outcome is reported back.
 */

type Sort = 'name' | 'corpus' | 'accepted';
type View = { kind: 'gallery' } | { kind: 'detail'; slug: string } | { kind: 'review' };

function bytes(n: number): string {
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

/* ── Gallery ── */

function Gallery({ rows, enabled, loading, error, onOpen, onNew, onDelete, onReview }: { rows: ExpertSummary[]; enabled: boolean; loading: boolean; error: string | null; onOpen: (slug: string) => void; onNew: () => void; onDelete: (row: ExpertSummary) => void; onReview: () => void }) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<Sort>('name');
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const hit = needle ? rows.filter((r) => [r.name, r.slug, r.description, r.model].some((v) => v.toLowerCase().includes(needle))) : rows.slice();
    const byName = (a: ExpertSummary, b: ExpertSummary) => a.name.localeCompare(b.name);
    if (sort === 'corpus') return hit.sort((a, b) => b.chunks - a.chunks || byName(a, b));
    if (sort === 'accepted') return hit.sort((a, b) => b.accepted - a.accepted || byName(a, b));
    return hit.sort(byName);
  }, [rows, query, sort]);
  return (
    <div className="fs-exp__gallery">
      <div className="fs-agents__intro">
        <p className="fs-prose">{t('A specialist with its own corpus: your books, indexed by page, and corrections that cite the page they came from.')}</p>
        {!enabled && (
          <p className="fs-agents__note">
            {t('Experts are switched off in Settings')} (<code>agent_experts</code>): {t('nothing is injected into a turn. Everything here still edits fine.')}
          </p>
        )}
      </div>
      <div className="fs-agents__toolbar">
        <label className="fs-agents__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder={t('Search experts…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search experts')} />
        </label>
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label={t('Sort experts')}>
          <option value="name">{t('Name')}</option>
          <option value="corpus">{t('Corpus size')}</option>
          <option value="accepted">{t('Accepted')}</option>
        </select>
        <span className="fs-agents__spacer" />
        <Button variant="ghost" size="sm" icon={ClipboardCheck} label={t('Review panel')} onClick={onReview} />
        <Button variant="primary" size="sm" icon={Plus} label={t('New expert')} onClick={onNew} testId="exp-new" />
      </div>
      {error && <div className="fs-wk__error">{error}</div>}
      {loading ? (
        <Skeleton label={t('Loading the experts')} height="120px" count={3} radius="panel" />
      ) : visible.length === 0 ? (
        rows.length ? (
          <p className="fs-agents__empty">{t('No expert matches that search.')}</p>
        ) : (
          <EmptyState title={t('No experts yet')} body={t('Create one, then drop its books into the corpus.')} primaryAction={{ label: t('New expert'), icon: Plus, onClick: onNew }} />
        )
      ) : (
        <div className="fs-exp__grid">
          {visible.map((r) => (
            <div key={r.slug} className="fs-exp__card-wrap" data-testid="expert-card">
              <button type="button" className="fs-exp__card" data-off={!r.enabled || undefined} onClick={() => onOpen(r.slug)}>
                <span className="fs-exp__card-head">
                  <strong>{r.name}</strong>
                  {!r.enabled && <span className="fs-exp__off">{t('off')}</span>}
                </span>
                <span className="fs-exp__card-desc">{r.description || t('No description yet — say what this expert knows.')}</span>
                <span className="fs-exp__card-meta">
                  <span title={t('Model this expert reviews with')}>{r.model || 'auto'}</span>
                  <span title={t('Corpus files')}>
                    {tn(r.corpus_files, '{n} file', '{n} files')}
                  </span>
                  <span title={t('Indexed chunks')}>
                    {tn(r.chunks, '{n} chunk', '{n} chunks')}
                  </span>
                </span>
                <span className="fs-exp__card-counters">
                  <span className="fs-exp__ok" title={t('Corrections accepted')}>✓ {r.accepted}</span>
                  <span className="fs-exp__no" title={t('Corrections rejected')}>✕ {r.rejected}</span>
                </span>
              </button>
              <IconButton icon={Trash2} label={t('Delete {name}', { name: r.name })} size="sm" onClick={() => onDelete(r)} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Detail: profile, corpus, search, block ── */

function Detail({ slug, onBack, onChanged, flash }: { slug: string; onBack: () => void; onChanged: () => void; flash: (m: string) => void }) {
  const [detail, setDetail] = useState<ExpertDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ name: '', description: '', model: '', temperature: '0.2', top_p: '1', enabled: true, instructions: '', rubric: '' });
  const [busy, setBusy] = useState<'save' | 'upload' | 'reindex' | 'search' | 'block' | null>(null);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState<CorpusSearch | null>(null);
  const [block, setBlock] = useState<BlockPreview | null>(null);
  const [reindex, setReindex] = useState<ReindexResult | null>(null);
  const [confirmFile, setConfirmFile] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(
    async (keepForm: boolean) => {
      try {
        const d = await readExpert(slug);
        setDetail(d);
        if (!keepForm) {
          const e = d.expert;
          setForm({ name: e.name, description: e.description, model: e.model, temperature: String(e.temperature), top_p: String(e.top_p), enabled: e.enabled, instructions: e.instructions, rubric: e.rubric.join('\n') });
        }
        setError(null);
      } catch (e) {
        setError(`${t('Could not open {name}', { name: slug })}: ${e instanceof Error ? e.message : String(e)}`);
      }
    },
    [slug],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  const save = async () => {
    if (!form.name.trim()) {
      setError(t('An expert needs a name.'));
      return;
    }
    setBusy('save');
    try {
      const temp = Number(form.temperature);
      const p = Number(form.top_p);
      await updateExpert(slug, {
        name: form.name.trim(),
        description: form.description,
        model: form.model.trim(),
        enabled: form.enabled,
        instructions: form.instructions,
        rubric: rubricLines(form.rubric),
        ...(Number.isFinite(temp) ? { temperature: temp } : {}),
        ...(Number.isFinite(p) ? { top_p: p } : {}),
      });
      flash(t('Expert saved'));
      await load(false);
      onChanged();
    } catch (e) {
      setError(`${t('Could not save')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const upload = async (files: FileList | null) => {
    if (!files || !files.length) return;
    setBusy('upload');
    try {
      const res = await uploadCorpus(slug, files);
      const bad = res.rejected.length ? ` · ${tn(res.rejected.length, '{n} rejected', '{n} rejected#')}: ${res.rejected.map((r) => `${r.name} (${r.reason})`).join(', ')}` : '';
      flash(`${tn(res.uploaded.length, '{n} file added', '{n} files added')}${bad}`);
      await load(true);
      onChanged();
    } catch (e) {
      setError(`${t('Could not upload')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const removeFile = async (name: string) => {
    setConfirmFile(null);
    try {
      await deleteCorpusFile(slug, name);
      flash(t('{name} deleted from the corpus', { name }));
      await load(true);
      onChanged();
    } catch (e) {
      setError(`${t('Could not delete {name}', { name })}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const doReindex = async () => {
    setBusy('reindex');
    try {
      setReindex(await reindexExpert(slug));
      await load(true);
      onChanged();
    } catch (e) {
      setError(`${t('Could not reindex')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const doSearch = async () => {
    setBusy('search');
    try {
      setSearch(await searchCorpus(slug, query.trim()));
      setError(null);
    } catch (e) {
      setError(`${t('The search failed')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const doBlock = async () => {
    setBusy('block');
    try {
      setBlock(await previewBlock(slug, query.trim()));
      setError(null);
    } catch (e) {
      setError(`${t('Could not build the block')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  if (!detail) {
    return (
      <div className="fs-exp__detail">
        <Button variant="ghost" size="sm" icon={ArrowLeft} label={t('Experts')} onClick={onBack} />
        {error ? <div className="fs-wk__error">{error}</div> : <Skeleton label={t('Loading the expert')} height="200px" radius="panel" />}
      </div>
    );
  }
  const files = detail.files;
  const total = files.reduce((s, f) => s + f.bytes, 0);
  const field = (k: keyof typeof form) => ({ value: String(form[k]), onChange: (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setForm((f) => ({ ...f, [k]: e.target.value })) });
  return (
    <div className="fs-exp__detail" data-testid="expert-detail">
      <div className="fs-exp__detail-head">
        <Button variant="ghost" size="sm" icon={ArrowLeft} label={t('Experts')} onClick={onBack} />
        <h2 className="fs-exp__detail-title">{detail.expert.name}</h2>
        <code className="fs-def__slug">{detail.expert.slug}</code>
        <span className="fs-agents__counts">
          ✓ {detail.usage.accepted} {t('accepted')} · ✕ {detail.usage.rejected} {t('rejected')} · {tn(detail.usage.invocations, '{n} run', '{n} runs')}
        </span>
      </div>
      {error && <div className="fs-wk__error">{error}</div>}
      <form
        className="fs-exp__form"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <label className="fs-exp__field">
          <span>{t('Name')}</span>
          <input type="text" className="fs-field" maxLength={120} required {...field('name')} />
        </label>
        <label className="fs-exp__field">
          <span>{t('Description')}</span>
          <input type="text" className="fs-field" maxLength={300} placeholder={t('What this expert knows')} {...field('description')} />
        </label>
        <label className="fs-exp__field">
          <span>{t('Model')}</span>
          <input type="text" className="fs-field" placeholder="auto" spellCheck={false} {...field('model')} />
        </label>
        <label className="fs-exp__field fs-exp__field--narrow">
          <span>{t('Temperature')}</span>
          <input type="number" className="fs-field" min={0} max={2} step={0.05} {...field('temperature')} />
        </label>
        <label className="fs-exp__field fs-exp__field--narrow">
          <span>Top p</span>
          <input type="number" className="fs-field" min={0} max={1} step={0.05} {...field('top_p')} />
        </label>
        <label className="fs-switch">
          <input type="checkbox" checked={form.enabled} onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))} />
          <span>{t('Enabled')}</span>
        </label>
        <label className="fs-exp__field fs-exp__field--wide">
          <span>{t('Instructions — the standing orders it reviews by')}</span>
          <textarea className="fs-field" rows={5} placeholder={t('How this specialist reads a passage.')} {...field('instructions')} />
        </label>
        <label className="fs-exp__field fs-exp__field--wide">
          <span>{t('Rubric — one item per line')}</span>
          <textarea className="fs-field" rows={5} placeholder={t('One rule per line. Without a rubric a local corrector rambles.')} {...field('rubric')} />
        </label>
        <div className="fs-exp__form-actions">
          <Button type="submit" variant="primary" size="sm" label={t('Save expert')} loading={busy === 'save'} testId="exp-save" />
        </div>
      </form>

      <section className="fs-exp__section">
        <h3>
          Corpus{' '}
          <span className="fs-agents__counts">
            {tn(files.length, '{n} file', '{n} files')} · {tn(detail.chunks, '{n} chunk', '{n} chunks')} · {bytes(total)}
          </span>
        </h3>
        <ul className="fs-exp__files">
          {files.length === 0 && <li className="fs-agents__empty">{t('No files yet — add the books this expert corrects by.')}</li>}
          {files.map((f) => (
            <li key={f.name} className="fs-exp__file">
              <a href={corpusUrl(slug, f.name)} target="_blank" rel="noopener">
                {f.name}
              </a>
              <span className="fs-exp__file-meta">
                {bytes(f.bytes)} · {f.pages == null ? t('pages unknown') : tn(f.pages, '{n} page', '{n} pages')} · {tn(f.chunks, '{n} chunk', '{n} chunks')}
              </span>
              <IconButton icon={X} label={t('Delete {name} from the corpus', { name: f.name })} size="sm" onClick={() => setConfirmFile(f.name)} />
            </li>
          ))}
        </ul>
        <div className="fs-exp__corpus-actions">
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => void upload(e.target.files)} data-testid="exp-upload" />
          <Button size="sm" variant="secondary" icon={FileUp} label={t('Add to corpus')} loading={busy === 'upload'} onClick={() => fileRef.current?.click()} />
          <Button size="sm" variant="ghost" icon={RefreshCw} label={t('Reindex')} loading={busy === 'reindex'} onClick={() => void doReindex()} />
          {reindex && (
            <span className="fs-exp__reindex">
              <b>{reindex.indexed}</b> {t('indexed')} · <b>{reindex.skipped}</b> {t('skipped')} · <b>{reindex.removed}</b> {t('removed')} · <b>{reindex.chunks}</b> {t('chunks')} · <b>{reindex.seconds.toFixed(2)}s</b>
            </span>
          )}
        </div>
        <p className="fs-agents__note">
          {t('Indexed')} {detail.indexed_at || t('never')}
          {detail.collection ? ` · ${detail.collection}` : ''}
        </p>
      </section>

      <section className="fs-exp__section">
        <h3>{t('Search the corpus')}</h3>
        <form
          className="fs-exp__search-form"
          onSubmit={(e) => {
            e.preventDefault();
            void doSearch();
          }}
        >
          <input type="search" className="fs-field" value={query} placeholder={t('A phrase from the books')} aria-label={t('Search this corpus')} onChange={(e) => setQuery(e.target.value)} />
          <Button type="submit" size="sm" variant="secondary" icon={Search} label={t('Search')} loading={busy === 'search'} />
          <Button size="sm" variant="ghost" label={t('Show what the model sees')} loading={busy === 'block'} onClick={() => void doBlock()} />
        </form>
        {search && (
          <div className="fs-exp__search-out">
            {search.hits.length === 0 ? (
              <p className="fs-agents__empty">{t('Nothing in this corpus matches {what}.', { what: search.query ? `«${search.query}»` : t('that') })}</p>
            ) : (
              <>
                {search.degraded && <p className="fs-agents__note">{t('Lexical only — this expert has no embedding collection yet. The hits are real, the ranking is just simpler.')}</p>}
                <ul className="fs-exp__hits">
                  {search.hits.map((h) => (
                    <li key={h.chunk_id || `${h.source}-${h.start_line}`} className="fs-exp__hit">
                      <a href={corpusUrl(slug, h.source)} target="_blank" rel="noopener">
                        {h.source || t('unknown source')}, {pageLabel(h)}
                      </a>
                      <span className="fs-exp__score" title={`${t('Score')} (${search.tier})`}>
                        {h.score.toFixed(3)}
                      </span>
                      <p>{h.text.slice(0, 400)}</p>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
        {block && (
          <div className="fs-exp__block-out">
            {!block.text ? (
              <p className="fs-agents__empty">{t('The block is empty for that query — the model would be given nothing from this corpus.')}</p>
            ) : (
              <>
                <p className="fs-agents__note">
                  {t('{a} of {b} characters', { a: block.chars, b: block.budget })} · {tn(block.chunk_ids.length, '{n} chunk', '{n} chunks')}
                  {block.degraded ? t(' · lexical only') : ''}
                </p>
                <pre className="fs-context">{block.text}</pre>
              </>
            )}
          </div>
        )}
      </section>

      {confirmFile && (
        <Dialog open onOpenChange={(o) => !o && setConfirmFile(null)} title={t('Delete from the corpus')} description={t('Remove "{name}" from the corpus? Its chunks leave the index.', { name: confirmFile })} footer={<><Button variant="ghost" label={t('Cancel')} onClick={() => setConfirmFile(null)} /><Button variant="danger-solid" label={t('Delete')} onClick={() => void removeFile(confirmFile)} /></>} />
      )}
    </div>
  );
}

/* ── Review: typed span deltas over the ORIGINAL text ── */

type Decision = 'accepted' | 'rejected';

function MarkedText({ text, deltas, decisions }: { text: string; deltas: ReviewDelta[]; decisions: Record<string, Decision> }) {
  const spans = deltas.filter((d) => d.span.start !== null && d.span.end !== null && d.span.start! >= 0 && d.span.start! <= d.span.end! && d.span.end! <= text.length).sort((a, b) => a.span.start! - b.span.start! || a.span.end! - b.span.end!);
  const out: ReactNode[] = [];
  let cursor = 0;
  for (const d of spans) {
    const start = d.span.start!;
    const end = d.span.end!;
    if (start < cursor) continue; // overlaps are resolved server-side
    out.push(text.slice(cursor, start));
    const piece = text.slice(start, end);
    out.push(
      <mark key={d.id} className="fs-exr__mark" data-severity={d.severity} data-state={decisions[d.id] ?? 'pending'} title={`${d.id}: ${d.rule || d.op}`}>
        {piece || <span className="fs-exr__caret" aria-hidden="true">{t('⟨insert⟩')}</span>}
      </mark>,
    );
    cursor = end;
  }
  out.push(text.slice(cursor));
  return <div className="fs-exr__text">{out}</div>;
}

function Review({ flash, onBack, onChanged }: { flash: (m: string) => void; onBack: () => void; onChanged: () => void }) {
  const [raw, setRaw] = useState<unknown>(null);
  const [text, setText] = useState('');
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pasteJson, setPasteJson] = useState('');
  const [pasteText, setPasteText] = useState('');

  const data: ReviewResult = useMemo(() => reviewFrom(raw), [raw]);
  const counts = useMemo(() => {
    let accepted = 0;
    let rejected = 0;
    for (const v of Object.values(decisions)) {
      if (v === 'accepted') accepted += 1;
      else if (v === 'rejected') rejected += 1;
    }
    return { accepted, rejected };
  }, [decisions]);
  const applied = useMemo(
    () =>
      applyAcceptedDeltas(
        text,
        data.deltas,
        data.deltas.filter((d) => decisions[d.id] === 'accepted').map((d) => d.id),
      ),
    [text, data, decisions],
  );

  const decide = (id: string, choice: Decision) => {
    setDecisions((m) => {
      const next = { ...m };
      if (next[id] === choice) delete next[id];
      else next[id] = choice;
      return next;
    });
    setSent(false);
  };

  const acceptJson = () => {
    try {
      const parsed = JSON.parse(pasteJson) as unknown;
      setRaw(parsed);
      setDecisions({});
      setText(reviewFrom(parsed).text);
      setError(null);
    } catch (e) {
      setError(`${t('That is not a review result')}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const copyResult = async () => {
    try {
      await navigator.clipboard.writeText(applied);
      flash(t('Result copied'));
    } catch {
      setError(t('The browser refused the clipboard — select the result and copy it by hand.'));
    }
  };

  const report = async () => {
    if (!data.expert.slug) {
      setError(t('This result does not name an expert, so there is nothing to report the outcome to.'));
      return;
    }
    try {
      await sendFeedback(data.expert.slug, counts.accepted, counts.rejected);
      setSent(true);
      setError(null);
      flash(t('Reported {a} accepted, {b} rejected', { a: counts.accepted, b: counts.rejected }));
      onChanged();
    } catch (e) {
      setError(`${t('Could not report the outcome')}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const head = (
    <div className="fs-exp__detail-head">
      <Button variant="ghost" size="sm" icon={ArrowLeft} label={t('Experts')} onClick={onBack} />
      <h2 className="fs-exp__detail-title">{data.expert.name || t('Review')}</h2>
      {data.expert.model && <code className="fs-def__slug">{data.expert.model}</code>}
    </div>
  );

  if (!data.deltas.length && !data.rejected.length) {
    return (
      <div className="fs-exr" data-testid="expert-review">
        {head}
        {error && <div className="fs-wk__error">{error}</div>}
        <p className="fs-agents__empty">
          {t('No review loaded. Ask an expert to review a passage (the')} <code>expert_review</code> {t('tool), or paste a result here.')}
        </p>
        <form
          className="fs-exr__paste"
          onSubmit={(e) => {
            e.preventDefault();
            acceptJson();
          }}
        >
          <textarea className="fs-field" rows={6} value={pasteJson} placeholder={t('{"expert": {...}, "deltas": [...], "text": "the passage"}')} spellCheck={false} onChange={(e) => setPasteJson(e.target.value)} data-testid="exr-json" />
          <Button type="submit" size="sm" variant="secondary" label={t('Render this review')} />
        </form>
      </div>
    );
  }
  if (!text) {
    return (
      <div className="fs-exr" data-testid="expert-review">
        {head}
        <p className="fs-agents__note">
          {tn(data.deltas.length, '{n} correction', '{n} corrections')} — {t('but the result does not carry the text they were made against. Paste the reviewed passage to see the spans.')}
        </p>
        <form
          className="fs-exr__paste"
          onSubmit={(e) => {
            e.preventDefault();
            setText(pasteText);
          }}
        >
          <textarea className="fs-field" rows={8} value={pasteText} placeholder={t('The passage that was reviewed')} spellCheck={false} onChange={(e) => setPasteText(e.target.value)} />
          <Button type="submit" size="sm" variant="secondary" label={t('Use this text')} />
        </form>
      </div>
    );
  }
  return (
    <div className="fs-exr" data-testid="expert-review">
      {head}
      {error && <div className="fs-wk__error">{error}</div>}
      <p className="fs-exr__counts">
        <span>
          <b>{data.deltas.length}</b> {data.deltas.length === 1 ? t('correction') : t('corrections')}
        </span>
        <span className="fs-exr__count-corpus">
          <b>{data.anchored_count}</b> {t('anchored to the corpus')}
        </span>
        <span className="fs-exr__count-opinion">
          <b>{data.opinion_count}</b> {t("the model's own opinion")}
        </span>
        <span>
          <b>{data.rejected.length}</b> {t('refused by the parser')}
        </span>
        <span>
          <b>{counts.accepted}</b> {t('accepted')} · <b>{counts.rejected}</b> {t('rejected')}
        </span>
      </p>
      {data.degraded && <p className="fs-agents__note">{t('The corpus answered degraded for at least one scene — lexical only, or a scene whose model call failed. Read the labels below accordingly.')}</p>}
      <MarkedText text={text} deltas={data.deltas} decisions={decisions} />
      <div className="fs-exr__cards">
        {data.deltas.map((d) => {
          const state = decisions[d.id] ?? 'pending';
          return (
            <article key={d.id} className="fs-exr__card" data-severity={d.severity} data-state={state} data-testid="exr-card">
              <header className="fs-exr__card-head">
                <span className="fs-exr__sev" data-severity={d.severity}>
                  {d.severity}
                </span>
                <span className="fs-exr__op">{d.op}</span>
                <span className="fs-exr__rule">{d.rule || t('no rubric rule named')}</span>
                {d.label ? <span className="fs-exr__label" data-label={d.label === 'corpus' ? 'corpus' : 'opinion'}>{d.label}</span> : <span className="fs-exr__label" data-label="none">{t('no label in the result')}</span>}
              </header>
              <p className="fs-exr__rationale">{d.rationale || t('No rationale given.')}</p>
              <div className="fs-exr__diff">
                {d.op !== 'ADD' && <del>{d.quote}</del>}
                {d.op !== 'KILL' && <ins>{d.replacement}</ins>}
              </div>
              {d.citations.length ? (
                <div className="fs-exr__cites">
                  {d.citations.map((c, i) => (
                    <span key={i} className="fs-exr__cite">
                      {c.marker && <span className="fs-exr__cite-marker">{c.marker}</span>}
                      {c.source && data.expert.slug ? (
                        <a href={corpusUrl(data.expert.slug, c.source)} target="_blank" rel="noopener">
                          {refOf(c)}
                        </a>
                      ) : (
                        refOf(c)
                      )}
                      {!c.known && <span className="fs-exr__cite-unknown" title={t('This marker is not in the block the model was given')}>{t('unknown marker')}</span>}
                    </span>
                  ))}
                </div>
              ) : (
                <div className="fs-exr__cites" data-none>
                  {t('No citation — nothing in the corpus was named.')}
                </div>
              )}
              {(d.relocated || d.notes.length > 0) && <p className="fs-exr__notes">{[...(d.relocated ? [t('span relocated to the quote')] : []), ...d.notes].join(' · ')}</p>}
              <footer className="fs-exr__card-actions">
                <Button size="sm" variant={state === 'accepted' ? 'primary' : 'secondary'} label={t('Accept')} onClick={() => decide(d.id, 'accepted')} />
                <Button size="sm" variant={state === 'rejected' ? 'danger' : 'secondary'} label={t('Reject')} onClick={() => decide(d.id, 'rejected')} />
                <span className="fs-exr__conf" title={t('Confidence from the anchoring layer that passed')}>
                  {d.confidence.toFixed(2)}
                </span>
              </footer>
            </article>
          );
        })}
      </div>
      {data.rejected.length > 0 && (
        <details className="fs-exr__dropped">
          <summary>
            {tn(data.rejected.length, '{n} correction the parser refused', '{n} corrections the parser refused')}
          </summary>
          <ul>
            {data.rejected.map((r, i) => (
              <li key={i}>
                <code>{r.id || '?'}</code> <span>{r.reason || t('no reason given')}</span>
                {r.quote && <span className="fs-exr__dropped-quote">«{r.quote}»</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="fs-exr__result-head">
        <h3>{t('Result')}</h3>
        <Button size="sm" variant="ghost" label={t('Copy result')} onClick={() => void copyResult()} />
        <Button size="sm" variant="secondary" label={sent ? t('Feedback sent') : t('Send feedback')} disabled={!data.expert.slug || sent} onClick={() => void report()} />
      </div>
      <pre className="fs-context">{applied}</pre>
    </div>
  );
}

/* ── The tab ── */

export function Experts() {
  const [rows, setRows] = useState<ExpertSummary[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>({ kind: 'gallery' });
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState(false);
  const [toDelete, setToDelete] = useState<ExpertSummary | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const flash = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 3000);
  }, []);

  const load = useCallback(async () => {
    try {
      const data = await listExperts();
      setRows(data.experts);
      setEnabled(data.enabled);
      setError(null);
    } catch (e) {
      setError(`${t('Could not load the experts')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async () => {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    try {
      const created = await createExpert({ name });
      setCreating(false);
      setNewName('');
      await load();
      if (created.slug) setView({ kind: 'detail', slug: created.slug });
    } catch (e) {
      setError(`${t('Could not create the expert')}: ${e instanceof Error ? e.message : String(e)}`);
      setCreating(false);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!toDelete) return;
    const row = toDelete;
    setToDelete(null);
    try {
      await deleteExpert(row.slug);
      flash(t('Expert deleted'));
      await load();
    } catch (e) {
      setError(`${t('Could not delete {name}', { name: row.slug })}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <div className="fs-exp" data-testid="experts">
      {view.kind === 'gallery' && <Gallery rows={rows} enabled={enabled} loading={loading} error={error} onOpen={(slug) => setView({ kind: 'detail', slug })} onNew={() => setCreating(true)} onDelete={setToDelete} onReview={() => setView({ kind: 'review' })} />}
      {view.kind === 'detail' && <Detail slug={view.slug} onBack={() => setView({ kind: 'gallery' })} onChanged={() => void load()} flash={flash} />}
      {view.kind === 'review' && <Review flash={flash} onBack={() => setView({ kind: 'gallery' })} onChanged={() => void load()} />}

      {creating && (
        <Dialog
          open
          onOpenChange={(o) => !o && setCreating(false)}
          title={t('New expert')}
          description={t('What should this expert be called?')}
          footer={
            <>
              <Button variant="ghost" label={t('Cancel')} onClick={() => setCreating(false)} />
              <Button variant="primary" label={t('Create')} loading={busy} disabled={!newName.trim()} onClick={() => void create()} testId="exp-create" />
            </>
          }
        >
          <input
            type="text"
            className="fs-field"
            autoFocus
            value={newName}
            placeholder={t('e.g. Brenner on craft')}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void create();
              }
            }}
            data-testid="exp-name"
          />
        </Dialog>
      )}
      {toDelete && (
        <Dialog
          open
          onOpenChange={(o) => !o && setToDelete(null)}
          title={t('Delete expert')}
          description={t('Delete "{name}"? Its corpus files and index go with it.', { name: toDelete.name })}
          footer={
            <>
              <Button variant="ghost" label={t('Cancel')} onClick={() => setToDelete(null)} />
              <Button variant="danger-solid" label={t('Delete')} onClick={() => void remove()} />
            </>
          }
        />
      )}
      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
