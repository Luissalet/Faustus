import { Archive, ChevronDown, ChevronUp, FileUp, History as HistoryIcon, MoreHorizontal, Search, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton } from '../../components';
import {
  dateLabel,
  deleteConversation,
  importPath,
  importUpload,
  listConversations,
  readConversation,
  searchHistory,
  sourceLabel,
  SOURCES,
  type Conversation,
  type ConversationDetail,
  type HistoryStats,
  type ImportReport,
  type SearchResult,
} from '../../adapters/history';
import { t, tn } from '../../i18n';
import { BulkBar, Highlight, SelectToggle, useSelection } from './parts';

/**
 * Imported history (Library → Imported): somebody else's export brought
 * here — ChatGPT, Claude, LM Studio or one of this app's own — previewed
 * before a single row is written, then a normal searchable archive.
 *
 * The library's search box filters titles; "inside messages" switches to
 * the two-tier search over every message, and the answer always says
 * which lane produced it (`tier`, `degraded`).
 */
export function HistoryLibrary({ query, say }: { query: string; say: (m: string) => void }) {
  const [rows, setRows] = useState<Conversation[] | null>(null);
  const [stats, setStats] = useState<HistoryStats | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState('');
  const [deep, setDeep] = useState(false);
  const [search, setSearch] = useState<SearchResult | null | 'loading'>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, ConversationDetail | string>>({});
  const [confirm, setConfirm] = useState<string[] | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const sel = useSelection<Conversation>();

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const out = await listConversations({ source: source || undefined, q: deep ? '' : query }, signal);
        if (signal?.aborted) return;
        setRows(out.conversations);
        setStats(out.stats);
        setEnabled(out.enabled);
        setError(null);
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      }
    },
    [source, query, deep],
  );

  useEffect(() => {
    const ac = new AbortController();
    void load(ac.signal);
    return () => ac.abort();
  }, [load]);

  useEffect(() => {
    if (!deep || !query.trim()) {
      setSearch(null);
      return;
    }
    const ac = new AbortController();
    setSearch('loading');
    const timer = window.setTimeout(() => {
      searchHistory(query.trim(), source || undefined, 30, ac.signal)
        .then((r) => {
          if (!ac.signal.aborted) setSearch(r);
        })
        .catch((e: Error) => {
          if (!ac.signal.aborted) {
            setSearch(null);
            setError(e.message);
          }
        });
    }, 250);
    return () => {
      ac.abort();
      window.clearTimeout(timer);
    };
  }, [deep, query, source]);

  const expand = async (id: string) => {
    if (expanded === id) {
      setExpanded(null);
      return;
    }
    setExpanded(id);
    if (!(id in detail)) {
      try {
        const d = await readConversation(id);
        setDetail((cur) => ({ ...cur, [id]: d }));
      } catch (e) {
        setDetail((cur) => ({ ...cur, [id]: (e as Error).message }));
      }
    }
  };

  const remove = async (ids: string[]) => {
    setBusy('delete');
    try {
      for (const id of ids) await deleteConversation(id);
      say(tn(ids.length, '{n} conversation deleted', '{n} conversations deleted'));
      sel.leave();
      setConfirm(null);
      await load();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const sourcesShown = useMemo(() => {
    const seen = new Set<string>(SOURCES);
    for (const s of stats?.sources ?? []) seen.add(s.source);
    return [...seen];
  }, [stats]);
  const countOf = (s: string) => stats?.sources.find((x) => x.source === s)?.conversations ?? 0;

  return (
    <div className="fs-gal fs-lib fs-his" data-testid="history-library">
      {!enabled && (
        <p className="fs-notice" data-tone="warning">
          {t('History import is switched off in Settings → Agent & automation; what was imported before is still here.')}
        </p>
      )}
      <div className="fs-gal__toolbar">
        <p className="fs-gal__stats" style={{ margin: 0 }}>
          {stats ? `${tn(stats.conversations, '{n} conversation', '{n} conversations')} · ${tn(stats.messages, '{n} message', '{n} messages')}${stats.oldest || stats.newest ? ` · ${dateLabel(stats.oldest, t('date unknown'))} – ${dateLabel(stats.newest, t('date unknown'))}` : ''}` : ''}
        </p>
        <span className="fs-gal__spacer" />
        <label className="fs-switch" title={t('Search every message instead of the titles')}>
          <input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} data-testid="history-deep" />
          <span>{t('Inside messages')}</span>
        </label>
        <Button variant="primary" size="sm" icon={FileUp} label={t('Import an export')} onClick={() => setImportOpen(true)} disabled={!enabled} testId="history-import" />
        <SelectToggle selecting={sel.selecting} onToggle={() => (sel.selecting ? sel.leave() : sel.enter())} testId="history-select" />
      </div>

      <div className="fs-gal__chips fs-lib__kinds" role="group" aria-label={t('Source')}>
        <button type="button" className="fs-chip" data-on={!source || undefined} onClick={() => setSource('')}>
          {t('All')}
        </button>
        {sourcesShown.map((s) => (
          <button key={s} type="button" className="fs-chip" data-on={source === s || undefined} onClick={() => setSource(s)}>
            {sourceLabel(s)} <span className="fs-gal__n">{countOf(s)}</span>
          </button>
        ))}
      </div>

      {sel.selecting && rows && (
        <BulkBar items={rows} selected={sel.selected} onAll={(on) => sel.all(rows, on)} label={t('Selection')}>
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} disabled={!sel.selected.size} onClick={() => setConfirm([...sel.selected])} />
        </BulkBar>
      )}

      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}

      {deep && query.trim() && (
        <section className="fs-his__hits" aria-label={t('Search results')}>
          {search === 'loading' && <Skeleton label={t('Searching the archive')} count={3} height="48px" radius="panel" />}
          {search && search !== 'loading' && (
            <>
              <p className="fs-his__tier">
                <span className="fs-lib__badge" data-tone={search.degraded ? 'warning' : undefined} title={search.degraded ? t('One search lane was unavailable; these are the lexical hits alone.') : t('Which lane produced these hits')}>
                  {search.tier}
                  {search.degraded ? ` · ${t('degraded')}` : ''}
                </span>
                <span className="fs-gal__muted">
                  {tn(search.hits.length, '{n} hit', '{n} hits')} · {tn(search.candidates, '{n} candidate', '{n} candidates')} · {search.elapsedMs.toFixed(0)} ms
                </span>
              </p>
              {search.hits.length === 0 && <p className="fs-gal__muted">{t('No message matches.')}</p>}
              <ul className="fs-lib__list">
                {search.hits.map((h) => (
                  <li key={h.messageId} className="fs-lib__item">
                    <div className="fs-lib__row">
                      <button type="button" className="fs-lib__main" onClick={() => void expand(h.conversationId)} aria-expanded={expanded === h.conversationId}>
                        <Search size={16} aria-hidden="true" className="fs-lib__icon" />
                        <span className="fs-lib__text">
                          <span className="fs-lib__title">
                            {h.title || t('Untitled')}
                            <span className="fs-lib__badge">{sourceLabel(h.source)}</span>
                            <span className="fs-lib__badge">{h.role}</span>
                          </span>
                          <span className="fs-his__snippet">
                            {h.snippet.slice(0, h.matchStart)}
                            <mark>{h.snippet.slice(h.matchStart, h.matchEnd)}</mark>
                            {h.snippet.slice(h.matchEnd)}
                          </span>
                          <span className="fs-lib__meta">{dateLabel(h.ts, t('date unknown'))}</span>
                        </span>
                      </button>
                    </div>
                    {expanded === h.conversationId && <Peek d={detail[h.conversationId]} />}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      {!(deep && query.trim()) && (
        <>
          {!rows && !error && <Skeleton label={t('Loading the archive')} count={4} height="56px" radius="panel" />}
          {rows && !rows.length && <EmptyState icon={Archive} title={stats && stats.conversations > 0 ? t('Nothing matches') : t('Nothing imported yet')} body={stats && stats.conversations > 0 ? t('Try another source or a shorter title.') : t('Bring your past here: a ChatGPT or Claude conversations.json, a folder of LM Studio chats, or one of this app’s own exports. Nothing is written until you have seen the preview.')} primaryAction={enabled && !(stats && stats.conversations > 0) ? { label: t('Import an export'), icon: FileUp, onClick: () => setImportOpen(true) } : undefined} />}
          {rows && rows.length > 0 && (
            <ul className="fs-lib__list">
              {rows.map((row) => {
                const open = expanded === row.id;
                return (
                  <li key={row.id} className="fs-lib__item" data-open={open || undefined} data-selected={sel.selected.has(row.id) || undefined}>
                    <div className="fs-lib__row">
                      {sel.selecting && <input type="checkbox" className="fs-lib__cb" checked={sel.selected.has(row.id)} onChange={() => sel.toggle(row.id)} aria-label={t('Select {name}', { name: row.title })} />}
                      <button type="button" className="fs-lib__main" onClick={() => void expand(row.id)} aria-expanded={open} data-testid="history-row">
                        <HistoryIcon size={16} aria-hidden="true" className="fs-lib__icon" />
                        <span className="fs-lib__text">
                          <span className="fs-lib__title">
                            <Highlight text={row.title || t('Untitled')} needle={query} />
                            <span className="fs-lib__badge" data-source={row.source}>
                              {sourceLabel(row.source)}
                            </span>
                            {row.messageCount > 0 && <span className="fs-lib__badge">{tn(row.messageCount, '{n} msg', '{n} msgs')}</span>}
                          </span>
                          <span className="fs-lib__meta">{[dateLabel(row.startedAt, t('date unknown')), row.model].filter(Boolean).join(' · ')}</span>
                        </span>
                        {open ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                      </button>
                      <Menu align="end" trigger={<IconButton icon={MoreHorizontal} label={t('Conversation actions')} size="sm" />} items={[{ label: t('Delete'), icon: Trash2, variant: 'danger', onSelect: () => setConfirm([row.id]) }]} />
                    </div>
                    {open && <Peek d={detail[row.id]} />}
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}

      {confirm && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setConfirm(null);
          }}
          title={tn(confirm.length, 'Delete {n} imported conversation?', 'Delete {n} imported conversations?')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button variant="danger-solid" size="sm" label={t('Delete')} loading={busy === 'delete'} onClick={() => void remove(confirm)} />
            </>
          }
        >
          <p className="fs-prose">{t('Only the copy here goes; the original export stays where it is.')}</p>
        </Dialog>
      )}

      {importOpen && (
        <ImportDialog
          onClose={() => setImportOpen(false)}
          onDone={(report) => {
            setImportOpen(false);
            say(t('{n} conversations imported ({c} new, {u} updated)', { n: report.conversations, c: report.created, u: report.updated }));
            void load();
          }}
        />
      )}
    </div>
  );
}

function Peek({ d }: { d: ConversationDetail | string | undefined }) {
  return (
    <div className="fs-lib__peek">
      {typeof d === 'string' && <p className="fs-gal__muted">{d}</p>}
      {d === undefined && <p className="fs-gal__muted">{t('Loading…')}</p>}
      {typeof d === 'object' && (
        <>
          <p className="fs-gal__muted">
            {[sourceLabel(d.source), d.model, d.externalId ? `id ${d.externalId}` : '', d.path, d.importedAt ? `${t('imported')} ${dateLabel(d.importedAt, '')}` : ''].filter(Boolean).join(' · ')}
          </p>
          {!d.messages.length && <p className="fs-gal__muted">{t('No messages were readable in this conversation.')}</p>}
          <div className="fs-his__messages">
            {d.messages.map((m) => (
              <p key={m.id || m.ordinal} className="fs-lib__msg" data-role={m.role}>
                <strong>{m.role === 'user' ? t('You') : m.role === 'assistant' ? t('Assistant') : m.role}</strong> {m.content}
              </p>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function ImportDialog({ onClose, onDone }: { onClose: () => void; onDone: (r: ImportReport) => void }) {
  const [path, setPath] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState('');
  const [report, setReport] = useState<ImportReport | null>(null);
  const [busy, setBusy] = useState<'preview' | 'import' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const run = async (dryRun: boolean) => {
    setBusy(dryRun ? 'preview' : 'import');
    setError(null);
    try {
      const out = file ? await importUpload(file, source, dryRun) : await importPath(path.trim(), source, dryRun);
      if (dryRun) setReport(out);
      else onDone(out);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const ready = Boolean(file) || Boolean(path.trim());

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={t('Import an export')}
      description={t('A ChatGPT or Claude conversations.json, a folder of LM Studio chats, or one of this app’s own JSON exports. Nothing is written until you have seen the preview.')}
      testId="history-import-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onClose} />
          {report?.dryRun && report.conversations > 0 ? (
            <Button variant="primary" size="sm" icon={FileUp} label={t('Import them')} loading={busy === 'import'} onClick={() => void run(false)} testId="history-import-commit" />
          ) : (
            <Button variant="primary" size="sm" label={t('Preview')} disabled={!ready} loading={busy === 'preview'} onClick={() => void run(true)} testId="history-import-preview" />
          )}
        </>
      }
    >
      <div className="fs-his__form">
        <label>
          <span className="fs-his__label">{t('Path on this machine')}</span>
          <input
            className="fs-field"
            value={path}
            onChange={(e) => {
              setPath(e.target.value);
              setReport(null);
            }}
            placeholder="C:\Users\you\Downloads\chatgpt-export"
            disabled={Boolean(file) || busy !== null}
            data-testid="history-import-path"
          />
        </label>
        <div className="fs-his__or">
          <span className="fs-his__label">{t('…or upload a file')}</span>
          <div className="fs-inline">
            <Button variant="secondary" size="sm" icon={FileUp} label={file ? file.name : t('Choose a file')} onClick={() => fileRef.current?.click()} disabled={busy !== null} />
            {file && <Button variant="ghost" size="sm" label={t('Remove')} onClick={() => setFile(null)} />}
            <input
              ref={fileRef}
              type="file"
              accept=".json,application/json"
              hidden
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                setReport(null);
              }}
            />
          </div>
        </div>
        <label>
          <span className="fs-his__label">{t('Format')}</span>
          <select
            className="fs-field"
            value={source}
            onChange={(e) => {
              setSource(e.target.value);
              setReport(null);
            }}
            disabled={busy !== null}
          >
            <option value="">{t('Detect automatically')}</option>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {sourceLabel(s)}
              </option>
            ))}
          </select>
        </label>
        {error && (
          <p className="fs-notice" data-tone="danger" role="alert">
            {error}
          </p>
        )}
        {report && (
          <div className="fs-his__report" data-testid="history-import-report">
            <p>
              <b>{report.detected ? sourceLabel(report.detected) : t('Nothing recognised')}</b> · {tn(report.files, '{n} file', '{n} files')} ·{' '}
              {report.dryRun
                ? t('would import {c} conversations and {m} messages — {created} new, {updated} already here', { c: report.conversations, m: report.messages, created: report.created, updated: report.updated })
                : t('imported {c} conversations and {m} messages — {created} new, {updated} already here', { c: report.conversations, m: report.messages, created: report.created, updated: report.updated })}{' '}
              <span className="fs-gal__muted">({report.seconds.toFixed(2)}s)</span>
            </p>
            {report.skipped.length ? (
              <details open>
                <summary>{tn(report.skipped.length, '{n} skipped', '{n} skipped#')}</summary>
                <ul>
                  {report.skipped.map((s, i) => (
                    <li key={i}>
                      <span className="fs-his__where">{s.where}</span> <span className="fs-gal__muted">{s.why}</span>
                    </li>
                  ))}
                </ul>
              </details>
            ) : (
              <p className="fs-gal__muted">{t('Nothing was skipped.')}</p>
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}
