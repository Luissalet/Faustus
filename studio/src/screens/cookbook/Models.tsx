import { Box, ChevronDown, ChevronUp, Image as ImageIcon, MoreHorizontal, RefreshCw, Search, Star, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton } from '../../components';
import { cachedModels, isLocal, shellExec, updateState, useCookbookState, type CachedModel, type Server } from '../../adapters/cookbook';
import { ggufQuant, bytesLabel, shellQuote, type ServeFields } from '../../lib/cookbook/serve';
import { onHostCmd } from '../../lib/cookbook/tasks';
import { t, tn } from '../../i18n';
import { ServeForm } from './ServeForm';

/**
 * Models: what is already on the selected server (the HF cache, custom
 * model folders, Ollama tags), ready to launch. A row opens its launch
 * form; favourites float; tags split LLM / GGUF / image / Ollama.
 */

type Tag = 'all' | 'llm' | 'gguf' | 'image' | 'ollama';
type Sort = 'name' | 'size-desc' | 'size-asc' | 'recent';

function tagOf(m: CachedModel): Exclude<Tag, 'all'> {
  if (m.is_ollama || m.backend === 'ollama') return 'ollama';
  if (m.is_diffusion || m.is_video) return 'image';
  if (m.is_gguf || m.gguf_files.length || /gguf/i.test(m.repo_id)) return 'gguf';
  return 'llm';
}

const parseSize = (s: string): number => {
  const m = (s || '').match(/([\d.]+)\s*(GB|MB|KB)/i);
  if (!m) return 0;
  const n = parseFloat(m[1]);
  return m[2].toUpperCase() === 'GB' ? n * 1024 : m[2].toUpperCase() === 'MB' ? n : n / 1024;
};

export function Models({ server, hwBackend, say, openRepo, edit, onLaunched, onSchedule, onDownloadTab }: { server: Server | null; hwBackend: string; say: (m: string) => void; openRepo?: string | null; edit?: { repo: string; fields?: ServeFields; replaceTaskId?: string; focus?: string } | null; onLaunched: () => void; onSchedule: (repo: string) => void; onDownloadTab: () => void }) {
  const state = useCookbookState();
  const [models, setModels] = useState<CachedModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [tag, setTag] = useState<Tag>('all');
  const [sort, setSort] = useState<Sort>('name');
  const [open, setOpen] = useState<string | null>(openRepo ?? edit?.repo ?? null);
  const [confirm, setConfirm] = useState<{ model: CachedModel; file?: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setModels(null);
      setError(null);
      try {
        const out = await cachedModels(server, signal);
        if (signal?.aborted) return;
        setModels(out.models);
        if (out.error) setError(out.error);
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      }
    },
    [server],
  );

  useEffect(() => {
    const ac = new AbortController();
    void load(ac.signal);
    return () => ac.abort();
  }, [load]);

  useEffect(() => {
    if (openRepo) setOpen(openRepo);
  }, [openRepo]);
  useEffect(() => {
    if (edit?.repo) setOpen(edit.repo);
  }, [edit]);

  const favourites = new Set(state.serveFavorites);
  const toggleFav = (repo: string) => updateState((s) => ({ ...s, serveFavorites: s.serveFavorites.includes(repo) ? s.serveFavorites.filter((r) => r !== repo) : [...s.serveFavorites, repo] }));

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: 0, llm: 0, gguf: 0, image: 0, ollama: 0 };
    for (const m of models ?? []) {
      if (m.is_adapter && !m.is_diffusion && !m.is_video) continue;
      c.all += 1;
      c[tagOf(m)] += 1;
    }
    return c;
  }, [models]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = (models ?? []).filter((m) => !(m.is_adapter && !m.is_diffusion && !m.is_video)).filter((m) => (tag === 'all' || tagOf(m) === tag) && (!q || m.repo_id.toLowerCase().includes(q)));
    list.sort((a, b) => {
      if (sort === 'name') return a.repo_id.localeCompare(b.repo_id);
      if (sort === 'size-desc') return parseSize(b.size) - parseSize(a.size);
      if (sort === 'size-asc') return parseSize(a.size) - parseSize(b.size);
      return (b.mtime || 0) - (a.mtime || 0);
    });
    list.sort((a, b) => Number(favourites.has(b.repo_id)) - Number(favourites.has(a.repo_id)));
    return list;
  }, [models, query, tag, sort, favourites]);

  const serving = (repo: string) => state.tasks.some((x) => x.type === 'serve' && (x.status === 'running' || x.status === 'ready') && x.payload?.repo_id === repo);
  const downloading = (repo: string) => state.tasks.some((x) => x.type === 'download' && (x.status === 'running' || x.status === 'queued') && x.payload?.repo_id === repo);

  const remove = async () => {
    if (!confirm) return;
    setBusy(true);
    const { model, file } = confirm;
    try {
      const host = server && !isLocal(server) ? server.host : '';
      let cmd: string;
      if (model.is_ollama || model.backend === 'ollama') cmd = `ollama rm ${shellQuote(model.repo_id)}`;
      else if (file) {
        const base = (model.path || '').replace(/\/+$/, '');
        const dir = model.is_local_dir ? `${base}/${model.repo_id}` : `${base || '~/.cache/huggingface/hub'}/models--${model.repo_id.replace(/\//g, '--')}/snapshots`;
        cmd = `find ${shellQuote(dir)} -name ${shellQuote(file.split('/').pop() || file)} -print -delete`;
      } else {
        const base = (model.path || '').replace(/\/+$/, '');
        const dir = model.is_local_dir ? `${base}/${model.repo_id}` : `${base || '~/.cache/huggingface/hub'}/models--${model.repo_id.replace(/\//g, '--')}`;
        cmd = `rm -rf ${shellQuote(dir)}`;
      }
      const r = await shellExec(onHostCmd(host, server?.port, cmd), 60);
      if (r.exit_code !== 0) throw new Error(r.stderr || r.stdout || t('Delete failed'));
      say(file ? t('File deleted') : t('Model deleted'));
      setConfirm(null);
      await load();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-ck__models" data-testid="cookbook-models">
      <div className="fs-ck__toolbar">
        <label className="fs-search">
          <Search size={14} aria-hidden="true" />
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('Search cached models')} aria-label={t('Search')} data-testid="models-search" />
        </label>
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label={t('Sort')}>
          <option value="name">{t('A to Z')}</option>
          <option value="recent">{t('Newest first')}</option>
          <option value="size-desc">{t('Biggest first')}</option>
          <option value="size-asc">{t('Smallest first')}</option>
        </select>
        <span className="fs-spacer" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Rescan')} onClick={() => void load()} />
      </div>
      <div className="fs-gal__chips" role="group" aria-label={t('Kind')}>
        {(['all', 'llm', 'gguf', 'image', 'ollama'] as Tag[]).map((k) => (
          <button key={k} type="button" className="fs-chip" data-on={tag === k || undefined} onClick={() => setTag(k)}>
            {t(k === 'all' ? 'All' : k === 'llm' ? 'LLM' : k === 'gguf' ? 'GGUF' : k === 'image' ? 'Image' : 'Ollama')} <span className="fs-gal__n">{counts[k]}</span>
          </button>
        ))}
      </div>
      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!models && <Skeleton label={t('Scanning the cache')} count={4} height="52px" radius="panel" />}
      {models && !visible.length && <EmptyState icon={Box} title={models.length ? t('Nothing matches') : t('Nothing cached here yet')} body={models.length ? t('Try another kind or a shorter search.') : t('Download a model first; everything under the HF cache, your model folders and Ollama shows up here.')} headingLevel={3} primaryAction={models.length ? undefined : { label: t('Go to Download'), onClick: onDownloadTab }} />}
      {models && visible.length > 0 && (
        <ul className="fs-ck__list">
          {visible.map((m) => {
            const isOpen = open === m.repo_id;
            const kind = tagOf(m);
            const quant = m.gguf_files.length ? [...new Set(m.gguf_files.map((g) => ggufQuant(g.rel_path)))].slice(0, 4).join(' · ') : '';
            return (
              <li key={m.repo_id} className="fs-ck__item" data-open={isOpen || undefined} data-serving={serving(m.repo_id) || undefined}>
                <div className="fs-ck__item-row">
                  <IconButton icon={Star} size="sm" label={favourites.has(m.repo_id) ? t('Unfavourite') : t('Favourite')} data-on={favourites.has(m.repo_id) || undefined} onClick={() => toggleFav(m.repo_id)} />
                  <button type="button" className="fs-ck__item-main" onClick={() => setOpen(isOpen ? null : m.repo_id)} aria-expanded={isOpen} data-testid="model-row">
                    {kind === 'image' ? <ImageIcon size={14} aria-hidden="true" /> : <Box size={14} aria-hidden="true" />}
                    <span className="fs-ck__item-name">{m.repo_id}</span>
                    <span className="fs-ck__tag" data-kind={kind}>
                      {kind === 'llm' ? 'LLM' : kind === 'gguf' ? 'GGUF' : kind === 'image' ? t('Image') : 'Ollama'}
                    </span>
                    {serving(m.repo_id) && <span className="fs-ck__tag" data-kind="serving">{t('serving')}</span>}
                    {downloading(m.repo_id) && <span className="fs-ck__tag" data-kind="downloading">{t('downloading')}</span>}
                    {m.has_incomplete && <span className="fs-ck__tag" data-kind="incomplete">{t('incomplete')}</span>}
                    <span className="fs-ck__item-meta">
                      {m.size}
                      {m.nb_files ? ` · ${tn(m.nb_files, '{n} file', '{n} files')}` : ''}
                      {quant ? ` · ${quant}` : ''}
                    </span>
                    {isOpen ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                  </button>
                  <Menu
                    align="end"
                    trigger={<IconButton icon={MoreHorizontal} label={t('Model actions')} size="sm" />}
                    items={[
                      { label: t('Copy id'), onSelect: () => void navigator.clipboard.writeText(m.repo_id).then(() => say(t('Copied'))) },
                      ...m.gguf_files.filter((g) => /\.gguf$/i.test(g.rel_path)).map((g) => ({ label: t('Delete {file}', { file: `${ggufQuant(g.rel_path)}${g.size_bytes ? ` (${bytesLabel(g.size_bytes)})` : ''}` }), icon: Trash2, variant: 'danger' as const, onSelect: () => setConfirm({ model: m, file: g.rel_path }) })),
                      { label: t('Delete from disk'), icon: Trash2, variant: 'danger', onSelect: () => setConfirm({ model: m }) },
                    ]}
                  />
                </div>
                {isOpen && <ServeForm key={`${m.repo_id}-${edit?.replaceTaskId ?? ''}`} model={m} server={server} hwBackend={hwBackend} initial={edit?.repo === m.repo_id ? edit.fields : undefined} replaceTaskId={edit?.repo === m.repo_id ? edit.replaceTaskId : undefined} focus={edit?.repo === m.repo_id ? edit.focus : undefined} say={say} onLaunched={onLaunched} onSchedule={onSchedule} />}
              </li>
            );
          })}
        </ul>
      )}

      {confirm && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setConfirm(null);
          }}
          title={confirm.file ? t('Delete {file}?', { file: confirm.file.split('/').pop() || confirm.file }) : t('Delete {name} from disk?', { name: confirm.model.repo_id })}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button variant="danger-solid" size="sm" label={t('Delete')} loading={busy} onClick={() => void remove()} />
            </>
          }
        >
          <p className="fs-prose">{confirm.file ? t('Only that file goes; the rest of the repo stays cached.') : t('Frees {size} on {where}. Downloading it again takes as long as it did the first time.', { size: confirm.model.size, where: server && !isLocal(server) ? server.host : t('this machine') })}</p>
        </Dialog>
      )}
    </div>
  );
}
