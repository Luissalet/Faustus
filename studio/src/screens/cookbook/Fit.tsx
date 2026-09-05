import { ChevronDown, ChevronUp, Cpu, Download as DownloadIcon, Play, RefreshCw, Search, SlidersHorizontal } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, IconButton, Popover, Skeleton } from '../../components';
import { hwImageModels, hwModels, serveCtx, useCookbookState, type FitModel, type HwSystem, type ImageFitModel, type Server } from '../../adapters/cookbook';
import { detectBackend, BACKEND_LABEL, type Backend } from '../../lib/cookbook/serve';
import { t, tn } from '../../i18n';
import { backendForDownload, ggufInclude, ggufSource, startDownload, targetFor } from './actions';
import { Field, FIT_LABEL, Switch } from './parts';

/**
 * Fit ("What fits?"): the hardware the selected server actually has, and
 * the catalogue ranked against it — perfect / good / marginal / tight —
 * with the engine each row would run on. A row expands to Download (with
 * the GGUF source when llama.cpp) or Pull (Ollama). A manual hardware
 * override answers "what if I had…".
 */

type UseCase = '' | 'general' | 'multimodal' | 'image_gen';
type SortKey = 'newest' | 'fit' | 'score' | 'vram' | 'speed' | 'params' | 'context';
const CTX_PRESETS: [string, string][] = [['', t('model max')], ['8192', '8k'], ['16384', '16k'], ['32768', '32k'], ['50000', '50k'], ['131072', '128k']];

const MANUAL_KEY = 'fs-cookbook-manual-hw';

interface Manual {
  on: boolean;
  gpuCount: string;
  vramGb: string;
  ramGb: string;
  backend: string;
}

export function Fit({ server, hwBackend, onSystem, say, onCached }: { server: Server | null; hwBackend: string; onSystem: (s: HwSystem) => void; say: (m: string) => void; onCached: (repo: string) => void }) {
  const state = useCookbookState();
  const [system, setSystem] = useState<HwSystem | null>(null);
  const [models, setModels] = useState<FitModel[] | null>(null);
  const [images, setImages] = useState<ImageFitModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [useCase, setUseCase] = useState<UseCase>('');
  const [engine, setEngine] = useState<Backend | ''>('');
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<SortKey>('newest');
  const [asc, setAsc] = useState(false);
  const [fitOnly, setFitOnly] = useState(false);
  const [ctx, setCtx] = useState('');
  const [gpuCount, setGpuCount] = useState('');
  const [gpuGroup, setGpuGroup] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const [manual, setManual] = useState<Manual>(() => {
    try {
      return { on: false, gpuCount: '1', vramGb: '24', ramGb: '64', backend: 'cuda', ...(JSON.parse(localStorage.getItem(MANUAL_KEY) || '{}') as Partial<Manual>) };
    } catch {
      return { on: false, gpuCount: '1', vramGb: '24', ramGb: '64', backend: 'cuda' };
    }
  });
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(MANUAL_KEY, JSON.stringify(manual));
    } catch {
      /* private mode */
    }
  }, [manual]);

  const load = useCallback(
    async (fresh = false, signal?: AbortSignal) => {
      setModels(null);
      setError(null);
      try {
        const q = { server, useCase: useCase === 'image_gen' ? '' : useCase, search, sort: 'newest', limit: 120, ctx, gpuCount, gpuGroup, fresh, fitOnly, manual: manual.on ? { mode: 'manual', gpuCount: manual.gpuCount, vramGb: manual.vramGb, ramGb: manual.ramGb, backend: manual.backend, ignoreGpu: true, ignoreRam: true } : null };
        if (useCase === 'image_gen') {
          const out = await hwImageModels(server, signal);
          if (signal?.aborted) return;
          setSystem(out.system);
          onSystem(out.system);
          setImages(out.models);
          setModels([]);
        } else {
          const out = await hwModels(q, signal);
          if (signal?.aborted) return;
          setSystem(out.system);
          onSystem(out.system);
          setModels(out.models);
          setImages(null);
          if (out.error) setError(out.error);
        }
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      }
    },
    [server, useCase, search, ctx, gpuCount, gpuGroup, fitOnly, manual, onSystem],
  );

  useEffect(() => {
    const ac = new AbortController();
    const timer = window.setTimeout(() => void load(false, ac.signal), search ? 350 : 0);
    return () => {
      ac.abort();
      window.clearTimeout(timer);
    };
  }, [load, search]);

  const sctx = useMemo(() => serveCtx(state.env, hwBackend, server), [state.env, hwBackend, server]);
  const rows = useMemo(() => {
    const list = (models ?? []).filter((m) => !engine || detectBackend(m, sctx).backend === engine);
    const rank: Record<string, number> = { perfect: 4, good: 3, marginal: 2, too_tight: 1, no_fit: 0 };
    const dir = asc ? 1 : -1;
    list.sort((a, b) => {
      if (sort === 'fit') return ((rank[a.fit_level] ?? -1) - (rank[b.fit_level] ?? -1)) * dir || (a.score - b.score) * dir;
      if (sort === 'newest') {
        if (!a.release_date && !b.release_date) return 0;
        if (!a.release_date) return 1;
        if (!b.release_date) return -1;
        return a.release_date < b.release_date ? -dir : dir;
      }
      const field: Record<string, keyof FitModel> = { score: 'score', vram: 'required_gb', speed: 'speed_tps', params: 'params_b', context: 'context' };
      return ((Number(a[field[sort]]) || 0) - (Number(b[field[sort]]) || 0)) * dir;
    });
    return list;
  }, [models, engine, sort, asc, sctx]);

  const download = async (m: FitModel) => {
    const backend = detectBackend(m, sctx).backend;
    const kind = backendForDownload(backend);
    const src = kind === 'llamacpp' ? ggufSource(m) : null;
    if (kind === 'llamacpp' && !src) {
      say(t('No GGUF source is configured for {name}. Pick a row with a GGUF source, or paste the GGUF repo in Download.', { name: m.name }));
      return;
    }
    const repo = kind === 'ollama' ? String(m.ollama || m.name) : src?.repo || String(m.quant_repo || m.name);
    setBusy(m.name);
    try {
      const target = targetFor(state.env, server);
      const out = await startDownload({ repo, backend: kind, include: kind === 'llamacpp' ? ggufInclude(m, src) : undefined, requiredGb: m.required_gb, target, displayName: m.name });
      if ('duplicate' in out) say(t('{name} is already {state}', { name: m.name, state: out.duplicate.status === 'queued' ? t('queued') : t('downloading') }));
      else say(out.queued ? t('Queued {name}', { name: out.task.name }) : t('Downloading {name}…', { name: out.task.name }));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const isCached = (name: string) => state.tasks.some((x) => x.type === 'download' && x.status === 'done' && (x.payload?.repo_id === name || x.name === name.split('/').pop()));
  const groups = system?.gpu_groups ?? [];

  return (
    <div className="fs-ck__fit" data-testid="cookbook-fit">
      <section className="fs-ck__hw">
        {!system && <Skeleton label={t('Detecting the hardware')} height="64px" radius="panel" />}
        {system && (
          <>
            <div className="fs-ck__hw-main">
              <Cpu size={16} aria-hidden="true" />
              <div>
                <strong>{manual.on ? t('Imagined machine') : system.gpu_name || t('No GPU detected')}</strong>
                <p className="fs-muted">
                  {manual.on
                    ? `${manual.gpuCount} × ${manual.vramGb} GB ${manual.backend} · ${manual.ramGb} GB RAM`
                    : `${groups.map((g) => `${g.count} × ${g.name.replace(/NVIDIA GeForce |AMD /g, '')} ${g.vram_each} GB`).join(' + ') || t('CPU only')} · ${system.backend} · ${system.total_ram_gb} GB RAM (${system.available_ram_gb} free) · ${system.cpu_name} (${tn(system.cpu_cores, '{n} core', '{n} cores')}) · ${system.platform}`}
                </p>
                {system.gpu_error && <p className="fs-notice" data-tone="warning">{system.gpu_error}</p>}
              </div>
              <span className="fs-spacer" />
              {groups.length > 1 && (
                <select className="fs-field" value={gpuGroup} onChange={(e) => setGpuGroup(e.target.value)} aria-label={t('GPU pool')}>
                  <option value="">{t('Biggest pool')}</option>
                  {groups.map((g, i) => (
                    <option key={i} value={String(i)}>
                      {g.count} × {g.name}
                    </option>
                  ))}
                </select>
              )}
              {system.gpu_count > 1 && (
                <div className="fs-seg" role="radiogroup" aria-label={t('GPUs to use')}>
                  {['', ...Array.from({ length: system.gpu_count }, (_, i) => String(i + 1))].map((n) => (
                    <button key={n} type="button" role="radio" aria-checked={gpuCount === n} onClick={() => setGpuCount(n)}>
                      {n === '' ? t('all') : `${n} GPU`}
                    </button>
                  ))}
                </div>
              )}
              <Popover trigger={<Button variant={manual.on ? 'secondary' : 'ghost'} size="sm" icon={SlidersHorizontal} label={manual.on ? t('What if: on') : t('What if…')} />} align="end">
                <div className="fs-ck__manual">
                  <Switch label={t('Rank against an imagined machine')} checked={manual.on} onChange={(v) => setManual((m) => ({ ...m, on: v }))} />
                  <Field label={t('GPUs')}>
                    <input className="fs-field" value={manual.gpuCount} onChange={(e) => setManual((m) => ({ ...m, gpuCount: e.target.value }))} inputMode="numeric" />
                  </Field>
                  <Field label={t('VRAM each (GB)')}>
                    <input className="fs-field" value={manual.vramGb} onChange={(e) => setManual((m) => ({ ...m, vramGb: e.target.value }))} inputMode="numeric" />
                  </Field>
                  <Field label={t('RAM (GB)')}>
                    <input className="fs-field" value={manual.ramGb} onChange={(e) => setManual((m) => ({ ...m, ramGb: e.target.value }))} inputMode="numeric" />
                  </Field>
                  <Field label={t('Backend')}>
                    <select className="fs-field" value={manual.backend} onChange={(e) => setManual((m) => ({ ...m, backend: e.target.value }))}>
                      {['cuda', 'rocm', 'metal', 'vulkan', 'cpu'].map((b) => (
                        <option key={b} value={b}>
                          {b}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
              </Popover>
              <IconButton icon={RefreshCw} label={t('Rescan hardware')} onClick={() => void load(true)} />
            </div>
          </>
        )}
      </section>

      <div className="fs-ck__toolbar">
        <div className="fs-seg" role="radiogroup" aria-label={t('Use case')}>
          {(
            [
              ['', t('Any')],
              ['general', t('Text')],
              ['multimodal', t('Vision')],
              ['image_gen', t('Image generation')],
            ] as [UseCase, string][]
          ).map(([v, l]) => (
            <button key={v} type="button" role="radio" aria-checked={useCase === v} onClick={() => setUseCase(v)}>
              {l}
            </button>
          ))}
        </div>
        {useCase !== 'image_gen' && (
          <select className="fs-field" value={engine} onChange={(e) => setEngine(e.target.value as Backend | '')} aria-label={t('Engine')}>
            <option value="">{t('Any engine')}</option>
            {(['vllm', 'sglang', 'llamacpp', 'ollama', 'mlx'] as Backend[]).map((b) => (
              <option key={b} value={b}>
                {BACKEND_LABEL[b]}
              </option>
            ))}
          </select>
        )}
        <label className="fs-search">
          <Search size={14} aria-hidden="true" />
          <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t('Search the catalogue')} aria-label={t('Search')} data-testid="fit-search" />
        </label>
        {useCase !== 'image_gen' && (
          <>
            <div className="fs-inline">
              <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as SortKey)} aria-label={t('Sort')}>
                <option value="newest">{t('Newest')}</option>
                <option value="fit">{t('Best fit')}</option>
                <option value="score">{t('Score')}</option>
                <option value="vram">{t('VRAM')}</option>
                <option value="speed">{t('Speed')}</option>
                <option value="params">{t('Parameters')}</option>
                <option value="context">{t('Context')}</option>
              </select>
              <IconButton icon={asc ? ChevronUp : ChevronDown} size="sm" label={asc ? t('Ascending') : t('Descending')} onClick={() => setAsc((v) => !v)} />
            </div>
            <div className="fs-ck__ctx" role="group" aria-label={t('Target context')}>
              {CTX_PRESETS.map(([v, l]) => (
                <button key={v} type="button" className="fs-chip" data-on={ctx === v || undefined} onClick={() => setCtx(v)}>
                  {l}
                </button>
              ))}
            </div>
            <Switch label={t('Only what fits')} checked={fitOnly} onChange={setFitOnly} />
          </>
        )}
      </div>

      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!models && <Skeleton label={t('Ranking the catalogue')} count={6} height="40px" radius="panel" />}

      {images && (
        <ul className="fs-ck__list">
          {images.map((m) => (
            <li key={m.id} className="fs-ck__item">
              <div className="fs-ck__item-row">
                <span className="fs-ck__fitdot" data-fit={m.fit} title={m.fit_label} />
                <button type="button" className="fs-ck__item-main" onClick={() => setOpen(open === m.id ? null : m.id)} aria-expanded={open === m.id}>
                  <span className="fs-ck__item-name">{m.name}</span>
                  <span className="fs-ck__item-meta">
                    {m.provider} · {m.params_b}B · {m.quant} · {m.vram_needed} GB · {m.fit_label}
                  </span>
                  {open === m.id ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                </button>
              </div>
              {open === m.id && (
                <div className="fs-ck__item-body">
                  <p className="fs-muted">{m.id}</p>
                  <Button variant="primary" size="sm" icon={DownloadIcon} label={t('Download')} loading={busy === m.id} onClick={() => void download({ ...m, name: m.quant_repo || m.id, required_gb: m.vram_needed, is_image_gen: true } as unknown as FitModel)} />
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {models && !images && !rows.length && <EmptyState icon={Search} title={t('Nothing to show')} body={search || engine || fitOnly ? t('Loosen a filter: the search, the engine or "only what fits".') : t('The catalogue came back empty for this hardware; rescan or try the imagined machine.')} headingLevel={3} />}

      {rows.length > 0 && (
        <div className="fs-ck__table-wrap">
          <table className="fs-ck__table">
            <thead>
              <tr>
                <th>{t('Fit')}</th>
                <th>{t('Model')}</th>
                <th>{t('VRAM')}</th>
                <th>{t('Params')}</th>
                <th>{t('Quant')}</th>
                <th>{t('Ctx')}</th>
                <th>{t('Speed')}</th>
                <th>{t('Score')}</th>
                <th>{t('Engine')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => {
                const eng = detectBackend(m, sctx);
                const isOpen = open === m.name;
                return [
                  <tr key={m.name} className="fs-ck__fitrow" data-open={isOpen || undefined} onClick={() => setOpen(isOpen ? null : m.name)} tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(isOpen ? null : m.name); } }}>
                    <td>
                      <span className="fs-ck__fitdot" data-fit={m.fit_level} /> {t(FIT_LABEL[m.fit_level] ?? m.fit_level)}
                    </td>
                    <td className="fs-ck__td-name">
                      <span className="fs-ck__item-name">{m.name}</span>
                      <span className="fs-muted"> {m.provider}{m.is_moe ? ' · MoE' : ''}{m.release_date ? ` · ${m.release_date}` : ''}</span>
                    </td>
                    <td>{m.required_gb ? `${m.required_gb} GB` : '—'}</td>
                    <td>{m.parameter_count || (m.params_b ? `${m.params_b}B` : '—')}</td>
                    <td>{m.quant}</td>
                    <td>{m.context >= 1024 ? `${Math.round(m.context / 1024)}k` : m.context}</td>
                    <td>{m.speed_tps ? `${m.speed_tps} t/s` : '—'}</td>
                    <td>{m.score}</td>
                    <td>{eng.label}{m.run_mode === 'cpu_offload' ? ` · ${t('offload')}` : ''}</td>
                  </tr>,
                  isOpen ? (
                    <tr key={`${m.name}-x`} className="fs-ck__fitdetail">
                      <td colSpan={9}>
                        <div className="fs-ck__fitdetail-body">
                          <p className="fs-muted">
                            {t('quality {q} · speed {s} · fit {f} · context {c}', { q: m.scores.quality, s: m.scores.speed, f: m.scores.fit, c: m.scores.context })}
                            {m.gguf_sources.length ? ` · GGUF: ${m.gguf_sources.map((g) => `${g.repo}${g.file ? ` (${g.file})` : ''}`).join(', ')}` : ''}
                            {m.context_length ? ` · ${t('max context')} ${m.context_length}` : ''}
                          </p>
                          <div className="fs-inline">
                            {isCached(m.name) ? (
                              <Button variant="primary" size="sm" icon={Play} label={t('Launch')} onClick={() => onCached(m.name)} />
                            ) : (
                              <Button variant="primary" size="sm" icon={DownloadIcon} label={eng.backend === 'ollama' ? t('Pull with Ollama') : eng.backend === 'llamacpp' ? t('Download GGUF') : t('Download')} loading={busy === m.name} onClick={() => void download(m)} />
                            )}
                            <a className="fs-ck__link" href={`https://huggingface.co/${m.name}`} target="_blank" rel="noopener noreferrer">
                              {t('Model card')}
                            </a>
                          </div>
                        </div>
                      </td>
                    </tr>
                  ) : null,
                ];
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
