import { Archive, ArchiveRestore, ChevronDown, ChevronUp, Download, ExternalLink, MessageSquarePlus, MoreHorizontal, Search, Sparkles, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton } from '../../components';
import { relativeTime } from '../../adapters/home';
import { deleteResearch, discussResearch, exportFormats, exportUrl, loadResearchLibrary, reportUrl, researchDetail, setResearchArchived, type ResearchDetail, type ResearchItem, type ResearchSort } from '../../adapters/research';
import { t, tn } from '../../i18n';
import { safeExternal } from '../../lib/markdown';
import { BulkBar, Highlight, SelectToggle, useSelection } from './parts';

/**
 * Finished Deep Research reports: what was asked, how many sources, how
 * long it took; open the report, export it, discuss it in a new chat,
 * archive or delete. The peek shows the summary and the sources.
 */
export function ResearchLibrary({ query, say, archived = false }: { query: string; say: (m: string) => void; archived?: boolean }) {
  const navigate = useNavigate();
  const [items, setItems] = useState<ResearchItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<ResearchSort>('recent');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, ResearchDetail | string>>({});
  const [formats, setFormats] = useState<string[]>(['md']);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string[] | null>(null);
  const sel = useSelection<ResearchItem>();

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const out = await loadResearchLibrary({ search: query, sort, archived, limit: 100 }, signal);
        if (signal?.aborted) return;
        setItems(out.items);
        setError(null);
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      }
    },
    [query, sort, archived],
  );

  useEffect(() => {
    const ac = new AbortController();
    setItems(null);
    void load(ac.signal);
    return () => ac.abort();
  }, [load]);

  useEffect(() => {
    void exportFormats().then(setFormats);
  }, []);

  const act = async (what: string, work: () => Promise<void>, done?: string) => {
    setBusy(what);
    try {
      await work();
      if (done) say(done);
      await load();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const expand = async (item: ResearchItem) => {
    if (expanded === item.id) {
      setExpanded(null);
      return;
    }
    setExpanded(item.id);
    if (!(item.id in detail)) {
      try {
        const d = await researchDetail(item.id);
        setDetail((cur) => ({ ...cur, [item.id]: d }));
      } catch (e) {
        setDetail((cur) => ({ ...cur, [item.id]: (e as Error).message }));
      }
    }
  };

  const discuss = async (item: ResearchItem) => {
    await act('discuss', async () => {
      const out = await discussResearch(item.id);
      navigate(`/studio?s=${encodeURIComponent(out.sessionId)}`);
    });
  };

  const failed = useMemo(() => (items ?? []).filter((i) => i.status && i.status !== 'done' && i.status !== 'completed'), [items]);

  return (
    <div className="fs-gal fs-lib" data-testid="research-library">
      <div className="fs-gal__toolbar">
        <p className="fs-gal__stats" style={{ margin: 0 }}>
          {items ? tn(items.length, '{n} report', '{n} reports') : ''}
        </p>
        <span className="fs-gal__spacer" />
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as ResearchSort)} aria-label={t('Sort')}>
          <option value="recent">{t('Newest first')}</option>
          <option value="oldest">{t('Oldest first')}</option>
          <option value="most-sources">{t('Most sources')}</option>
          <option value="alpha">{t('A to Z')}</option>
        </select>
        {!archived && failed.length > 0 && <Button variant="ghost" size="sm" icon={Sparkles} label={t('Clean failed ({n})', { n: failed.length })} loading={busy === 'clean'} onClick={() => void act('clean', async () => { for (const f of failed) await deleteResearch(f.id); }, tn(failed.length, '{n} failed report removed', '{n} failed reports removed'))} />}
        <SelectToggle selecting={sel.selecting} onToggle={() => (sel.selecting ? sel.leave() : sel.enter())} testId="research-select" />
      </div>

      {sel.selecting && items && (
        <BulkBar items={items} selected={sel.selected} onAll={(on) => sel.all(items, on)} label={t('Selection')}>
          {archived ? (
            <Button variant="ghost" size="sm" icon={ArchiveRestore} label={t('Restore')} disabled={!sel.selected.size} onClick={() => void act('archive', async () => { for (const id of sel.selected) await setResearchArchived(id, false); sel.leave(); }, t('Restored'))} />
          ) : (
            <Button variant="ghost" size="sm" icon={Archive} label={t('Archive')} disabled={!sel.selected.size} onClick={() => void act('archive', async () => { for (const id of sel.selected) await setResearchArchived(id, true); sel.leave(); }, t('Archived'))} />
          )}
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} disabled={!sel.selected.size} onClick={() => setConfirm([...sel.selected])} />
        </BulkBar>
      )}

      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!items && !error && <Skeleton label={t('Loading reports')} count={3} height="64px" radius="panel" />}
      {items && !items.length && <EmptyState icon={Search} title={archived ? t('No archived reports') : t('No research yet')} body={archived ? t('Archived reports wait here until you restore or delete them.') : t('Run a Deep Research from Studio; the finished reports collect here with their sources.')} />}

      {items && items.length > 0 && (
        <ul className="fs-lib__list">
          {items.map((item) => {
            const open = expanded === item.id;
            const d = detail[item.id];
            return (
              <li key={item.id} className="fs-lib__item" data-open={open || undefined} data-selected={sel.selected.has(item.id) || undefined}>
                <div className="fs-lib__row">
                  {sel.selecting && <input type="checkbox" className="fs-lib__cb" checked={sel.selected.has(item.id)} onChange={() => sel.toggle(item.id)} aria-label={t('Select {name}', { name: item.query })} />}
                  <button type="button" className="fs-lib__main" onClick={() => void expand(item)} aria-expanded={open}>
                    {item.thumbnail ? <img className="fs-lib__thumb" src={item.thumbnail} alt="" /> : <Search size={16} aria-hidden="true" className="fs-lib__icon" />}
                    <span className="fs-lib__text">
                      <span className="fs-lib__title">
                        <Highlight text={item.query || t('Untitled research')} needle={query} />
                        {item.status && item.status !== 'done' && item.status !== 'completed' && <span className="fs-lib__badge" data-tone="warning">{item.status}</span>}
                      </span>
                      <span className="fs-lib__meta">{[item.category, tn(item.sourceCount, '{n} source', '{n} sources'), item.duration, item.rounds ? t('{n} rounds', { n: item.rounds }) : '', relativeTime(item.completedAt || item.startedAt)].filter(Boolean).join(' · ')}</span>
                    </span>
                    {open ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                  </button>
                  <Button size="sm" variant="ghost" icon={ExternalLink} label={t('Report')} title={t('Open the report in a new tab')} onClick={() => window.open(reportUrl(item.id), '_blank', 'noopener')} />
                  <Menu
                    align="end"
                    trigger={<IconButton icon={MoreHorizontal} label={t('Report actions')} size="sm" />}
                    items={[
                      { label: t('Discuss in a new chat'), icon: MessageSquarePlus, onSelect: () => void discuss(item) },
                      ...formats.map((f) => ({ label: t('Export as {format}', { format: f.toUpperCase() }), icon: Download, onSelect: () => window.open(exportUrl(item.id, f), '_blank', 'noopener') })),
                      null,
                      archived ? { label: t('Restore'), icon: ArchiveRestore, onSelect: () => void act('archive', () => setResearchArchived(item.id, false), t('Restored')) } : { label: t('Archive'), icon: Archive, onSelect: () => void act('archive', () => setResearchArchived(item.id, true), t('Archived')) },
                      { label: t('Delete'), icon: Trash2, variant: 'danger', onSelect: () => setConfirm([item.id]) },
                    ]}
                  />
                </div>
                {open && (
                  <div className="fs-lib__peek">
                    {typeof d === 'string' && <p className="fs-gal__muted">{d}</p>}
                    {d === undefined && <p className="fs-gal__muted">{t('Loading…')}</p>}
                    {typeof d === 'object' && (
                      <>
                        <p className="fs-lib__summary">{d.summary || d.report.slice(0, 1200) || t('No summary')}</p>
                        {d.sources.length > 0 && (
                          <ol className="fs-lib__sources">
                            {d.sources.slice(0, 20).map((s, i) => (
                              <li key={i}>{safeExternal(s.url) ? <a href={safeExternal(s.url) as string} target="_blank" rel="noopener noreferrer">{s.title || s.url}</a> : s.title}</li>
                            ))}
                          </ol>
                        )}
                        <div className="fs-gal__row">
                          <Button size="sm" icon={MessageSquarePlus} label={busy === 'discuss' ? t('Creating…') : t('Discuss in a new chat')} loading={busy === 'discuss'} onClick={() => void discuss(item)} />
                        </div>
                      </>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <Dialog
        open={!!confirm}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={confirm && confirm.length > 1 ? t('Delete {n} reports?', { n: confirm.length }) : t('Delete this report?')}
        description={t('The report and its sources are removed. Chats that discussed it keep their copy.')}
        testId="research-delete"
        footer={
          <>
            <Button variant="ghost" label={t('Cancel')} onClick={() => setConfirm(null)} />
            <Button
              variant="danger-solid"
              label={t('Delete')}
              onClick={() => {
                const ids = confirm ?? [];
                setConfirm(null);
                void act('delete', async () => { for (const id of ids) await deleteResearch(id); sel.leave(); }, tn(ids.length, '{n} report deleted', '{n} reports deleted'));
              }}
            />
          </>
        }
      />
    </div>
  );
}
