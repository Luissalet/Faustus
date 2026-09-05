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
        <p className="fs-prose">Un especialista con su propio corpus: tus libros, indexados por página, y correcciones que citan la página de la que salen.</p>
        {!enabled && (
          <p className="fs-agents__note">
            Los expertos están apagados en Ajustes (<code>agent_experts</code>): no se inyecta nada en un turno. Todo lo de aquí se sigue editando igual.
          </p>
        )}
      </div>
      <div className="fs-agents__toolbar">
        <label className="fs-agents__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder="Buscar expertos…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Buscar expertos" />
        </label>
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label="Ordenar expertos">
          <option value="name">Nombre</option>
          <option value="corpus">Tamaño del corpus</option>
          <option value="accepted">Aceptadas</option>
        </select>
        <span className="fs-agents__spacer" />
        <Button variant="ghost" size="sm" icon={ClipboardCheck} label="Panel de revisión" onClick={onReview} />
        <Button variant="primary" size="sm" icon={Plus} label="Nuevo experto" onClick={onNew} testId="exp-new" />
      </div>
      {error && <div className="fs-wk__error">{error}</div>}
      {loading ? (
        <Skeleton label="Cargando los expertos" height="120px" count={3} radius="panel" />
      ) : visible.length === 0 ? (
        rows.length ? (
          <p className="fs-agents__empty">Ningún experto coincide con esa búsqueda.</p>
        ) : (
          <EmptyState title="Todavía no hay expertos" body="Crea uno y luego deja caer sus libros en el corpus." primaryAction={{ label: 'Nuevo experto', icon: Plus, onClick: onNew }} />
        )
      ) : (
        <div className="fs-exp__grid">
          {visible.map((r) => (
            <div key={r.slug} className="fs-exp__card-wrap" data-testid="expert-card">
              <button type="button" className="fs-exp__card" data-off={!r.enabled || undefined} onClick={() => onOpen(r.slug)}>
                <span className="fs-exp__card-head">
                  <strong>{r.name}</strong>
                  {!r.enabled && <span className="fs-exp__off">apagado</span>}
                </span>
                <span className="fs-exp__card-desc">{r.description || 'Sin descripción aún — di qué sabe este experto.'}</span>
                <span className="fs-exp__card-meta">
                  <span title="Modelo con el que revisa">{r.model || 'auto'}</span>
                  <span title="Ficheros del corpus">
                    {r.corpus_files} fichero{r.corpus_files === 1 ? '' : 's'}
                  </span>
                  <span title="Fragmentos indexados">
                    {r.chunks} fragmento{r.chunks === 1 ? '' : 's'}
                  </span>
                </span>
                <span className="fs-exp__card-counters">
                  <span className="fs-exp__ok" title="Correcciones aceptadas">✓ {r.accepted}</span>
                  <span className="fs-exp__no" title="Correcciones rechazadas">✕ {r.rejected}</span>
                </span>
              </button>
              <IconButton icon={Trash2} label={`Borrar ${r.name}`} size="sm" onClick={() => onDelete(r)} />
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
        setError(`No he podido abrir ${slug}: ${e instanceof Error ? e.message : String(e)}`);
      }
    },
    [slug],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  const save = async () => {
    if (!form.name.trim()) {
      setError('Un experto necesita un nombre.');
      return;
    }
    setBusy('save');
    try {
      const t = Number(form.temperature);
      const p = Number(form.top_p);
      await updateExpert(slug, {
        name: form.name.trim(),
        description: form.description,
        model: form.model.trim(),
        enabled: form.enabled,
        instructions: form.instructions,
        rubric: rubricLines(form.rubric),
        ...(Number.isFinite(t) ? { temperature: t } : {}),
        ...(Number.isFinite(p) ? { top_p: p } : {}),
      });
      flash('Experto guardado');
      await load(false);
      onChanged();
    } catch (e) {
      setError(`No he podido guardar: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const upload = async (files: FileList | null) => {
    if (!files || !files.length) return;
    setBusy('upload');
    try {
      const res = await uploadCorpus(slug, files);
      const bad = res.rejected.length ? ` · ${res.rejected.length} rechazado${res.rejected.length === 1 ? '' : 's'}: ${res.rejected.map((r) => `${r.name} (${r.reason})`).join(', ')}` : '';
      flash(`${res.uploaded.length} fichero${res.uploaded.length === 1 ? '' : 's'} añadido${res.uploaded.length === 1 ? '' : 's'}${bad}`);
      await load(true);
      onChanged();
    } catch (e) {
      setError(`No he podido subir: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const removeFile = async (name: string) => {
    setConfirmFile(null);
    try {
      await deleteCorpusFile(slug, name);
      flash(`${name} borrado del corpus`);
      await load(true);
      onChanged();
    } catch (e) {
      setError(`No he podido borrar ${name}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const doReindex = async () => {
    setBusy('reindex');
    try {
      setReindex(await reindexExpert(slug));
      await load(true);
      onChanged();
    } catch (e) {
      setError(`No he podido reindexar: ${e instanceof Error ? e.message : String(e)}`);
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
      setError(`La búsqueda falló: ${e instanceof Error ? e.message : String(e)}`);
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
      setError(`No he podido montar el bloque: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  if (!detail) {
    return (
      <div className="fs-exp__detail">
        <Button variant="ghost" size="sm" icon={ArrowLeft} label="Expertos" onClick={onBack} />
        {error ? <div className="fs-wk__error">{error}</div> : <Skeleton label="Cargando el experto" height="200px" radius="panel" />}
      </div>
    );
  }
  const files = detail.files;
  const total = files.reduce((s, f) => s + f.bytes, 0);
  const field = (k: keyof typeof form) => ({ value: String(form[k]), onChange: (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setForm((f) => ({ ...f, [k]: e.target.value })) });
  return (
    <div className="fs-exp__detail" data-testid="expert-detail">
      <div className="fs-exp__detail-head">
        <Button variant="ghost" size="sm" icon={ArrowLeft} label="Expertos" onClick={onBack} />
        <h2 className="fs-exp__detail-title">{detail.expert.name}</h2>
        <code className="fs-def__slug">{detail.expert.slug}</code>
        <span className="fs-agents__counts">
          ✓ {detail.usage.accepted} aceptadas · ✕ {detail.usage.rejected} rechazadas · {detail.usage.invocations} uso{detail.usage.invocations === 1 ? '' : 's'}
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
          <span>Nombre</span>
          <input type="text" className="fs-field" maxLength={120} required {...field('name')} />
        </label>
        <label className="fs-exp__field">
          <span>Descripción</span>
          <input type="text" className="fs-field" maxLength={300} placeholder="Qué sabe este experto" {...field('description')} />
        </label>
        <label className="fs-exp__field">
          <span>Modelo</span>
          <input type="text" className="fs-field" placeholder="auto" spellCheck={false} {...field('model')} />
        </label>
        <label className="fs-exp__field fs-exp__field--narrow">
          <span>Temperatura</span>
          <input type="number" className="fs-field" min={0} max={2} step={0.05} {...field('temperature')} />
        </label>
        <label className="fs-exp__field fs-exp__field--narrow">
          <span>Top p</span>
          <input type="number" className="fs-field" min={0} max={1} step={0.05} {...field('top_p')} />
        </label>
        <label className="fs-switch">
          <input type="checkbox" checked={form.enabled} onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))} />
          <span>Activado</span>
        </label>
        <label className="fs-exp__field fs-exp__field--wide">
          <span>Instrucciones — las órdenes con las que revisa</span>
          <textarea className="fs-field" rows={5} placeholder="Cómo lee un pasaje este especialista." {...field('instructions')} />
        </label>
        <label className="fs-exp__field fs-exp__field--wide">
          <span>Rúbrica — un punto por línea</span>
          <textarea className="fs-field" rows={5} placeholder="Una regla por línea. Sin rúbrica, un corrector local divaga." {...field('rubric')} />
        </label>
        <div className="fs-exp__form-actions">
          <Button type="submit" variant="primary" size="sm" label="Guardar experto" loading={busy === 'save'} testId="exp-save" />
        </div>
      </form>

      <section className="fs-exp__section">
        <h3>
          Corpus{' '}
          <span className="fs-agents__counts">
            {files.length} fichero{files.length === 1 ? '' : 's'} · {detail.chunks} fragmento{detail.chunks === 1 ? '' : 's'} · {bytes(total)}
          </span>
        </h3>
        <ul className="fs-exp__files">
          {files.length === 0 && <li className="fs-agents__empty">Sin ficheros aún — añade los libros con los que corrige este experto.</li>}
          {files.map((f) => (
            <li key={f.name} className="fs-exp__file">
              <a href={corpusUrl(slug, f.name)} target="_blank" rel="noopener">
                {f.name}
              </a>
              <span className="fs-exp__file-meta">
                {bytes(f.bytes)} · {f.pages == null ? 'páginas desconocidas' : `${f.pages} página${f.pages === 1 ? '' : 's'}`} · {f.chunks} fragmento{f.chunks === 1 ? '' : 's'}
              </span>
              <IconButton icon={X} label={`Borrar ${f.name} del corpus`} size="sm" onClick={() => setConfirmFile(f.name)} />
            </li>
          ))}
        </ul>
        <div className="fs-exp__corpus-actions">
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => void upload(e.target.files)} data-testid="exp-upload" />
          <Button size="sm" variant="secondary" icon={FileUp} label="Añadir al corpus" loading={busy === 'upload'} onClick={() => fileRef.current?.click()} />
          <Button size="sm" variant="ghost" icon={RefreshCw} label="Reindexar" loading={busy === 'reindex'} onClick={() => void doReindex()} />
          {reindex && (
            <span className="fs-exp__reindex">
              <b>{reindex.indexed}</b> indexados · <b>{reindex.skipped}</b> saltados · <b>{reindex.removed}</b> quitados · <b>{reindex.chunks}</b> fragmentos · <b>{reindex.seconds.toFixed(2)}s</b>
            </span>
          )}
        </div>
        <p className="fs-agents__note">
          Indexado {detail.indexed_at || 'nunca'}
          {detail.collection ? ` · ${detail.collection}` : ''}
        </p>
      </section>

      <section className="fs-exp__section">
        <h3>Buscar en el corpus</h3>
        <form
          className="fs-exp__search-form"
          onSubmit={(e) => {
            e.preventDefault();
            void doSearch();
          }}
        >
          <input type="search" className="fs-field" value={query} placeholder="Una frase de los libros" aria-label="Buscar en este corpus" onChange={(e) => setQuery(e.target.value)} />
          <Button type="submit" size="sm" variant="secondary" icon={Search} label="Buscar" loading={busy === 'search'} />
          <Button size="sm" variant="ghost" label="Ver lo que ve el modelo" loading={busy === 'block'} onClick={() => void doBlock()} />
        </form>
        {search && (
          <div className="fs-exp__search-out">
            {search.hits.length === 0 ? (
              <p className="fs-agents__empty">Nada en este corpus coincide con {search.query ? `«${search.query}»` : 'eso'}.</p>
            ) : (
              <>
                {search.degraded && <p className="fs-agents__note">Solo léxico — este experto aún no tiene colección de embeddings. Los resultados son reales; el orden, más simple.</p>}
                <ul className="fs-exp__hits">
                  {search.hits.map((h) => (
                    <li key={h.chunk_id || `${h.source}-${h.start_line}`} className="fs-exp__hit">
                      <a href={corpusUrl(slug, h.source)} target="_blank" rel="noopener">
                        {h.source || 'fuente desconocida'}, {pageLabel(h)}
                      </a>
                      <span className="fs-exp__score" title={`Puntuación (${search.tier})`}>
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
              <p className="fs-agents__empty">El bloque queda vacío para esa consulta — el modelo no recibiría nada de este corpus.</p>
            ) : (
              <>
                <p className="fs-agents__note">
                  {block.chars} de {block.budget} caracteres · {block.chunk_ids.length} fragmento{block.chunk_ids.length === 1 ? '' : 's'}
                  {block.degraded ? ' · solo léxico' : ''}
                </p>
                <pre className="fs-context">{block.text}</pre>
              </>
            )}
          </div>
        )}
      </section>

      {confirmFile && (
        <Dialog open onOpenChange={(o) => !o && setConfirmFile(null)} title="Borrar del corpus" description={`¿Quitar «${confirmFile}» del corpus? Sus fragmentos salen del índice.`} footer={<><Button variant="ghost" label="Cancelar" onClick={() => setConfirmFile(null)} /><Button variant="danger-solid" label="Borrar" onClick={() => void removeFile(confirmFile)} /></>} />
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
        {piece || <span className="fs-exr__caret" aria-hidden="true">⟨insertar⟩</span>}
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
      setError(`Eso no es un resultado de revisión: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const copyResult = async () => {
    try {
      await navigator.clipboard.writeText(applied);
      flash('Resultado copiado');
    } catch {
      setError('El navegador rechazó el portapapeles — selecciona el resultado y cópialo a mano.');
    }
  };

  const report = async () => {
    if (!data.expert.slug) {
      setError('Este resultado no nombra un experto, así que no hay a quién informar del desenlace.');
      return;
    }
    try {
      await sendFeedback(data.expert.slug, counts.accepted, counts.rejected);
      setSent(true);
      setError(null);
      flash(`Informado: ${counts.accepted} aceptadas, ${counts.rejected} rechazadas`);
      onChanged();
    } catch (e) {
      setError(`No he podido informar del desenlace: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const head = (
    <div className="fs-exp__detail-head">
      <Button variant="ghost" size="sm" icon={ArrowLeft} label="Expertos" onClick={onBack} />
      <h2 className="fs-exp__detail-title">{data.expert.name || 'Revisión'}</h2>
      {data.expert.model && <code className="fs-def__slug">{data.expert.model}</code>}
    </div>
  );

  if (!data.deltas.length && !data.rejected.length) {
    return (
      <div className="fs-exr" data-testid="expert-review">
        {head}
        {error && <div className="fs-wk__error">{error}</div>}
        <p className="fs-agents__empty">
          No hay ninguna revisión cargada. Pide a un experto que revise un pasaje (la herramienta <code>expert_review</code>), o pega un resultado aquí.
        </p>
        <form
          className="fs-exr__paste"
          onSubmit={(e) => {
            e.preventDefault();
            acceptJson();
          }}
        >
          <textarea className="fs-field" rows={6} value={pasteJson} placeholder='{"expert": {...}, "deltas": [...], "text": "el pasaje"}' spellCheck={false} onChange={(e) => setPasteJson(e.target.value)} data-testid="exr-json" />
          <Button type="submit" size="sm" variant="secondary" label="Mostrar esta revisión" />
        </form>
      </div>
    );
  }
  if (!text) {
    return (
      <div className="fs-exr" data-testid="expert-review">
        {head}
        <p className="fs-agents__note">
          {data.deltas.length} correcci{data.deltas.length === 1 ? 'ón' : 'ones'} — pero el resultado no trae el texto contra el que se hicieron. Pega el pasaje revisado para ver los tramos.
        </p>
        <form
          className="fs-exr__paste"
          onSubmit={(e) => {
            e.preventDefault();
            setText(pasteText);
          }}
        >
          <textarea className="fs-field" rows={8} value={pasteText} placeholder="El pasaje que se revisó" spellCheck={false} onChange={(e) => setPasteText(e.target.value)} />
          <Button type="submit" size="sm" variant="secondary" label="Usar este texto" />
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
          <b>{data.deltas.length}</b> correcci{data.deltas.length === 1 ? 'ón' : 'ones'}
        </span>
        <span className="fs-exr__count-corpus">
          <b>{data.anchored_count}</b> ancladas al corpus
        </span>
        <span className="fs-exr__count-opinion">
          <b>{data.opinion_count}</b> opinión del modelo
        </span>
        <span>
          <b>{data.rejected.length}</b> rechazadas por el parser
        </span>
        <span>
          <b>{counts.accepted}</b> aceptadas · <b>{counts.rejected}</b> rechazadas
        </span>
      </p>
      {data.degraded && <p className="fs-agents__note">El corpus respondió degradado en al menos una escena — solo léxico, o una escena cuya llamada al modelo falló. Lee las etiquetas de abajo con eso en cuenta.</p>}
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
                <span className="fs-exr__rule">{d.rule || 'sin regla de rúbrica'}</span>
                {d.label ? <span className="fs-exr__label" data-label={d.label === 'corpus' ? 'corpus' : 'opinion'}>{d.label}</span> : <span className="fs-exr__label" data-label="none">sin etiqueta en el resultado</span>}
              </header>
              <p className="fs-exr__rationale">{d.rationale || 'Sin justificación.'}</p>
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
                      {!c.known && <span className="fs-exr__cite-unknown" title="Este marcador no está en el bloque que se le dio al modelo">marcador desconocido</span>}
                    </span>
                  ))}
                </div>
              ) : (
                <div className="fs-exr__cites" data-none>
                  Sin cita — no se nombró nada del corpus.
                </div>
              )}
              {(d.relocated || d.notes.length > 0) && <p className="fs-exr__notes">{[...(d.relocated ? ['tramo reubicado a la cita'] : []), ...d.notes].join(' · ')}</p>}
              <footer className="fs-exr__card-actions">
                <Button size="sm" variant={state === 'accepted' ? 'primary' : 'secondary'} label="Aceptar" onClick={() => decide(d.id, 'accepted')} />
                <Button size="sm" variant={state === 'rejected' ? 'danger' : 'secondary'} label="Rechazar" onClick={() => decide(d.id, 'rejected')} />
                <span className="fs-exr__conf" title="Confianza de la capa de anclaje que pasó">
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
            {data.rejected.length} correcci{data.rejected.length === 1 ? 'ón' : 'ones'} que el parser rechazó
          </summary>
          <ul>
            {data.rejected.map((r, i) => (
              <li key={i}>
                <code>{r.id || '?'}</code> <span>{r.reason || 'sin motivo'}</span>
                {r.quote && <span className="fs-exr__dropped-quote">«{r.quote}»</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="fs-exr__result-head">
        <h3>Resultado</h3>
        <Button size="sm" variant="ghost" label="Copiar resultado" onClick={() => void copyResult()} />
        <Button size="sm" variant="secondary" label={sent ? 'Desenlace enviado' : 'Enviar desenlace'} disabled={!data.expert.slug || sent} onClick={() => void report()} />
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
      setError(`No he podido cargar los expertos: ${e instanceof Error ? e.message : String(e)}`);
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
      setError(`No he podido crear el experto: ${e instanceof Error ? e.message : String(e)}`);
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
      flash('Experto borrado');
      await load();
    } catch (e) {
      setError(`No he podido borrar ${row.slug}: ${e instanceof Error ? e.message : String(e)}`);
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
          title="Nuevo experto"
          description="¿Cómo se llama este experto?"
          footer={
            <>
              <Button variant="ghost" label="Cancelar" onClick={() => setCreating(false)} />
              <Button variant="primary" label="Crear" loading={busy} disabled={!newName.trim()} onClick={() => void create()} testId="exp-create" />
            </>
          }
        >
          <input
            type="text"
            className="fs-field"
            autoFocus
            value={newName}
            placeholder="p. ej. Brenner sobre oficio"
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
          title="Borrar experto"
          description={`¿Borrar «${toDelete.name}»? Sus ficheros del corpus y su índice se van con él.`}
          footer={
            <>
              <Button variant="ghost" label="Cancelar" onClick={() => setToDelete(null)} />
              <Button variant="danger-solid" label="Borrar" onClick={() => void remove()} />
            </>
          }
        />
      )}
      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
