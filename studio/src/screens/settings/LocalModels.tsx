import { ArrowDown, ArrowUp, Download, HardDrive, RefreshCw, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, EmptyState, IconButton, Skeleton } from '../../components';
import { invalidateSettings } from '../../adapters/settings';
import {
  calibrateModel,
  cancelPull,
  capChipState,
  deleteModel,
  discoverModels,
  fitState,
  fmtCtx,
  fmtGb,
  loadLocalModels,
  loadModel,
  loadModelCapabilities,
  pinWarning,
  vramFit,
  type VramFit,
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
  type ModelCapabilityManifest,
  type Pull,
  type TestKey,
  type Vram,
} from '../../adapters/localModels';
import type { AdmissionAction, VramBlocked } from '../../adapters/vramAdmission';
import { locale, t, tn } from '../../i18n';
import { VramAdmissionDialog } from '../VramAdmissionDialog';
import { Select } from './fields';

const POLL_MS = 8000;

/** Thrown inside `act` when the outcome was shown some other way (a dialog). */
class NoToast extends Error {}

/* ── MOD-04: per-model options with scope (global / project / session) ──
 *
 * The adapter (studio/src/adapters/localModels.ts) is a foreign file this
 * lote does not touch, so the three new endpoints
 * (routes/local_models_routes.py's /options/scoped and /options/effective)
 * are called directly here, mirroring that adapter's own `call()`/`encName`
 * helpers rather than adding a second, differently-shaped client. */

type LoadScope = 'project' | 'session';

interface EffectiveOptions {
  endpoint_id: string;
  model: string;
  options: Record<string, string | number>;
  origin: Record<string, { scope: string; path: string }>;
  overridden: Record<string, { scope: string; value: string | number; path: string }[]>;
}

const encNameLocal = (name: string) => name.split('/').map(encodeURIComponent).join('/');

async function callLocalModels<T>(path: string, init: RequestInit = {}): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init });
  const text = await r.text();
  let data: unknown = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    /* not json */
  }
  if (!r.ok) {
    const d = data as { detail?: unknown; error?: string };
    throw new Error(typeof d.detail === 'string' ? d.detail : d.error ?? `HTTP ${r.status}`);
  }
  return data as T;
}

function getScopedOptions(endpointId: string, name: string, scope: LoadScope, scopeId: string) {
  const q = `scope=${scope}&scope_id=${encodeURIComponent(scopeId)}&endpoint_id=${encodeURIComponent(endpointId)}`;
  return callLocalModels<{ options: Record<string, string | number> }>(`/api/local-models/${encNameLocal(name)}/options/scoped?${q}`);
}

function putScopedOptions(endpointId: string, name: string, scope: LoadScope, scopeId: string, options: Record<string, string>) {
  const q = `scope=${scope}&scope_id=${encodeURIComponent(scopeId)}&endpoint_id=${encodeURIComponent(endpointId)}`;
  return callLocalModels<{ options: Record<string, string | number> }>(`/api/local-models/${encNameLocal(name)}/options/scoped?${q}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ options }),
  });
}

function getEffectiveOptions(endpointId: string, name: string, sessionId: string, projectId: string) {
  const params = new URLSearchParams({ endpoint_id: endpointId });
  if (sessionId) params.set('session_id', sessionId);
  if (projectId) params.set('project_id', projectId);
  return callLocalModels<EffectiveOptions>(`/api/local-models/${encNameLocal(name)}/options/effective?${params.toString()}`);
}

const SCOPE_LABEL: Record<string, string> = { session: 'Session', project: 'Project', global: 'Global default' };

/**
 * MOD-04's "quien manda" — a per-model panel where an admin can set a
 * project- or session-scoped override on top of the global default above,
 * and see which scope currently wins each field (session > project >
 * global), reusing `src/model_load_options.py::resolve_with_origin` through
 * the new `/options/effective` route rather than recomputing precedence
 * here.
 */
/** MOD-04: which of the four numeric override fields only take effect on the
 *  NEXT load of the model (`num_ctx`/`num_gpu`/`main_gpu` — Ollama runtime
 *  options fixed at load time) versus one that applies to the model as it
 *  already sits resident (`keep_alive`, honoured on the next request). A
 *  field in this set gets the "reload" badge and, if the saved value
 *  actually changes, the confirmation dialog below names the impact before
 *  the save goes through — the literal MOD-04 acceptance ("un cambio que
 *  exige recarga pide permiso y muestra impacto"). */
const RELOAD_FIELDS = new Set(['num_ctx', 'num_gpu', 'main_gpu']);

function ScopedOverridePanel({ endpointId, model }: { endpointId: string; model: InstalledModel }) {
  const [scope, setScope] = useState<LoadScope>('project');
  const [scopeId, setScopeId] = useState('');
  const [ctx, setCtx] = useState('');
  const [keep, setKeep] = useState('');
  const [gpu, setGpu] = useState('');
  const [main, setMain] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<Record<string, string | number> | null>(null);
  const [effective, setEffective] = useState<EffectiveOptions | null>(null);

  const load = useCallback(async () => {
    if (!scopeId.trim()) { setSaved(null); return; }
    setErr(null);
    try {
      const d = await getScopedOptions(endpointId, model.name, scope, scopeId.trim());
      setSaved(d.options);
      setCtx(d.options.num_ctx == null ? '' : String(d.options.num_ctx));
      setKeep(d.options.keep_alive == null ? '' : String(d.options.keep_alive));
      setGpu(d.options.num_gpu == null ? '' : String(d.options.num_gpu));
      setMain(d.options.main_gpu == null ? '' : String(d.options.main_gpu));
      const eff = await getEffectiveOptions(
        endpointId, model.name,
        scope === 'session' ? scopeId.trim() : '',
        scope === 'project' ? scopeId.trim() : '',
      );
      setEffective(eff);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [endpointId, model.name, scope, scopeId]);

  const doSave = async () => {
    setBusy(true);
    setErr(null);
    try {
      const d = await putScopedOptions(endpointId, model.name, scope, scopeId.trim(), { num_ctx: ctx.trim(), keep_alive: keep.trim(), num_gpu: gpu.trim(), main_gpu: main.trim() });
      setSaved(d.options);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!scopeId.trim()) { setErr(t('Enter a project or session id first.')); return; }
    const draft: Record<string, string> = { num_ctx: ctx.trim(), num_gpu: gpu.trim(), main_gpu: main.trim() };
    const changedReloadFields = Object.keys(draft).filter((k) => RELOAD_FIELDS.has(k) && draft[k] !== (saved?.[k] == null ? '' : String(saved[k])));
    if (changedReloadFields.length > 0) {
      const impact = model.loaded
        ? t('{model} is loaded right now; it will unload and reload with the new value the next time this scope uses it, interrupting anything using it mid-request.', { model: model.name })
        : t('{model} is not loaded right now; the new value applies the next time this scope loads it.', { model: model.name });
      if (!window.confirm(`${t('{fields} only takes effect on the next load — this requires a reload.', { fields: changedReloadFields.join(', ') })}\n\n${impact}\n\n${t('Save anyway?')}`)) return;
    }
    await doSave();
  };

  const clear = async () => {
    setBusy(true);
    setErr(null);
    try {
      await putScopedOptions(endpointId, model.name, scope, scopeId.trim(), {});
      setCtx('');
      setKeep('');
      setGpu('');
      setMain('');
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-lm__scoped" data-testid="scoped-options-panel">
      <strong>{t('Override for one project or session')}</strong>
      <p className="fs-set__help">{t('Wins over the global default above for that project or session only — nothing else changes.')}</p>
      <div className="fs-set__row">
        <select className="fs-field" value={scope} onChange={(e) => { setScope(e.target.value as LoadScope); setSaved(null); setEffective(null); }}>
          <option value="project">{t('Project')}</option>
          <option value="session">{t('Session')}</option>
        </select>
        <input
          className="fs-field"
          placeholder={scope === 'project' ? t('project id') : t('session id')}
          value={scopeId}
          onChange={(e) => setScopeId(e.target.value)}
          onBlur={() => void load()}
          data-testid="scoped-options-id"
        />
      </div>
      <div className="fs-set__row" title={t('num_ctx takes effect on the next load of the model (requires reload).')}>
        <input className="fs-field" type="number" min={512} max={1048576} step={512} placeholder="num_ctx" value={ctx} onChange={(e) => setCtx(e.target.value)} data-testid="scoped-options-num-ctx" />
        <span className="fs-set__restart">{t('reload')}</span>
        <input className="fs-field" type="number" min={0} max={1024} placeholder="num_gpu" value={gpu} onChange={(e) => setGpu(e.target.value)} data-testid="scoped-options-num-gpu" />
        <span className="fs-set__restart">{t('reload')}</span>
        <input className="fs-field" type="number" min={0} max={15} placeholder="main_gpu" value={main} onChange={(e) => setMain(e.target.value)} data-testid="scoped-options-main-gpu" />
        <span className="fs-set__restart">{t('reload')}</span>
        <input className="fs-field" placeholder="keep_alive" value={keep} onChange={(e) => setKeep(e.target.value)} data-testid="scoped-options-keep-alive" />
      </div>
      <p className="fs-set__help">{t('num_ctx, num_gpu and main_gpu only take effect the next time this scope loads the model; keep_alive applies to the model as it already sits loaded. Ollama-only values (the extra field above) that name a llama-server flag are rejected with the reason, not sent as an Ollama option.')}</p>
      <div className="fs-set__row-end" style={{ justifyContent: 'flex-start' }}>
        <Button size="sm" variant="ghost" label={t('Save override')} loading={busy} onClick={() => void save()} disabled={!scopeId.trim()} />
        {saved && Object.keys(saved).length > 0 && <Button size="sm" variant="ghost" label={t('Clear')} onClick={() => void clear()} disabled={busy} />}
      </div>
      {err && <p className="fs-set__help" data-tone="bad" role="alert">{err}</p>}
      {effective && (
        <ul className="fs-set__help" data-testid="scoped-options-origin">
          {Object.entries(effective.origin).map(([field, o]) => (
            <li key={field}>
              <strong>{field}</strong>: {String(effective.options[field])} — {t('set by {scope}', { scope: t(SCOPE_LABEL[o.scope] ?? o.scope) })}
              {(effective.overridden[field] ?? []).length > 0 && (
                <> ({t('over')} {(effective.overridden[field] ?? []).map((x) => `${t(SCOPE_LABEL[x.scope] ?? x.scope)}: ${x.value}`).join(', ')})</>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

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
  // ACT-06: 401/403 and 426 read as their own EmptyState tone instead of
  // the same generic "could not read" every other failure got.
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
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
        setErrorStatus(null);
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
        if (!silent) {
          setError((e as Error).message);
          setErrorStatus((e as { status?: number })?.status ?? null);
        }
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

  // Load / unload take seconds to minutes (a 27B is read off disk), and the
  // row used to show nothing at all until it was over — the button looked
  // dead. `working` names the model whose action is in flight: its button
  // spins, and the toast says what is happening now, not only afterwards.
  const [working, setWorking] = useState<string>('');
  // The capability manifest (announced vs tested) per model name. A ref
  // holds the cache itself — `capsTick` is the only thing that triggers a
  // re-render, so a manifest that arrives after the row it belongs to has
  // scrolled away does not restart the fetch loop below.
  const caps = useRef<Map<string, ModelCapabilityManifest>>(new Map());
  const [, setCapsTick] = useState(0);
  const [calibrating, setCalibrating] = useState<string>('');
  // The "no room in VRAM" question for the Load button (OBJ-1). The server
  // refused to load behind your back and sent what is resident; the dialog
  // asks, and the answer is carried out from here — this screen has no
  // waiting loader on the server the way a research does.
  const [blocked, setBlocked] = useState<{ model: InstalledModel; blocked: VramBlocked } | null>(null);
  const act = async (fn: () => Promise<unknown>, okMsg: string, opts?: { name?: string; startMsg?: string }) => {
    if (opts?.name) setWorking(opts.name);
    if (opts?.startMsg) say(opts.startMsg);
    const started = Date.now();
    try {
      await fn();
      const secs = Math.round((Date.now() - started) / 1000);
      say(secs >= 3 ? `${okMsg} · ${secs}s` : okMsg);
      afterChange();
    } catch (e) {
      if (!(e instanceof NoToast)) say((e as Error).message);
    } finally {
      if (opts?.name) setWorking('');
    }
  };
  const decideLoad = async (action: AdmissionAction, names: string[]) => {
    const target = blocked;
    if (!target) return;
    if (action === 'cancel') return;
    const embedding = !!target.model.capabilities?.embedding;
    await act(async () => {
      if (action === 'unload') {
        for (const n of names) {
          const victim = data?.models.find((x) => x.name === n);
          await unloadModel(data!.endpoint_id, n, !!victim?.capabilities?.embedding);
        }
      }
      // `force`: the question has been answered, one way or the other.
      const out = await loadModel(data!.endpoint_id, target.model.name, embedding, true);
      if ('blocked' in out) throw new Error(t('Still no room after unloading; nothing was loaded.'));
    }, t('Loaded {name}', { name: target.model.name }), {
      name: target.model.name,
      startMsg: action === 'unload' ? t('Unloading {names}, then loading {name}…', { names: names.join(', '), name: target.model.name }) : t('Loading {name} anyway…', { name: target.model.name }),
    });
  };

  // Best-effort: one capability manifest per installed model, fetched once
  // and cached in `caps` (a fresh /api/show read each time, but the tested
  // side only changes on Calibrate). A model whose manifest fails to load
  // just keeps showing announced-only chips — this never blocks the table.
  useEffect(() => {
    if (!data) return;
    let cancelled = false;
    (async () => {
      for (const m of data.models) {
        if (cancelled) return;
        if (caps.current.has(m.name)) continue;
        try {
          const manifest = await loadModelCapabilities(data.endpoint_id, m.name);
          if (cancelled) return;
          caps.current.set(m.name, manifest);
          setCapsTick((n) => n + 1);
        } catch {
          /* the row still renders with announced-only chips */
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [data]);

  const handleCalibrate = useCallback(async (m: InstalledModel) => {
    if (!data) return;
    setCalibrating(m.name);
    try {
      const manifest = await calibrateModel(data.endpoint_id, m.name);
      caps.current.set(m.name, manifest);
      setCapsTick((n) => n + 1);
      say(t('Calibrated {name}', { name: m.name }));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setCalibrating('');
    }
  }, [data, say]);

  if (error && !data) {
    if (errorStatus === 401 || errorStatus === 403) {
      return <EmptyState tone="denied" title={t('Administrators only')} body={t('This account cannot see the local models.')} />;
    }
    if (errorStatus === 426) {
      return <EmptyState tone="incompatible" title={t('This client is out of date')} body={t('Update Faustus before managing local models.')} />;
    }
    return <EmptyState icon={HardDrive} tone="error" title={t('Could not read the local models.')} body={error} primaryAction={{ label: t('Try again'), onClick: () => void refresh() }} />;
  }
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
          <VramCard vram={data.vram} loaded={data.loaded} policy={data.placement_policy} admin={admin} onPlacement={async (order) => { await setPlacement(order); await refresh(true); say(t('GPU priority saved.')); }} onRelease={(pid) => void act(() => releaseOrphanRunner(pid), t('Runner released.'))} />
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
              working={working}
              manifests={caps.current}
              calibrating={calibrating}
              endpointId={data.endpoint_id}
              onCalibrate={(m) => void handleCalibrate(m)}
              onLoad={(m) => void act(async () => {
                const out = await loadModel(data.endpoint_id, m.name, !!m.capabilities?.embedding);
                // No room next to what is resident: the server did not load
                // it. Ask (the same dialog a research shows), then act.
                if ('blocked' in out) {
                  setBlocked({ model: m, blocked: out.blocked });
                  throw new NoToast();
                }
              }, t('Loaded {name}', { name: m.name }), { name: m.name, startMsg: t('Loading {name}…', { name: m.name }) })}
              onUnload={(m) => void act(() => unloadModel(data.endpoint_id, m.name, !!m.capabilities?.embedding), t('Unloaded {name}', { name: m.name }), { name: m.name, startMsg: t('Unloading {name}…', { name: m.name }) })}
              onDefault={(m) => void act(() => setDefaultModel(data.endpoint_id, m.name), t('{name} is now the default chat model.', { name: m.name }))}
              onDelete={(m) => {
                if (!window.confirm(t('Delete {name} from this Ollama? The files are removed from disk; pull it again to get it back.', { name: m.name }))) return;
                void act(() => deleteModel(data.endpoint_id, m.name), t('Deleted {name}', { name: m.name }));
              }}
              onSaveOptions={async (m, opts) => {
                // Errors propagate: the form shows them next to the field
                // that caused them, where a toast would have vanished.
                const saved = await saveModelOptions(data.endpoint_id, m.name, opts);
                say(Object.keys(saved).length ? t('Saved options for {name}', { name: m.name }) : t('Cleared options for {name}', { name: m.name }));
                setOptionsFor('');
                afterChange();
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
      <VramAdmissionDialog
        blocked={blocked?.blocked ?? null}
        onDone={() => setBlocked(null)}
        say={(text) => say(text)}
        onDecide={decideLoad}
      />
    </section>
  );
}

/* ── the card(s) ── */

function VramCard({ vram, loaded, policy, admin, onPlacement, onRelease }: { vram: Vram; loaded: LoadedModel[]; policy?: { prefer: number; order?: number[] }; admin: boolean; onPlacement: (order: number[]) => Promise<void>; onRelease: (pid: number) => void }) {
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
        {multi && <GpuPriority cards={cards} policy={policy} admin={admin} onSave={onPlacement} />}
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
              {/* `fmtGb(0)` is "—" (unknown); an idle card is not unknown, it is empty. */}
            <span className="fs-set__help">{t('{used} of {total} used · {free} free', { used: used > 0 ? fmtGb(used) : '0 MB', total: fmtGb(gt), free: fmtGb(gfree) })}</span>
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
      {multi && <p className="fs-set__help">{t('New models use the first GPU with enough free memory, then the next. Loaded models stay in place. If none fits, Ollama decides how to split or offload. Per-model GPU settings take priority.')}</p>}
    </div>
  );
}

function GpuPriority({ cards, policy, admin, onSave }: { cards: GpuCard[]; policy?: { prefer: number; order?: number[] }; admin: boolean; onSave: (order: number[]) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const saved = policy?.order ?? ((policy?.prefer ?? -1) >= 0 ? [policy!.prefer] : []);
  const active = saved.length > 0;
  const order = [...saved.filter((i) => cards.some((g) => g.index === i)), ...cards.map((g) => g.index).filter((i) => !saved.includes(i))];
  const save = async (next: number[]) => {
    setBusy(true); setError('');
    try { await onSave(next); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const move = (position: number, delta: number) => {
    const next = [...order];
    [next[position], next[position + delta]] = [next[position + delta], next[position]];
    void save(next);
  };
  return <div className="fs-lm__placement" aria-busy={busy}>
    <label className="fs-lm__priority-mode">
      <span>{t('GPU priority')}</span>
      <select className="fs-field" value={active ? 'priority' : 'auto'} disabled={!admin || busy} onChange={(e) => void save(e.target.value === 'auto' ? [] : order)}>
        <option value="auto">{t('Automatic')}</option>
        <option value="priority">{t('Custom order')}</option>
      </select>
    </label>
    {active && <ol className="fs-lm__priority-list" aria-label={t('GPU fill order')}>
      {order.map((index, position) => {
        const card = cards.find((g) => g.index === index)!;
        return <li key={index}>
          <span className="fs-lm__priority-rank">{position + 1}</span>
          <span className="fs-lm__priority-name">GPU {index} · {shortGpuName(card.name)} <span className="fs-set__help">({fmtGb(card.total_bytes)})</span></span>
          {admin && <span className="fs-lm__priority-actions">
            <IconButton icon={ArrowUp} label={t('Move GPU {n} up', { n: index })} disabled={busy || position === 0} onClick={() => move(position, -1)} />
            <IconButton icon={ArrowDown} label={t('Move GPU {n} down', { n: index })} disabled={busy || position === order.length - 1} onClick={() => move(position, 1)} />
          </span>}
        </li>;
      })}
    </ol>}
    {busy && <span role="status" className="fs-set__help">{t('Saving…')}</span>}
    {error && <p role="alert">{error}</p>}
  </div>;
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

/** HW-02: the two fields collect_local_models now sends alongside every
 * loaded row — declared here rather than in the (foreign) adapter file,
 * since LoadedModel there does not carry them yet. */
type LoadedModelHw02 = LoadedModel & {
  gpu_ram_split_text?: string;
  kv?: { state: 'measured' | 'unknown'; bytes_per_token?: number; context_length?: number; total_bytes?: number };
};

function LoadedList({ loaded, cards, admin, onUnload }: { loaded: LoadedModelHw02[]; cards: GpuCard[]; admin: boolean; onUnload: (m: LoadedModel) => void }) {
  if (!loaded.length) return <p className="fs-set__help">{t('Nothing is loaded right now.')}</p>;
  // Two large models resident at once is how the machine went down on
  // 08-09-2026 (two 27B against the commit limit). Say it here, where the
  // person can unload one, rather than in a log nobody reads in time.
  const big = loaded.filter((m) => (m.size ?? 0) >= 8 * 1073741824);
  const crowded = big.length >= 2;
  return (
    <>
    {crowded && (
      <p className="fs-notice" data-tone="danger" role="alert" data-testid="vram-crowded">
        {t('{n} large models are loaded at once ({names}). Two 27B models stacked like this exhausted the machine\'s memory on 08-09-2026 — unload the one you are not using.', { n: big.length, names: big.map((m) => m.name).join(', ') })}
      </p>
    )}
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
              {/* HW-02: the split and the KV verdict said explicitly, not
                  only as a percentage or a hover title. */}
              {m.gpu_ram_split_text && <span className="fs-set__help" data-testid="hw02-split-text">{m.gpu_ram_split_text}</span>}
              <span className="fs-set__help" data-testid="hw02-kv" title={t('The bytes-per-token cost of the context cache, measured from this exact resident model — never a guess when unknown.')}>
                {m.kv?.state === 'measured'
                  ? t('kv: {gb} for {ctx} tokens', { gb: fmtGb(m.kv.total_bytes ?? 0), ctx: fmtCtx(m.kv.context_length ?? 0) })
                  : t('kv: unknown')}
              </span>
              {m.context_length ? <span className="fs-set__help">ctx {fmtCtx(m.context_length)}</span> : null}
              {untilText(m.expires_at) && <span className="fs-set__help" title={m.expires_at ?? undefined}>{untilText(m.expires_at)}</span>}
            </span>
            {admin && <Button size="sm" variant="ghost" label={t('Unload')} onClick={() => onUnload(m)} title={t('Evict from VRAM now (keep_alive 0)')} />}
          </li>
        );
      })}
    </ul>
    </>
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
// Only these two chips have a matching calibration probe (Lote 17); the
// other two (thinking, embedding) stay announced-only chips — nothing here
// tests reasoning mode or embedding output.
const CAP_TEST_KEY: Partial<Record<keyof Caps, TestKey>> = { vision: 'vision', tools: 'tool_calling' };

function capsEvidenceTitle(label: string, result?: { ok: boolean | null; tested_at?: string; evidence?: Record<string, unknown> }): string {
  if (!result) return t(label);
  const verdict = result.ok === true ? t('tested — passed') : result.ok === false ? t('tested — failed') : t('not tested');
  const when = result.tested_at ? new Date(result.tested_at).toLocaleString(locale()) : '';
  let evidence = '';
  try {
    evidence = result.evidence ? JSON.stringify(result.evidence).slice(0, 300) : '';
  } catch {
    /* evidence is best-effort context for the tooltip, never required */
  }
  return [t(label), when ? `${verdict} (${when})` : verdict, evidence].filter(Boolean).join('\n');
}
/** Announced (gray) vs probed ✓/✗ (green/red), from a calibration manifest — falls back to announced-only when none has loaded yet. */
function CapsChips({ caps, manifest }: { caps?: Caps | string[]; manifest?: ModelCapabilityManifest }) {
  const set = new Set(Array.isArray(caps) ? caps : Object.keys(caps ?? {}).filter((k) => (caps as Caps)[k as keyof Caps]));
  const items = CAP_LABELS.filter(([k]) => set.has(k));
  if (!items.length) return <span className="fs-set__help">—</span>;
  return (
    <>
      {items.map(([k, label, title]) => {
        const testKey = CAP_TEST_KEY[k];
        const result = testKey ? manifest?.tested?.[testKey] : undefined;
        const state = capChipState(result);
        return (
          <span key={k} className="fs-lm__cap" data-cap={k} data-state={state} title={capsEvidenceTitle(title, result)}>
            {label}
            {state === 'tested' && ' ✓'}
            {state === 'failed' && ' ✗'}
          </span>
        );
      })}
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
  const extra = (o as Record<string, unknown>).extra;
  if (extra && typeof extra === 'object') {
    const n = Object.keys(extra as object).length;
    if (n) bits.push(t('+{n} options', { n }));
  }
  return bits.join(' · ');
}

function InstalledTable({ models, cards, admin, optionsFor, setOptionsFor, working = '', manifests, calibrating = '', endpointId = '', onCalibrate, onLoad, onUnload, onDefault, onDelete, onSaveOptions }: { models: InstalledModel[]; cards: GpuCard[]; admin: boolean; optionsFor: string; setOptionsFor: (n: string) => void; working?: string; manifests?: Map<string, ModelCapabilityManifest>; calibrating?: string; endpointId?: string; onCalibrate: (m: InstalledModel) => void; onLoad: (m: InstalledModel) => void; onUnload: (m: InstalledModel) => void; onDefault: (m: InstalledModel) => void; onDelete: (m: InstalledModel) => void; onSaveOptions: (m: InstalledModel, opts: Record<string, string>) => Promise<void> }) {
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
              <CapsChips caps={m.capabilities} manifest={manifests?.get(m.name)} />
            </span>
            <span className="fs-set__help" title={t('Context length the model was trained for (from /api/show)')}>{fmtCtx(m.context_length)}</span>
            <span className="fs-lm__actions">
              {admin && (m.loaded
                ? <Button size="sm" variant="ghost" label={working === m.name ? t('Unloading…') : t('Unload')} loading={working === m.name} disabled={!!working && working !== m.name} onClick={() => onUnload(m)} />
                : <Button size="sm" variant="ghost" label={working === m.name ? t('Loading…') : t('Load')} loading={working === m.name} disabled={!!working && working !== m.name} onClick={() => onLoad(m)} title={t('Load into VRAM now')} />)}
              {admin && !m.capabilities?.embedding && <Button size="sm" variant="ghost" label={t('Set default')} onClick={() => onDefault(m)} title={t('Make this the default chat model (Settings → Default AI)')} />}
              {admin && !m.capabilities?.embedding && (
                <Button
                  size="sm"
                  variant="ghost"
                  label={calibrating === m.name ? t('Calibrating…') : t('Calibrate')}
                  loading={calibrating === m.name}
                  disabled={!m.loaded || (!!calibrating && calibrating !== m.name) || (!!working && working !== m.name)}
                  onClick={() => onCalibrate(m)}
                  title={m.loaded ? t('Run a brief capability check (under a minute) against the loaded model') : t('Load the model first — calibration never loads one on its own')}
                />
              )}
              {admin && <Button size="sm" variant="ghost" label={t('Options')} onClick={() => setOptionsFor(optionsFor === m.name ? '' : m.name)} title="num_ctx / num_gpu / keep_alive / main_gpu" />}
              {admin && <Button size="sm" variant="danger" label={t('Delete')} onClick={() => onDelete(m)} title={t('Remove the model files from this Ollama')} />}
            </span>
            {optionsFor === m.name && <OptionsForm model={m} cards={cards} endpointId={endpointId} onCancel={() => setOptionsFor('')} onSave={(opts) => onSaveOptions(m, opts)} />}
          </div>
        );
      })}
    </div>
  );
}

function OptionsForm({ model, cards, endpointId = '', onCancel, onSave }: { model: InstalledModel; cards: GpuCard[]; endpointId?: string; onCancel: () => void; onSave: (opts: Record<string, string>) => Promise<void> }) {
  const o = model.options ?? {};
  const [ctx, setCtx] = useState(o.num_ctx == null ? '' : String(o.num_ctx));
  const [gpu, setGpu] = useState(o.num_gpu == null ? '' : String(o.num_gpu));
  const [main, setMain] = useState(o.main_gpu == null ? '' : String(o.main_gpu));
  const [keep, setKeep] = useState(o.keep_alive == null ? '' : String(o.keep_alive));
  // Further Ollama `options` by name, as JSON. What Ollama accepts per
  // request is a fixed list (src/model_load_options.EXTRA_OPTION_KEYS); the
  // server names it in the error when a key is not on it.
  const [extra, setExtra] = useState(() => {
    const block = (o as Record<string, unknown>).extra;
    return block && typeof block === 'object' && Object.keys(block as object).length ? JSON.stringify(block, null, 1).replace(/\n\s*/g, ' ') : '';
  });
  const [busy, setBusy] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  // The advisor: it measures rather than guesses, and fills the two fields
  // that decide whether the model runs on the card or crawls on the CPU.
  const [fitting, setFitting] = useState(false);
  const [plan, setPlan] = useState<VramFit | null>(null);
  const [fitErr, setFitErr] = useState<string | null>(null);
  const showMain = cards.length >= 2 || main !== '';
  const warn = pinWarning(main === '' ? null : Number(main), model.size, cards);
  const [preview, setPreview] = useState<VramFit | null>(null);
  const [previewError, setPreviewError] = useState(false);
  useEffect(() => {
    let active = true;
    const refresh = () => void vramFit(model.name).then(value => {
      if (active) { setPreview(value); setPreviewError(false); }
    }).catch(() => { if (active) { setPreview(null); setPreviewError(true); } });
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    return () => { active = false; window.clearInterval(timer); };
  }, [model.name]);
  const previewCtx = ctx.trim() ? Number(ctx) : preview?.runtimeContext;
  const canEstimate = gpu === '' && main === '' && Number.isInteger(previewCtx) && Number(previewCtx) >= 512 && preview?.bytesPerToken != null && preview.previewBudget != null && preview.weights > 0;
  const needed = canEstimate ? preview!.weights + preview!.bytesPerToken! * Number(previewCtx) : null;
  const excess = needed == null ? null : Math.max(0, needed - preview!.previewBudget!);
  // Keep the existing per-card reserve and round DOWN to the input's step.
  // This is independent of the edited context; the user can see the ceiling
  // before entering a value. Manual placement cannot use a pooled estimate.
  const maxVramContext = gpu === '' && main === '' && preview?.bytesPerToken != null && preview.bytesPerToken > 0 && preview.previewBudget != null && preview.weights > 0
    ? Math.max(0, Math.floor(Math.min(
      (preview.previewBudget - preview.weights) / preview.bytesPerToken,
      model.context_length || 1048576,
      1048576,
    ) / 512) * 512)
    : null;

  const suggest = async () => {
    setFitting(true);
    setFitErr(null);
    try {
      const p = await vramFit(model.name, ctx.trim() ? Number(ctx.trim()) : undefined);
      setPlan(p);
      if (p.num_ctx) setCtx(String(p.num_ctx));
      // null means "let Ollama decide", which is the right answer when it
      // all fits: writing a number there would pin it for no reason.
      setGpu(p.num_gpu == null ? '' : String(p.num_gpu));
    } catch (err) {
      setFitErr((err as Error).message);
    } finally {
      setFitting(false);
    }
  };
  return (
    <form
      className="fs-lm__options"
      onSubmit={(e) => {
        e.preventDefault();
        setBusy(true);
        setSaveErr(null);
        void onSave({ num_ctx: ctx.trim(), num_gpu: gpu.trim(), main_gpu: main, keep_alive: keep.trim(), extra: extra.trim() })
          .catch((err) => setSaveErr((err as Error).message))
          .finally(() => setBusy(false));
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
      <label className="fs-lm__options-extra">
        {t('Other options')} <span className="fs-set__help">{t('(JSON, sent to Ollama with every request)')}</span>
        <textarea
          className="fs-field"
          rows={2}
          spellCheck={false}
          placeholder='{"num_batch": 512, "min_p": 0.05, "repeat_penalty": 1.1}'
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
          data-testid="options-extra"
        />
        <span className="fs-set__help">{t('Only Ollama request options are accepted (num_batch, num_thread, min_p, top_k, repeat_penalty, seed, stop…). llama-server flags such as --jinja, --spec-* or --cache-type-* are not per-request options; set them where Ollama starts its runner.')}</span>
      </label>
      {warn && <p className="fs-set__help" data-tone="bad">{warn}</p>}
      {saveErr && <p className="fs-set__help" data-tone="bad" role="alert" data-testid="options-save-error">{saveErr}</p>}
      <div className="fs-lm__plan fs-lm__memory-preview" role="status" aria-live="polite" data-fits={excess === 0 || undefined} data-testid="context-memory-preview">
        <strong>{t('Context memory preview')}</strong>
        {maxVramContext != null && <p className="fs-set__help">
          <strong>{maxVramContext >= 512
            ? t('Estimated maximum in VRAM: {n} tokens.', { n: maxVramContext.toLocaleString(locale()) })
            : t('No context fits entirely in the available VRAM.')}</strong>
          {' '}{t('Includes GPU reserve and the model limit. Changes with available memory; not a guarantee against spill.')}
        </p>}
        {maxVramContext != null && maxVramContext >= 512 && <Button size="sm" variant="ghost"
          label={t('Use estimated maximum')}
          disabled={busy || Number(ctx) === maxVramContext}
          onClick={() => { setCtx(String(maxVramContext)); setPlan(null); }} />}
        <p className="fs-set__help">{excess == null
          ? t(previewError ? 'Memory estimate unavailable. Retry by reopening Options.' : !preview ? 'Checking memory…' : 'Enter a context and use automatic GPU layers and placement to estimate memory.')
          : excess > 0
            ? t('RAM required · PCIe spill risk. Estimated overflow: {ram}.', { ram: fmtGb(excess) })
            : t('Fits in VRAM · no PCIe spill expected.')}</p>
        {needed != null && <p className="fs-set__help">{t('Estimate: {needed} needed / {available} VRAM available. Actual placement may differ; RAM use can reduce speed.', { needed: fmtGb(needed), available: fmtGb(preview!.previewBudget!) })}</p>}
        {preview && <p className="fs-set__help">{t('Live Ollama: {state}', { state: t(preview.runtimeSpilling === true ? 'PCIe spill detected' : preview.runtimeSpilling === false ? 'no PCIe spill detected' : 'PCIe spill telemetry unavailable') })}
          {preview.runtimeContext != null && <> · {t('Loaded context: {ctx}; model RAM: {ram}.', { ctx: fmtCtx(preview.runtimeContext), ram: fmtGb(preview.runtimeRam ?? 0) })}</>}
        </p>}
        <p className="fs-set__help">{t('The estimate follows your edits. Live readings describe the loaded model; saving applies the new context on its next request.')}</p>
      </div>
      {plan && (
        <div className="fs-lm__plan" data-fits={plan.fits || undefined} data-testid="vram-plan">
          <p className="fs-set__help">
            {plan.fits
              ? t('It fits on {gpu}.', { gpu: plan.gpuName || t('the card') })
              : t('It does not fit whole on {gpu}. This is the best split:', { gpu: plan.gpuName || t('the card') })}
          </p>
          {plan.steps.length > 0 && (
            <ul className="fs-lm__steps">
              {plan.steps.map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {fitErr && <p className="fs-set__help" data-tone="bad">{fitErr}</p>}
      <div className="fs-set__row-end">
        <Button size="sm" variant="ghost" label={t('Fit to VRAM')} loading={fitting} onClick={() => void suggest()} title={t('Measures this model against the free VRAM and fills num_ctx and num_gpu.')} />
        <span className="fs-set__err" style={{ color: 'var(--fs-text-3)' }}>{t('Applied to every request for this model on this endpoint, under anything the chat sets explicitly.')}</span>
        <Button size="sm" variant="ghost" label={t('Cancel')} onClick={onCancel} />
        <Button size="sm" variant="primary" label={t('Save')} loading={busy} type="submit" />
      </div>
      {endpointId && <ScopedOverridePanel endpointId={endpointId} model={model} />}
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
