import { Download, HardDrive, RefreshCw, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, EmptyState, IconButton, Skeleton } from '../../components';
import { invalidateSettings } from '../../adapters/settings';
import {
  cancelPull,
  deleteModel,
  discoverModels,
  fitState,
  fmtCtx,
  fmtGb,
  loadLocalModels,
  loadModel,
  pinWarning,
  pullEvents,
  releaseOrphanRunner,
  saveModelOptions,
  setDefaultModel,
  setPlacement,
  shortGpuName,
  startPull,
  unloadModel,
  untilText,
  VALID_NAME,
  type Caps,
  type DiscoverEntry,
  type Fit,
  type GpuCard,
  type InstalledModel,
  type LoadedModel,
  type LocalModelsData,
  type Pull,
  type Vram,
} from '../../adapters/localModels';
import { locale, t, tn } from '../../i18n';
import { Select } from './fields';

const POLL_MS = 8000;

/**
 * Local models: what is installed on the Ollama server, what is resident in
 * VRAM right now, whether each model fits the card(s), pulls with live
 * progress (a pull is a server job — closing the page does not stop it, and
 * an open page re-attaches to whatever is still running), load/unload,
 * delete, per-model options, the placement policy, and the catalogue.
 * Everyone can look; the buttons that change something are for admins.
 */
export function LocalModelsSection({ admin, say }: { admin: boolean; say: (t: string) => void }) {
  const [data, setData] = useState<LocalModelsData | null>(null);
  const [endpointId, setEndpointId] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pulls, setPulls] = useState<Map<string, Pull>>(new Map());
  const sources = useRef<Map<string, EventSource>>(new Map());
  const dismissed = useRef<Set<string>>(new Set());
  const [optionsFor, setOptionsFor] = useState('');

  const refresh = useCallback(
    async (silent = false) => {
      try {
        const d = await loadLocalModels(endpointId || undefined);
        setData(d);
        setError(null);
        if (d.endpoint_id && d.endpoint_id !== endpointId) setEndpointId(d.endpoint_id);
        setPulls((cur) => {
          const next = new Map(cur);
          for (const p of d.pulls ?? []) {
            if (dismissed.current.has(p.id)) continue;
            next.set(p.id, p);
            if (p.active) attach(p.id);
          }
          return next;
        });
      } catch (e) {
        if (!silent) setError((e as Error).message);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [endpointId],
  );

  const afterChange = useCallback(() => {
    invalidateSettings();
    void refresh(true);
  }, [refresh]);

  const attach = useCallback(
    (id: string) => {
      if (sources.current.has(id) || typeof EventSource === 'undefined') return;
      let es: EventSource;
      try {
        es = pullEvents(id);
      } catch {
        return;
      }
      sources.current.set(id, es);
      const done = () => {
        try {
          es.close();
        } catch {
          /* closed */
        }
        sources.current.delete(id);
        setPulls((cur) => {
          const snap = cur.get(id);
          if (snap?.status === 'done') {
            say(t('Pulled {name}', { name: snap.name }));
            afterChange();
          } else if (snap?.status === 'error') say(t('Pull failed: {why}', { why: snap.error ?? snap.name }));
          return cur;
        });
      };
      es.onmessage = (ev) => {
        if (!ev.data || ev.data === '{}') return;
        try {
          const snap = JSON.parse(ev.data) as Pull;
          if (snap?.id) setPulls((cur) => new Map(cur).set(snap.id, snap));
        } catch {
          /* ignore */
        }
      };
      es.addEventListener('end', done);
      es.onerror = () => {
        setPulls((cur) => {
          const snap = cur.get(id);
          if (snap && !snap.active) done();
          return cur;
        });
      };
    },
    [afterChange, say],
  );

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => {
      if (!document.hidden) void refresh(true);
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);
  useEffect(() => {
    const map = sources.current;
    return () => {
      for (const es of map.values()) es.close();
      map.clear();
    };
  }, []);

  const pull = async (name: string) => {
    const clean = name.trim();
    if (!clean) return;
    if (!VALID_NAME.test(clean)) return say(t('That does not look like an Ollama model name (letters, digits, . _ - / :).'));
    try {
      const out = await startPull(endpointId, clean);
      if (out.pull?.id) {
        dismissed.current.delete(out.pull.id);
        setPulls((cur) => new Map(cur).set(out.pull!.id, out.pull!));
        attach(out.pull.id);
        say(out.created === false ? t('Already pulling {name}', { name: clean }) : t('Pulling {name}…', { name: clean }));
      }
    } catch (e) {
      say(t('Pull failed: {why}', { why: (e as Error).message }));
    }
  };

  const act = async (fn: () => Promise<unknown>, okMsg: string) => {
    try {
      await fn();
      say(okMsg);
      afterChange();
    } catch (e) {
      say((e as Error).message);
    }
  };

  if (error && !data) return <EmptyState icon={HardDrive} title={t('Could not read the local models.')} body={error} />;
  const ep = data?.endpoints.find((e) => e.id === (data.endpoint_id || endpointId));
  const cards = (data?.vram.gpus ?? []) as GpuCard[];

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-lm">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-lm" className="fs-set__title">{t('Local models')}</h2>
          <p className="fs-prose">{t('What the Ollama server has installed, what sits in VRAM right now, and whether each model fits the card before you load it.')}</p>
        </div>
        <div className="fs-set__row-actions">
          {data && data.endpoints.length > 1 && (
            <Select id="lm-ep" value={data.endpoint_id} options={data.endpoints.map((e) => ({ value: e.id, label: `${e.name} — ${e.same_machine ? t('this machine') : t('remote')}` }))} onChange={(v) => setEndpointId(v)} />
          )}
          <IconButton icon={RefreshCw} label={t('Re-read installed and loaded models')} size="sm" onClick={() => void refresh()} />
        </div>
      </header>
      {!admin && <p className="fs-set__help">{t('Read-only: pulling, loading and deleting models is for administrators.')}</p>}
      {data === null ? (
        <Skeleton label={t('Loading')} count={4} height="56px" />
      ) : (
        <>
          {data.error && <p className="fs-notice" data-tone="danger">{data.error}</p>}
          <VramCard vram={data.vram} loaded={data.loaded} policy={data.placement_policy} admin={admin} onPlacement={(prefer) => void act(() => setPlacement(prefer), prefer < 0 ? t('Placement: auto') : t('Placement: fill GPU {n} first', { n: prefer }))} onRelease={(pid) => void act(() => releaseOrphanRunner(pid), t('Runner released.'))} />
          {data.disk?.free_bytes != null && <p className="fs-set__help">{t('{free} free of {total} where Ollama keeps its blobs ({path}).', { free: fmtGb(data.disk.free_bytes), total: fmtGb(data.disk.total_bytes), path: data.disk.path ?? '' })}</p>}

          <div className="fs-set__card">
            <h3 className="fs-set__card-title">{t('Loaded now')}</h3>
            <LoadedList loaded={data.loaded} cards={cards} admin={admin} onUnload={(m) => void act(() => unloadModel(data.endpoint_id, m.name, false), t('Unloaded {name}', { name: m.name }))} />
          </div>

          <div className="fs-set__card">
            <h3 className="fs-set__card-title">
              {t('Installed')} <span className="fs-set__help">{tn(data.models.length, '{n} model', '{n} models')}{ep ? ` · ${ep.name}` : ''}</span>
            </h3>
            <InstalledTable
              models={data.models}
              cards={cards}
              admin={admin}
              optionsFor={optionsFor}
              setOptionsFor={setOptionsFor}
              onLoad={(m) => void act(() => loadModel(data.endpoint_id, m.name, !!m.capabilities?.embedding), t('Loading {name}…', { name: m.name }))}
              onUnload={(m) => void act(() => unloadModel(data.endpoint_id, m.name, !!m.capabilities?.embedding), t('Unloaded {name}', { name: m.name }))}
              onDefault={(m) => void act(() => setDefaultModel(data.endpoint_id, m.name), t('{name} is now the default chat model.', { name: m.name }))}
              onDelete={(m) => {
                if (!window.confirm(t('Delete {name} from this Ollama? The files are removed from disk; pull it again to get it back.', { name: m.name }))) return;
                void act(() => deleteModel(data.endpoint_id, m.name), t('Deleted {name}', { name: m.name }));
              }}
              onSaveOptions={async (m, opts) => {
                try {
                  const saved = await saveModelOptions(data.endpoint_id, m.name, opts);
                  say(Object.keys(saved).length ? t('Saved options for {name}', { name: m.name }) : t('Cleared options for {name}', { name: m.name }));
                  setOptionsFor('');
                  afterChange();
                } catch (e) {
                  say((e as Error).message);
                }
              }}
            />
          </div>

          <div className="fs-set__card">
            <h3 className="fs-set__card-title">{t('Pull a model')}</h3>
            <PullForm admin={admin} onPull={pull} />
            <PullList pulls={[...pulls.values()]} admin={admin} onCancel={(id) => void cancelPull(id).then(() => say(t('Pull cancelled'))).catch((e: Error) => say(e.message))} onDismiss={(id) => { dismissed.current.add(id); setPulls((cur) => { const n = new Map(cur); n.delete(id); return n; }); }} />
            <Discover endpointId={data.endpoint_id} vram={data.vram} admin={admin} onPull={pull} version={data.models.length} />
          </div>
        </>
      )}
    </section>
  );
}

/* ── the card(s) ── */

function VramCard({ vram, loaded, policy, admin, onPlacement, onRelease }: { vram: Vram; loaded: LoadedModel[]; policy?: { prefer: number }; admin: boolean; onPlacement: (prefer: number) => void; onRelease: (pid: number) => void }) {
  if (!vram?.supported) return <p className="fs-set__help">{t('No VRAM reading for this endpoint.')} {vram?.reason ?? ''}</p>;
  const total = vram.total_bytes ?? 0;
  const runner = vram.held_by_runner_bytes ?? 0;
  const others = vram.other_bytes ?? 0;
  const free = Math.max(0, total - runner - others);
  const pct = (v: number) => (total ? Math.max(0, Math.min(100, (100 * v) / total)) : 0);
  const multi = (vram.count ?? 0) > 1;
  const cards = multi ? (vram.gpus ?? []) : [];
  const names = loaded.map((m) => m.name).join(', ');
  return (
    <div className="fs-set__card fs-lm__vram">
      <div className="fs-lm__vram-head">
        <strong>
          {vram.name ?? 'GPU'}
          {multi && ` · ${tn(vram.count ?? 0, '{n} GPU', '{n} GPUs')}`}
        </strong>
        <span className="fs-set__help">{t('{used} of {total} used · {free} free', { used: fmtGb(runner + others), total: fmtGb(total), free: fmtGb(free) })}</span>
        {multi && admin && (
          <label className="fs-lm__placement">
            <span className="fs-set__help">{t('Fill first')}</span>
            <select className="fs-field" value={policy?.prefer ?? -1} onChange={(e) => onPlacement(Number(e.target.value))} title={t('Which card Ollama fills first. A model that fits the chosen card is pinned to it; bigger ones stay Auto and are split.')}>
              <option value={-1}>{t('Auto — freest card, split when nothing fits one')}</option>
              {cards.map((g) => (
                <option key={g.index} value={g.index}>
                  {t('Fill GPU {n} first — {name} ({gb} GB)', { n: g.index, name: shortGpuName(g.name), gb: Math.round((g.total_bytes ?? 0) / 1073741824) })}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <div className="fs-lm__bar" role="img" aria-label={t('VRAM: {a} models, {b} other, {c} free', { a: fmtGb(runner), b: fmtGb(others), c: fmtGb(free) })}>
        <span className="fs-lm__seg" data-kind="models" style={{ inlineSize: `${pct(runner).toFixed(1)}%` }} title={names ? `${t('Models loaded by Ollama')}: ${names}` : t('Models loaded by Ollama')} />
        <span className="fs-lm__seg" data-kind="other" style={{ inlineSize: `${pct(others).toFixed(1)}%` }} title={t('Other processes on the card')} />
      </div>
      <p className="fs-lm__legend fs-set__help">
        <span><i data-kind="models" /> {t('models')} {fmtGb(runner)}</span>
        <span><i data-kind="other" /> {t('other')} {fmtGb(others)}</span>
        <span title={t('CUDA context, cuBLAS workspace and compute buffers, gone before a single weight is loaded.')}>{t('reserve')} {fmtGb(multi ? (vram.reserve_per_gpu_bytes ?? vram.reserve_bytes) : vram.reserve_bytes)}</span>
        <span title={t('What a model\'s weights can take right now, KV cache not included.')}>
          {t('budget')} {fmtGb(vram.budget_bytes)}
          {multi && vram.largest_single_budget_bytes != null && ` (${t('one card up to {n}', { n: fmtGb(vram.largest_single_budget_bytes) })})`}
        </span>
      </p>
      {cards.map((g) => {
        const gt = g.total_bytes ?? 0;
        const used = Math.max(0, g.used_bytes ?? 0);
        const measured = g.models_bytes != null;
        const models = measured ? Math.max(0, g.models_bytes ?? 0) : 0;
        const other = measured ? Math.max(0, g.other_bytes ?? used - models) : 0;
        const gfree = Math.max(0, gt - used);
        const p = (v: number) => (gt ? Math.max(0, Math.min(100, (100 * v) / gt)) : 0);
        const mnames = (g.models ?? []).filter(Boolean).join(', ');
        return (
          <div key={g.index} className="fs-lm__gpu">
            <div className="fs-lm__vram-head">
              <strong>
                GPU {g.index} · {shortGpuName(g.name) || 'GPU'}
              </strong>
              <span className="fs-set__help">{t('{used} of {total} used · {free} free', { used: fmtGb(used), total: fmtGb(gt), free: fmtGb(gfree) })}</span>
            </div>
            <div className="fs-lm__bar" role="img" aria-label={measured ? `GPU ${g.index}: ${fmtGb(models)} ${t('models')}, ${fmtGb(other)} ${t('other')}, ${fmtGb(gfree)} ${t('free')}` : `GPU ${g.index}: ${fmtGb(used)} ${t('used')}, ${fmtGb(gfree)} ${t('free')}`}>
              {measured ? (
                <>
                  <span className="fs-lm__seg" data-kind="models" style={{ inlineSize: `${p(models).toFixed(1)}%` }} />
                  <span className="fs-lm__seg" data-kind="other" style={{ inlineSize: `${p(other).toFixed(1)}%` }} />
                </>
              ) : (
                <span className="fs-lm__seg" data-kind="used" style={{ inlineSize: `${p(used).toFixed(1)}%` }} />
              )}
            </div>
            <p className="fs-set__help">
              {mnames || t('nothing loaded on this card')}
              {g.budget_bytes != null && ` · ${t('budget')} ${fmtGb(g.budget_bytes)}`}
            </p>
          </div>
        );
      })}
      {(vram.orphans ?? []).map((o) => (
        <div key={o.pid} className="fs-lm__orphan">
          <span>
            {t('Orphaned runner')} <code>{o.name ?? 'runner'}</code> (pid {o.pid}){o.bytes != null && ` ${t('holds')} ${fmtGb(o.bytes)}`}{o.gpus?.length ? ` · #${o.gpus.join(', #')}` : ''}
          </span>
          {admin && <Button size="sm" variant="secondary" label={t('Release')} onClick={() => onRelease(o.pid)} title={t('Kill this runner and free its VRAM; the next request loads the model again')} />}
        </div>
      ))}
      {multi && <p className="fs-set__help">{t('Ollama places each model on the card with the most free memory and splits a model across cards only when it does not fit one; pin a card per model in Options (main_gpu).')}</p>}
    </div>
  );
}

/* ── loaded ── */

function Placement({ m, cards }: { m: LoadedModel; cards: GpuCard[] }) {
  const p = m.placement;
  if (!p || p === 'unknown') return null;
  const cardName = (idx: number) => shortGpuName(cards.find((x) => x.index === idx)?.name);
  if (p === 'cpu') return <span className="fs-lm__place" data-kind="cpu" title={t('No weights on a GPU: the model runs on the CPU.')}>CPU</span>;
  if (p === 'single') {
    const idx = m.gpus?.[0];
    if (idx == null) return null;
    return <span className="fs-lm__place" data-kind="single">GPU {idx}{cardName(idx) ? ` · ${cardName(idx)}` : ''}</span>;
  }
  const parts: { index: number; bytes?: number }[] = m.per_gpu?.length ? m.per_gpu : (m.gpus ?? []).map((i) => ({ index: i }));
  if (!parts.length) return null;
  return <span className="fs-lm__place" data-kind="split" title={t('Bigger than any one card: Ollama split the weights across {n} GPUs.', { n: parts.length })}>{t('split')} {parts.map((x) => `#${x.index}${x.bytes != null ? ` ${fmtGb(x.bytes)}` : ''}`).join(' + ')}</span>;
}

function LoadedList({ loaded, cards, admin, onUnload }: { loaded: LoadedModel[]; cards: GpuCard[]; admin: boolean; onUnload: (m: LoadedModel) => void }) {
  if (!loaded.length) return <p className="fs-set__help">{t('Nothing is loaded right now.')}</p>;
  return (
    <ul className="fs-lm__loaded">
      {loaded.map((m) => {
        const gpu = m.gpu_pct ?? 0;
        const spill = gpu < 100 && (m.size_cpu ?? 0) > 0;
        return (
          <li key={m.name} className="fs-lm__row">
            <span className="fs-lm__main">
              <strong>{m.name}</strong>
              <span className="fs-set__help">{t('{a} resident · {b} VRAM', { a: fmtGb(m.size), b: fmtGb(m.size_vram) })}</span>
              <Placement m={m} cards={cards} />
              <span className="fs-lm__split" data-spill={spill || undefined} title={spill ? t('{n} of the weights are in system RAM — expect PCIe paging and a fraction of the speed.', { n: fmtGb(m.size_cpu) }) : undefined}>
                {spill ? `${gpu}% GPU · ${100 - gpu}% CPU` : '100% GPU'}
              </span>
              {m.context_length ? <span className="fs-set__help">ctx {fmtCtx(m.context_length)}</span> : null}
              {untilText(m.expires_at) && <span className="fs-set__help" title={m.expires_at ?? undefined}>{untilText(m.expires_at)}</span>}
            </span>
            {admin && <Button size="sm" variant="ghost" label={t('Unload')} onClick={() => onUnload(m)} title={t('Evict from VRAM now (keep_alive 0)')} />}
          </li>
        );
      })}
    </ul>
  );
}

/* ── installed ── */

const FIT_WORD: Record<string, string> = { fits: 'fits', tight: 'tight', over: 'no fit', split: 'split' };

function FitBadge({ fit, size }: { fit?: Fit; size?: number }) {
  const state = fitState(fit);
  const word = FIT_WORD[state] ? t(FIT_WORD[state]) : '';
  return (
    <span className="fs-lm__fit" data-state={state || undefined} title={fit?.note ?? t('{size} on disk. Approximate — the KV cache grows on top of it with the context window.', { size: fmtGb(size) })}>
      {word ? `${fmtGb(size)} · ${word}` : fmtGb(size)}
    </span>
  );
}
const CAP_LABELS: [keyof Caps, string, string][] = [
  ['vision', 'vision', 'Accepts images'],
  ['tools', 'tools', 'Native tool calling'],
  ['thinking', 'think', 'Reasoning / thinking mode'],
  ['embedding', 'embed', 'Embedding model (no chat)'],
];
function CapsChips({ caps }: { caps?: Caps | string[] }) {
  const set = new Set(Array.isArray(caps) ? caps : Object.keys(caps ?? {}).filter((k) => (caps as Caps)[k as keyof Caps]));
  const items = CAP_LABELS.filter(([k]) => set.has(k));
  if (!items.length) return <span className="fs-set__help">—</span>;
  return (
    <>
      {items.map(([k, label, title]) => (
        <span key={k} className="fs-lm__cap" data-cap={k} title={t(title)}>
          {label}
        </span>
      ))}
    </>
  );
}
function optionsSummary(o?: Record<string, string | number>): string {
  if (!o || !Object.keys(o).length) return '';
  const bits: string[] = [];
  if (o.num_ctx != null) bits.push(`ctx ${fmtCtx(Number(o.num_ctx))}`);
  if (o.num_gpu != null) bits.push(`gpu ${o.num_gpu}`);
  if (o.main_gpu != null && o.main_gpu !== '') bits.push(`gpu #${o.main_gpu}`);
  if (o.keep_alive != null && o.keep_alive !== '') bits.push(`keep ${o.keep_alive}`);
  return bits.join(' · ');
}

function InstalledTable({ models, cards, admin, optionsFor, setOptionsFor, onLoad, onUnload, onDefault, onDelete, onSaveOptions }: { models: InstalledModel[]; cards: GpuCard[]; admin: boolean; optionsFor: string; setOptionsFor: (n: string) => void; onLoad: (m: InstalledModel) => void; onUnload: (m: InstalledModel) => void; onDefault: (m: InstalledModel) => void; onDelete: (m: InstalledModel) => void; onSaveOptions: (m: InstalledModel, opts: Record<string, string>) => Promise<void> }) {
  if (!models.length) return <p className="fs-set__help">{t('No models installed on this endpoint yet — pull one below.')}</p>;
  return (
    <div className="fs-lm__table" role="table">
      <div className="fs-lm__thead" role="row">
        <span>{t('Model')}</span>
        <span>{t('Size · fit')}</span>
        <span>{t('Quant · params')}</span>
        <span>{t('Caps')}</span>
        <span>{t('Ctx')}</span>
        <span />
      </div>
      {models.map((m) => {
        const summary = optionsSummary(m.options);
        const sub = [m.family || m.families?.[0], m.license, m.modified_at ? new Date(m.modified_at).toLocaleDateString(locale()) : ''].filter(Boolean).join(' · ');
        return (
          <div key={m.name} className="fs-lm__trow" data-loaded={m.loaded || undefined} role="row">
            <span className="fs-lm__main">
              <strong title={m.digest ? `digest ${m.digest}` : m.name}>{m.name}</strong>
              {m.loaded && <span className="fs-lm__pill">{t('loaded')}</span>}
              {sub && <span className="fs-set__help">{sub}</span>}
              {summary && <span className="fs-set__help" title={t('Saved load options')}>{summary}</span>}
            </span>
            <span>
              <FitBadge fit={m.fit} size={m.size} />
            </span>
            <span className="fs-set__help">{[m.quantization, m.parameter_size].filter(Boolean).join(' · ') || '—'}</span>
            <span className="fs-lm__caps">
              <CapsChips caps={m.capabilities} />
            </span>
            <span className="fs-set__help" title={t('Context length the model was trained for (from /api/show)')}>{fmtCtx(m.context_length)}</span>
            <span className="fs-lm__actions">
              {admin && (m.loaded ? <Button size="sm" variant="ghost" label={t('Unload')} onClick={() => onUnload(m)} /> : <Button size="sm" variant="ghost" label={t('Load')} onClick={() => onLoad(m)} title={t('Load into VRAM now')} />)}
              {admin && !m.capabilities?.embedding && <Button size="sm" variant="ghost" label={t('Set default')} onClick={() => onDefault(m)} title={t('Make this the default chat model (Settings → Default AI)')} />}
              {admin && <Button size="sm" variant="ghost" label={t('Options')} onClick={() => setOptionsFor(optionsFor === m.name ? '' : m.name)} title="num_ctx / num_gpu / keep_alive / main_gpu" />}
              {admin && <Button size="sm" variant="danger" label={t('Delete')} onClick={() => onDelete(m)} title={t('Remove the model files from this Ollama')} />}
            </span>
            {optionsFor === m.name && <OptionsForm model={m} cards={cards} onCancel={() => setOptionsFor('')} onSave={(opts) => onSaveOptions(m, opts)} />}
          </div>
        );
      })}
    </div>
  );
}

function OptionsForm({ model, cards, onCancel, onSave }: { model: InstalledModel; cards: GpuCard[]; onCancel: () => void; onSave: (opts: Record<string, string>) => Promise<void> }) {
  const o = model.options ?? {};
  const [ctx, setCtx] = useState(o.num_ctx == null ? '' : String(o.num_ctx));
  const [gpu, setGpu] = useState(o.num_gpu == null ? '' : String(o.num_gpu));
  const [main, setMain] = useState(o.main_gpu == null ? '' : String(o.main_gpu));
  const [keep, setKeep] = useState(o.keep_alive == null ? '' : String(o.keep_alive));
  const [busy, setBusy] = useState(false);
  const showMain = cards.length >= 2 || main !== '';
  const warn = pinWarning(main === '' ? null : Number(main), model.size, cards);
  return (
    <form
      className="fs-lm__options"
      onSubmit={(e) => {
        e.preventDefault();
        setBusy(true);
        void onSave({ num_ctx: ctx.trim(), num_gpu: gpu.trim(), main_gpu: main, keep_alive: keep.trim() }).finally(() => setBusy(false));
      }}
    >
      <label>
        num_ctx <span className="fs-set__help">{model.context_length ? t('(model max {n})', { n: fmtCtx(model.context_length) }) : ''}</span>
        <input className="fs-field" type="number" min={512} max={1048576} step={512} placeholder={t('model default')} value={ctx} onChange={(e) => setCtx(e.target.value)} />
      </label>
      <label>
        num_gpu <span className="fs-set__help">{t('(layers on the GPU)')}</span>
        <input className="fs-field" type="number" min={0} max={1024} step={1} placeholder={t('auto')} value={gpu} onChange={(e) => setGpu(e.target.value)} />
      </label>
      {showMain && (
        <label>
          main_gpu <span className="fs-set__help">{t('(pin to a card)')}</span>
          <select className="fs-field" value={main} onChange={(e) => setMain(e.target.value)}>
            <option value="">{t('Auto — Ollama picks the freest card, splits when needed')}</option>
            {cards.map((g) => (
              <option key={g.index} value={String(g.index)}>
                GPU {g.index} — {shortGpuName(g.name) || 'GPU'}{g.total_bytes ? ` (${Math.round(g.total_bytes / 1073741824)} GB)` : ''}
              </option>
            ))}
            {main !== '' && !cards.some((g) => String(g.index) === main) && <option value={main}>{t('GPU {n} — not listed on this endpoint', { n: main })}</option>}
          </select>
        </label>
      )}
      <label>
        keep_alive <span className="fs-set__help">{t('(5m, 1h, -1 = forever)')}</span>
        <input className="fs-field" placeholder="5m" value={keep} onChange={(e) => setKeep(e.target.value)} />
      </label>
      {warn && <p className="fs-set__help" data-tone="bad">{warn}</p>}
      <div className="fs-set__row-end">
        <span className="fs-set__err" style={{ color: 'var(--fs-text-3)' }}>{t('Applied to every request for this model on this endpoint, under anything the chat sets explicitly.')}</span>
        <Button size="sm" variant="ghost" label={t('Cancel')} onClick={onCancel} />
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} type="submit" />
      </div>
    </form>
  );
}

/* ── pulls + discover ── */

function PullForm({ admin, onPull }: { admin: boolean; onPull: (name: string) => Promise<void> }) {
  const [name, setName] = useState('');
  return (
    <form
      className="fs-set__inline"
      onSubmit={(e) => {
        e.preventDefault();
        if (!admin) return;
        void onPull(name).then(() => setName(''));
      }}
    >
      <input className="fs-field" value={name} onChange={(e) => setName(e.target.value)} placeholder="qwen3.5:9b, gemma3:12b, hf.co/user/repo:Q4_K_M…" aria-label={t('Model to pull')} spellCheck={false} disabled={!admin} />
      <Button size="sm" variant="primary" icon={Download} label={t('Pull')} disabled={!admin || !name.trim()} type="submit" />
    </form>
  );
}

function PullList({ pulls, admin, onCancel, onDismiss }: { pulls: Pull[]; admin: boolean; onCancel: (id: string) => void; onDismiss: (id: string) => void }) {
  if (!pulls.length) return null;
  return (
    <ul className="fs-lm__pulls">
      {pulls.map((p) => {
        const pct = p.percent ?? 0;
        const label = p.active ? `${p.status_text ?? t('pulling')}${p.total ? ` · ${fmtGb(p.completed)} / ${fmtGb(p.total)}` : ''}` : p.status === 'done' ? t('done') : p.status === 'cancelled' ? t('cancelled') : p.status === 'lost' ? t('lost — the server restarted; pull it again to resume') : `${t('failed')}: ${p.error ?? t('unknown error')}`;
        return (
          <li key={p.id} className="fs-lm__pull" data-state={p.status}>
            <div className="fs-lm__vram-head">
              <strong>{p.name}</strong>
              <span className="fs-set__help">{label}</span>
              {p.active && admin ? <Button size="sm" variant="ghost" label={t('Cancel')} onClick={() => onCancel(p.id)} /> : !p.active ? <IconButton icon={X} label={t('Hide')} size="sm" onClick={() => onDismiss(p.id)} /> : null}
            </div>
            <div className="fs-lm__bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
              <span className="fs-lm__seg" data-kind="models" data-indeterminate={(p.active && !p.total) || undefined} style={{ inlineSize: p.active && !p.total ? '30%' : `${(p.status === 'done' ? 100 : pct).toFixed(1)}%` }} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function Discover({ endpointId, vram, admin, onPull, version }: { endpointId: string; vram: Vram; admin: boolean; onPull: (name: string) => Promise<void>; version: number }) {
  const [q, setQ] = useState('');
  const [items, setItems] = useState<DiscoverEntry[] | null>(null);
  const seq = useRef(0);
  useEffect(() => {
    const id = ++seq.current;
    const timer = window.setTimeout(() => {
      discoverModels(q, endpointId)
        .then((list) => {
          if (id === seq.current) setItems(list);
        })
        .catch(() => setItems([]));
    }, 200);
    return () => window.clearTimeout(timer);
  }, [q, endpointId, version]);
  const note = !vram?.supported ? t('Sizes are approximate (the default build of each tag). No VRAM reading, so no fit verdict.') : t('Sizes are approximate (the default build of each tag). Fit is against {against} with nothing loaded: {usable} usable of {total}.', { against: `${vram.name ?? t('your card')}${(vram.count ?? 0) > 1 ? ` (${vram.count} GPUs)` : ''}`, usable: fmtGb(vram.clean_budget_bytes), total: fmtGb(vram.total_bytes) });
  return (
    <div className="fs-lm__discover">
      <div className="fs-lm__vram-head">
        <h4 className="fs-users__h" style={{ margin: 0 }}>{t('Discover')}</h4>
        <input type="search" className="fs-field" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t('Filter the catalogue: coder, vision, embedding, 7b…')} aria-label={t('Search the catalogue')} />
      </div>
      <p className="fs-set__help">{note}</p>
      {items === null ? (
        <Skeleton label={t('Loading')} count={2} height="48px" />
      ) : items.length === 0 ? (
        <p className="fs-set__help">{t('Nothing in the catalogue matches "{q}". You can still type its exact name above and pull it.', { q })}</p>
      ) : (
        <ul className="fs-lm__disc">
          {items.map((e) => (
            <li key={e.name} className="fs-lm__disc-row">
              <div className="fs-lm__vram-head">
                <strong>{e.name}</strong>
                <span className="fs-set__help">{e.vendor ?? ''}</span>
                <span className="fs-lm__caps">
                  <CapsChips caps={e.capabilities} />
                </span>
              </div>
              {e.blurb && <p className="fs-set__help">{e.blurb}</p>}
              <div className="fs-lm__tags">
                {e.tags.map((tag) => (
                  <span key={tag.name} className="fs-lm__tag" data-installed={tag.installed || undefined} data-default={tag.tag === e.default_tag || undefined}>
                    <span title={tag.name}>{tag.tag}</span>
                    <span className="fs-set__help">{tag.params ?? ''}</span>
                    <FitBadge fit={tag.fit} size={tag.size_bytes} />
                    {tag.installed ? <span className="fs-lm__pill">{t('installed')}</span> : admin ? <Button size="sm" variant="ghost" label={t('Pull')} onClick={() => void onPull(tag.name)} title={`ollama pull ${tag.name}`} /> : null}
                  </span>
                ))}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
