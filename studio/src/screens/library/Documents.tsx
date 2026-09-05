import { Archive, ArchiveRestore, ChevronDown, ChevronUp, Copy, Download, ExternalLink, FileText, MoreHorizontal, Sparkles, Trash2, Upload } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton } from '../../components';
import { deleteDoc, docFilename, duplicateDoc, exportDocsZip, getDoc, importDocumentFiles, loadDocLibrary, setDocArchived, tidyDocuments, type LibraryDoc, type LibrarySort } from '../../adapters/documents';
import { relativeTime } from '../../adapters/home';
import { t, tn } from '../../i18n';
import { BulkBar, downloadBlob, Highlight, SelectToggle, useSelection } from './parts';

const PAGE = 50;

/**
 * The documents the assistant and the person wrote, as a library: language
 * chips with counts, search, sort, import from disk (PDF, Word, sheets,
 * code, text), a tidy pass, and per-document open / duplicate / export /
 * archive / delete with a bulk bar for many at once.
 */
export function DocumentsLibrary({ query, say, archived = false }: { query: string; say: (m: string) => void; archived?: boolean }) {
  const navigate = useNavigate();
  const [docs, setDocs] = useState<LibraryDoc[] | null>(null);
  const [total, setTotal] = useState(0);
  const [languages, setLanguages] = useState<Record<string, number>>({});
  const [sessionCount, setSessionCount] = useState(0);
  const [language, setLanguage] = useState('');
  const [sort, setSort] = useState<LibrarySort>('recent');
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [full, setFull] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [confirm, setConfirm] = useState<{ ids: string[] } | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const count = useRef(0);
  const sel = useSelection<LibraryDoc>();

  const load = useCallback(
    async (append = false, signal?: AbortSignal) => {
      try {
        const page = await loadDocLibrary({ search: query, language, sort, offset: append ? count.current : 0, limit: PAGE, archived }, signal);
        if (signal?.aborted) return;
        setDocs((cur) => {
          const next = append && cur ? [...cur, ...page.documents] : page.documents;
          count.current = next.length;
          return next;
        });
        setTotal(page.total);
        setLanguages(page.languages);
        setSessionCount(page.sessionCount);
        setError(null);
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      }
    },
    [query, language, sort, archived],
  );

  useEffect(() => {
    const ac = new AbortController();
    setDocs(null);
    void load(false, ac.signal);
    return () => ac.abort();
  }, [load]);

  const act = async (what: string, work: () => Promise<void>, done?: string) => {
    setBusy(what);
    try {
      await work();
      if (done) say(done);
      await load(false);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const expand = async (doc: LibraryDoc) => {
    if (expanded === doc.id) {
      setExpanded(null);
      return;
    }
    setExpanded(doc.id);
    if (!(doc.id in full)) {
      try {
        const d = await getDoc(doc.id);
        setFull((cur) => ({ ...cur, [doc.id]: d.content }));
      } catch (e) {
        setFull((cur) => ({ ...cur, [doc.id]: t('Could not read the document: {error}', { error: (e as Error).message }) }));
      }
    }
  };

  const exportDocs = async (ids: string[]) => {
    if (!ids.length) return;
    if (ids.length > 5) {
      await act('export', async () => downloadBlob(await exportDocsZip(ids), 'documents.zip'), tn(ids.length, 'Exported {n} document (zip)', 'Exported {n} documents (zip)'));
      return;
    }
    await act(
      'export',
      async () => {
        for (const id of ids) {
          const d = await getDoc(id);
          downloadBlob(new Blob([d.content], { type: 'text/plain' }), docFilename(d));
        }
      },
      tn(ids.length, 'Exported {n} document', 'Exported {n} documents'),
    );
  };

  const importFiles = async (files: File[]) => {
    if (!files.length) return;
    setBusy('import');
    setProgress({ done: 0, total: files.length });
    try {
      const out = await importDocumentFiles(files, (done, tot) => setProgress({ done, total: tot }));
      if (out.failed.length) say(t('{n} imported; failed: {list}', { n: out.imported, list: out.failed.join(' · ') }));
      else say(tn(out.imported, '{n} document imported', '{n} documents imported'));
      await load(false);
    } finally {
      setBusy(null);
      setProgress(null);
    }
  };

  const totalAll = Object.values(languages).reduce((a, b) => a + b, 0);
  const langs = Object.entries(languages).sort((a, b) => b[1] - a[1]);

  return (
    <div
      className="fs-gal fs-lib"
      data-dragging={dragging || undefined}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes('Files')) {
          e.preventDefault();
          setDragging(true);
        }
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        void importFiles(Array.from(e.dataTransfer.files));
      }}
      data-testid="documents"
    >
      <div className="fs-gal__toolbar">
        <div className="fs-gal__chips" role="group" aria-label={t('Filter by language')}>
          <button type="button" className="fs-chip" data-on={!language || undefined} onClick={() => setLanguage('')}>
            {t('All')} <span className="fs-gal__n">{totalAll}</span>
          </button>
          {langs.map(([lang, n]) => (
            <button key={lang} type="button" className="fs-chip" data-on={language === lang || undefined} onClick={() => setLanguage(language === lang ? '' : lang)}>
              {lang} <span className="fs-gal__n">{n}</span>
            </button>
          ))}
        </div>
        <span className="fs-gal__spacer" />
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as LibrarySort)} aria-label={t('Sort')}>
          <option value="recent">{t('Newest first')}</option>
          <option value="oldest">{t('Oldest first')}</option>
          <option value="alpha">{t('A to Z')}</option>
          <option value="most-versions">{t('Most versions')}</option>
        </select>
        {!archived && (
          <>
            <Button variant="ghost" size="sm" icon={Upload} label={t('Import')} title={t('PDF, Word, spreadsheets, code and text files; or drop them on the list')} loading={busy === 'import'} onClick={() => fileRef.current?.click()} testId="docs-import" />
            <input ref={fileRef} type="file" multiple hidden accept=".pdf,.docx,.xlsx,.xls,.ods,.csv,.tsv,.md,.txt,.log,.json,.yml,.yaml,.py,.js,.jsx,.ts,.tsx,.html,.htm,.css,.scss,.sh,.sql,.rs,.go,.java,.c,.h,.cpp,.hpp,.rb,.php,.xml,.toml,.ini,.cfg,.conf,.env" onChange={(e) => { const files = Array.from(e.target.files ?? []); e.target.value = ''; void importFiles(files); }} />
            <Button
              variant="ghost"
              size="sm"
              icon={Sparkles}
              label={t('Tidy')}
              title={t('Fix empty titles, drop broken documents, and let a model flag the accidental ones')}
              loading={busy === 'tidy'}
              onClick={() =>
                void act('tidy', async () => {
                  const r = await tidyDocuments(true);
                  say(r.deleted || r.fixedTitles ? t('Tidy: {deleted} removed, {fixed} titles fixed. {message}', { deleted: r.deleted, fixed: r.fixedTitles, message: r.message }) : t('Already tidy'));
                })
              }
            />
          </>
        )}
        <SelectToggle selecting={sel.selecting} onToggle={() => (sel.selecting ? sel.leave() : sel.enter())} testId="docs-select" />
      </div>

      <p className="fs-gal__stats">
        {query || language ? t('{n} of {total} documents', { n: total, total: totalAll }) : tn(totalAll, '{n} document', '{n} documents')}
        {sessionCount > 0 && ` · ${tn(sessionCount, 'from {n} chat', 'from {n} chats')}`}
        {progress && ` · ${t('Importing {done} of {total}…', { done: progress.done, total: progress.total })}`}
      </p>

      {sel.selecting && docs && (
        <BulkBar items={docs} selected={sel.selected} onAll={(on) => sel.all(docs, on)} label={t('Selection')}>
          {!archived && <Button variant="ghost" size="sm" icon={Archive} label={t('Archive')} disabled={!sel.selected.size} onClick={() => void act('archive', async () => { for (const id of sel.selected) await setDocArchived(id, true); sel.leave(); }, t('Archived'))} />}
          {archived && <Button variant="ghost" size="sm" icon={ArchiveRestore} label={t('Restore')} disabled={!sel.selected.size} onClick={() => void act('archive', async () => { for (const id of sel.selected) await setDocArchived(id, false); sel.leave(); }, t('Restored'))} />}
          <Button variant="ghost" size="sm" icon={Copy} label={t('Duplicate')} disabled={!sel.selected.size} onClick={() => void act('dup', async () => { for (const id of sel.selected) await duplicateDoc(id); sel.leave(); }, t('Duplicated'))} />
          <Button variant="ghost" size="sm" icon={Download} label={t('Export')} disabled={!sel.selected.size} loading={busy === 'export'} onClick={() => void exportDocs([...sel.selected])} />
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} disabled={!sel.selected.size} onClick={() => setConfirm({ ids: [...sel.selected] })} />
        </BulkBar>
      )}

      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!docs && !error && <Skeleton label={t('Loading documents')} count={4} height="72px" radius="panel" />}
      {docs && !docs.length && <EmptyState icon={FileText} title={archived ? t('No archived documents') : query || language ? t('Nothing matches') : t('No documents yet')} body={archived ? t('Archived documents wait here until you restore or delete them.') : t('Import files, or ask the assistant to write one: documents it creates in a chat land here too.')} />}

      {docs && docs.length > 0 && (
        <ul className="fs-lib__list">
          {docs.map((doc) => {
            const open = expanded === doc.id;
            return (
              <li key={doc.id} className="fs-lib__item" data-open={open || undefined} data-selected={sel.selected.has(doc.id) || undefined}>
                <div className="fs-lib__row">
                  {sel.selecting && <input type="checkbox" className="fs-lib__cb" checked={sel.selected.has(doc.id)} onChange={() => sel.toggle(doc.id)} aria-label={t('Select {name}', { name: doc.title })} />}
                  <button type="button" className="fs-lib__main" onClick={() => void expand(doc)} aria-expanded={open}>
                    <span className="fs-lib__lang" aria-hidden="true">
                      {doc.language.slice(0, 3)}
                    </span>
                    <span className="fs-lib__text">
                      <span className="fs-lib__title">
                        <Highlight text={doc.title} needle={query} />
                        {doc.versionCount > 1 && <span className="fs-lib__badge">v{doc.versionCount}</span>}
                      </span>
                      <span className="fs-lib__meta">{[doc.language, doc.sessionName, relativeTime(doc.updatedAt ?? doc.createdAt)].filter(Boolean).join(' · ')}</span>
                    </span>
                    {open ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                  </button>
                  <Button size="sm" variant="ghost" icon={ExternalLink} label={t('Open')} onClick={() => navigate(`/documents/${encodeURIComponent(doc.id)}`)} title={t('Open in the editor')} />
                  <Menu
                    align="end"
                    trigger={<IconButton icon={MoreHorizontal} label={t('Document actions')} size="sm" />}
                    items={[
                      { label: t('Open in the editor'), icon: ExternalLink, onSelect: () => navigate(`/documents/${encodeURIComponent(doc.id)}`) },
                      ...(doc.sessionId ? [{ label: t('Open in its chat'), icon: ExternalLink, onSelect: () => navigate(`/studio?s=${encodeURIComponent(doc.sessionId ?? '')}&doc=${encodeURIComponent(doc.id)}`) }] : []),
                      { label: t('Duplicate'), icon: Copy, onSelect: () => void act('dup', async () => { await duplicateDoc(doc.id); }, t('Duplicated')) },
                      { label: t('Export'), icon: Download, onSelect: () => void exportDocs([doc.id]) },
                      null,
                      archived ? { label: t('Restore'), icon: ArchiveRestore, onSelect: () => void act('archive', () => setDocArchived(doc.id, false), t('Restored')) } : { label: t('Archive'), icon: Archive, onSelect: () => void act('archive', () => setDocArchived(doc.id, true), t('Archived')) },
                      { label: t('Delete'), icon: Trash2, variant: 'danger', onSelect: () => setConfirm({ ids: [doc.id] }) },
                    ]}
                  />
                </div>
                {open && (
                  <pre className="fs-lib__preview" data-lang={doc.language}>
                    {doc.id in full ? full[doc.id] || t('Empty document') : doc.preview || t('Loading…')}
                  </pre>
                )}
                {!open && doc.preview && <pre className="fs-lib__snippet">{doc.preview.slice(0, 240)}</pre>}
              </li>
            );
          })}
        </ul>
      )}

      {docs && docs.length < total && (
        <div className="fs-gal__more">
          <Button variant="secondary" label={t('Show more ({n} left)', { n: total - docs.length })} onClick={() => void load(true)} />
        </div>
      )}

      <Dialog
        open={!!confirm}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={confirm && confirm.ids.length > 1 ? t('Delete {n} documents?', { n: confirm.ids.length }) : t('Delete this document?')}
        description={t('The document and every version of it are removed. This cannot be undone.')}
        testId="docs-delete"
        footer={
          <>
            <Button variant="ghost" label={t('Cancel')} onClick={() => setConfirm(null)} />
            <Button
              variant="danger-solid"
              label={t('Delete')}
              onClick={() => {
                const ids = confirm?.ids ?? [];
                setConfirm(null);
                void act('delete', async () => { for (const id of ids) await deleteDoc(id); sel.leave(); }, tn(ids.length, '{n} document deleted', '{n} documents deleted'));
              }}
            />
          </>
        }
      />
    </div>
  );
}
