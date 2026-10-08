import { ExternalLink, RefreshCw, Server } from 'lucide-react';
import { useState } from 'react';
import { Button, IconButton, Popover } from '../../components';
import {
  deployRecipe,
  fmtCtxShort,
  gbOf,
  saveSparksSettings,
  useSparks,
  type SparkDeployment,
  type SparkNode,
  type SparkRecipe,
  type SparksState,
} from '../../adapters/sparks';
import { t } from '../../i18n';
import './vitals.css';
import './sparks.css';

/**
 * The DGX Spark cluster, in the Studio header, next to the pill of this PC's GPUs (both stay: the Sparks are the
 * default backend, the local cards keep their own models). The trigger draws the same three glances as the local
 * pill — GPU trace, one memory tank per Spark (unified memory: there is no separate VRAM), the loaded model — and the
 * popover is the cluster: each Spark, what is loaded, the recipes to load or unload, and whether new chats go to the
 * Sparks. Prometheus's Hoard does the work; Faustus only asks it (src/sparks.py).
 */
export function SparkVitals() {
  const [open, setOpen] = useState(false);
  const { state, history, refresh } = useSparks(open);
  if (!state || (!state.ok && state.error === 'disabled')) return null;

  const online = state.nodes.filter((n) => n.online);
  const util = online.length ? online.reduce((a, n) => a + (n.gpu?.util ?? 0), 0) / online.length : 0;
  const memUsed = online.reduce((a, n) => a + (n.memory?.used ?? 0), 0);
  const memTotal = online.reduce((a, n) => a + (n.memory?.total ?? 0), 0);
  const running = state.deployments.filter((d) => d.state === 'running');
  const starting = state.deployments.find((d) => d.state === 'starting' || d.state === 'stopping');
  const avgTrace = averageTrace(history, online.map((n) => n.id));

  const trigger = (
    <button
      type="button"
      className="fs-vitals fs-sparks"
      data-empty={!state.ok || undefined}
      aria-label={t('DGX Sparks: GPU, memory and loaded model')}
      title={t('DGX Sparks (click for the detail)')}
      data-testid="spark-vitals"
    >
      <Server size={13} aria-hidden="true" className="fs-sparks__icon" />
      {!state.ok ? (
        <span className="fs-vitals__of">{t('Sparks: unreachable')}</span>
      ) : (
        <>
          <Trace samples={avgTrace} />
          <span className="fs-vitals__pct">{Math.round(util)}%</span>
          <span className="fs-vitals__tanks" aria-hidden="true">
            {state.nodes.map((n) => (
              <span key={n.id} className="fs-vitals__tank" data-off={!n.online || undefined} data-level={levelOf(n.memory?.percent) || undefined} style={{ flexGrow: 1 }}>
                <span style={{ inlineSize: `${n.online ? Math.min(100, n.memory?.percent ?? 0) : 0}%` }} />
              </span>
            ))}
          </span>
          <span className="fs-vitals__vram">
            {gbOf(memUsed)}
            <span className="fs-vitals__of">/{gbOf(memTotal)} GB</span>
          </span>
          {starting ? (
            <span className="fs-vitals__model fs-sparks__busy">{starting.state === 'starting' ? t('loading…') : t('unloading…')}</span>
          ) : running.length ? (
            <span className="fs-vitals__model" data-testid="spark-vitals-model">
              <span className="fs-vitals__mname">{running[0].served?.[0] || running[0].served_model_name || running[0].title}</span>
              {running.length > 1 && <span className="fs-vitals__more"> +{running.length - 1}</span>}
            </span>
          ) : (
            <span className="fs-vitals__model fs-vitals__of">{t('no model')}</span>
          )}
          <span className="fs-sparks__count fs-vitals__of">
            {online.length}/{state.nodes.length}
          </span>
        </>
      )}
    </button>
  );

  return (
    <Popover trigger={trigger} align="end" className="fs-vt fs-sparks-panel" testId="spark-vitals-panel" open={open} onOpenChange={setOpen}>
      <Panel s={state} refresh={refresh} />
    </Popover>
  );
}

function averageTrace(history: Record<string, number[]>, ids: string[]): number[] {
  const series = ids.map((id) => history[id] ?? []).filter((s) => s.length);
  if (!series.length) return [0, 0];
  const n = Math.max(...series.map((s) => s.length));
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const vals = series.map((s) => s[s.length - n + i]).filter((v) => v != null);
    out.push(vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0);
  }
  return out;
}

function levelOf(p?: number | null): '' | 'warm' | 'hot' {
  if (p == null) return '';
  return p >= 92 ? 'hot' : p >= 80 ? 'warm' : '';
}

const W = 56;
const H = 18;
function Trace({ samples }: { samples: number[] }) {
  const pts = samples.length >= 2 ? samples : [samples[0] ?? 0, samples[0] ?? 0];
  const step = W / Math.max(1, pts.length - 1);
  const y = (v: number) => H - 1.5 - (Math.max(0, Math.min(100, v)) / 100) * (H - 3);
  const line = pts.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  return (
    <svg className="fs-vitals__trace" viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden="true" data-note="guard-ok: a live chart drawn from data, not an icon">
      <polyline points={line} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function Meter({ value, total }: { value: number; total: number }) {
  const p = total ? Math.max(0, Math.min(100, (value / total) * 100)) : 0;
  return (
    <span className="fs-vt__meter" data-level={levelOf(p) || undefined}>
      <span style={{ inlineSize: `${p.toFixed(1)}%` }} />
    </span>
  );
}

function Panel({ s, refresh }: { s: SparksState; refresh: () => Promise<void> }) {
  const [spinning, setSpinning] = useState(false);
  const [notice, setNotice] = useState('');
  const [working, setWorking] = useState('');
  const [conflict, setConflict] = useState<{ recipe: string; with: string[] } | null>(null);

  const act = async (recipe: string, action: 'start' | 'stop', stopConflicts = false) => {
    setWorking(recipe);
    setNotice('');
    const res = await deployRecipe(recipe, action, stopConflicts);
    setWorking('');
    if (res.ok === false) {
      if (res.status === 409 && Array.isArray(res.conflicts)) {
        setConflict({ recipe, with: res.conflicts as string[] });
        return;
      }
      setNotice(String(res.error || t('Prometheus refused it.')) + (res.hint ? ` — ${String(res.hint)}` : ''));
    } else {
      setConflict(null);
      setNotice(action === 'start' ? t('Loading {name}: the panel shows its progress.', { name: recipe }) : t('{name} unloaded.', { name: recipe }));
    }
    await refresh();
  };

  const toggleDefault = async (on: boolean) => {
    setNotice('');
    const res = await saveSparksSettings({ default_backend: on });
    if (res.ok === false) setNotice(String(res.error || t('Could not save.')));
    await refresh();
  };

  return (
    <div className="fs-vt__body" data-testid="spark-panel">
      <header className="fs-vt__head">
        <span className="fs-vt__title">
          <span className="fs-vt__dot" data-level={s.ok ? undefined : 'hot'} />
          {t('DGX Sparks')}
        </span>
        <span className="fs-vt__actions">
          <IconButton
            icon={RefreshCw}
            label={t('Refresh now')}
            size="sm"
            onClick={() => {
              setSpinning(true);
              void refresh().finally(() => setSpinning(false));
            }}
            disabled={spinning}
          />
          {s.url && <IconButton icon={ExternalLink} label={t("Open Prometheus's Hoard")} size="sm" onClick={() => window.open(s.url, '_blank', 'noopener,noreferrer')} />}
        </span>
      </header>
      {!s.ok ? (
        <p className="fs-vt__muted">
          {t("Prometheus's Hoard does not answer at {url}. Start it from the Hoard Hub, or change the address in Settings → Sparks.", { url: s.url })}
        </p>
      ) : (
        <>
          <DefaultSection s={s} onToggle={toggleDefault} />
          <section className="fs-vt__section">
            <h3 className="fs-vt__h">
              {t('Sparks')}
              <span className="fs-vt__aside">{t('{a} of {b} on', { a: s.nodes.filter((n) => n.online).length, b: s.nodes.length })}</span>
            </h3>
            {s.nodes.map((n) => (
              <NodeRow key={n.id} n={n} />
            ))}
          </section>
          <LoadedSection s={s} working={working} onStop={(r) => void act(r, 'stop')} />
          <RecipesSection s={s} working={working} conflict={conflict} onStart={(r, force) => void act(r, 'start', force)} onCancel={() => setConflict(null)} />
          {s.jobs.length > 0 && (
            <section className="fs-vt__section">
              <h3 className="fs-vt__h">{t('In progress')}</h3>
              {s.jobs.map((j) => (
                <div key={j.id} className="fs-vt__row" data-wide="">
                  <span className="fs-vt__label">{j.title}</span>
                  <span className="fs-vt__val">{j.progress != null ? `${Math.round(j.progress * 100)}%` : j.state}</span>
                </div>
              ))}
            </section>
          )}
          {notice && <p className="fs-vt__muted fs-sparks__notice" role="status">{notice}</p>}
        </>
      )}
    </div>
  );
}

function DefaultSection({ s, onToggle }: { s: SparksState; onToggle: (on: boolean) => void }) {
  const eff = s.effective;
  return (
    <section className="fs-vt__section">
      <h3 className="fs-vt__h">{t('Default backend')}</h3>
      <div className="fs-vt__row" data-wide="">
        <span className="fs-vt__label">{t('New chats')}</span>
        <span className="fs-vt__val" data-testid="spark-default">
          {eff?.on_sparks ? t('Sparks · {model}', { model: eff.model }) : eff?.model ? t('This PC · {model}', { model: eff.model }) : '—'}
        </span>
      </div>
      <label className="fs-sparks__switch">
        <input type="checkbox" checked={s.default_backend} onChange={(e) => onToggle(e.target.checked)} data-testid="spark-default-toggle" />
        <span>{t('Use the Sparks by default while a model is loaded there')}</span>
      </label>
      {!eff?.on_sparks && s.default_backend && (
        <p className="fs-vt__muted">{t('Nothing is loaded on the Sparks: new chats use this PC until a recipe serves.')}</p>
      )}
    </section>
  );
}

function NodeRow({ n }: { n: SparkNode }) {
  if (!n.online) {
    return (
      <div className="fs-vt__row fs-sparks__node" data-off="">
        <span className="fs-vt__label">{n.name}</span>
        <span />
        <span className="fs-vt__val" data-muted="">
          {n.power_state === 'restarting' ? t('restarting') : n.power_state === 'waking' ? t('turning on') : t('off or unreachable')}
        </span>
      </div>
    );
  }
  const used = n.memory?.used ?? 0;
  const total = n.memory?.total ?? 0;
  return (
    <div className="fs-sparks__node" data-testid={`spark-node-${n.id}`}>
      <div className="fs-vt__row">
        <span className="fs-vt__label">{n.name}</span>
        <Meter value={used} total={total} />
        <span className="fs-vt__val">
          {gbOf(used, 0)}/{gbOf(total, 0)} GB
        </span>
      </div>
      <div className="fs-sparks__sub">
        <span>GPU {Math.round(n.gpu?.util ?? 0)}%</span>
        {n.gpu?.temp_c != null && <span>{Math.round(n.gpu.temp_c)}°</span>}
        {n.gpu?.power_w != null && <span>{Math.round(n.gpu.power_w)} W</span>}
        <span>CPU {Math.round(n.cpu?.percent ?? 0)}%</span>
        <span>
          CX7 {n.fabric?.up ?? 0}/{n.fabric?.total ?? 0}
        </span>
        {n.deployments?.map((d) => (
          <span key={d.recipe} className="fs-sparks__chip">
            {d.served?.[0] || d.title} · {d.role === 'head' ? t('head') : t('worker')}
          </span>
        ))}
      </div>
    </div>
  );
}

function LoadedSection({ s, working, onStop }: { s: SparksState; working: string; onStop: (recipe: string) => void }) {
  const live = s.deployments.filter((d) => ['running', 'starting', 'stopping', 'failed'].includes(d.state));
  if (!live.length) return null;
  return (
    <section className="fs-vt__section">
      <h3 className="fs-vt__h">{t('Loaded')}</h3>
      {live.map((d: SparkDeployment) => (
        <div key={d.recipe} className="fs-sparks__dep" data-state={d.state} data-testid={`spark-dep-${d.recipe}`}>
          <div className="fs-sparks__dep-head">
            <span className="fs-sparks__dep-title">{d.title}</span>
            <span className="fs-sparks__state">{stateLabel(d.state)}</span>
          </div>
          <div className="fs-sparks__sub">
            <span>{d.served?.[0] || d.served_model_name}</span>
            {d.max_model_len ? <span>{fmtCtxShort(d.max_model_len)} {t('context')}</span> : null}
            <span>{d.nodes.join(' + ')}</span>
          </div>
          {(d.state === 'starting' || d.state === 'failed') && d.message && <p className="fs-vt__muted">{d.message}</p>}
          {d.base_url && <code className="fs-sparks__url">{d.base_url}</code>}
          {d.detected && <p className="fs-vt__muted">{t('Started outside a recipe: stop it from Prometheus or where it was started.')}</p>}
          {d.state !== 'stopping' && !d.detected && (
            <Button size="sm" variant="secondary" label={t('Unload')} loading={working === d.recipe} onClick={() => onStop(d.recipe)} testId={`spark-unload-${d.recipe}`} />
          )}
        </div>
      ))}
    </section>
  );
}

function RecipesSection({ s, working, conflict, onStart, onCancel }: { s: SparksState; working: string; conflict: { recipe: string; with: string[] } | null; onStart: (recipe: string, force: boolean) => void; onCancel: () => void }) {
  const loaded = new Set(s.deployments.filter((d) => ['running', 'starting'].includes(d.state)).map((d) => d.recipe));
  const recipes = s.recipes.filter((r: SparkRecipe) => !loaded.has(r.name));
  if (!s.recipes.length) {
    return (
      <section className="fs-vt__section">
        <h3 className="fs-vt__h">{t('Recipes')}</h3>
        <p className="fs-vt__muted">{t("There are no recipes in Prometheus's Hoard yet.")}</p>
      </section>
    );
  }
  if (!recipes.length) return null;
  return (
    <section className="fs-vt__section">
      <h3 className="fs-vt__h">{t('Load a model')}</h3>
      {recipes.map((r) => (
        <div key={r.name} className="fs-sparks__recipe" data-testid={`spark-recipe-${r.name}`}>
          <div className="fs-sparks__dep-head">
            <span className="fs-sparks__dep-title">{r.title}</span>
            {r.invalid ? (
              <span className="fs-sparks__state" data-state="failed">{t('invalid')}</span>
            ) : (
              <Button size="sm" variant="primary" label={t('Load')} loading={working === r.name} onClick={() => onStart(r.name, false)} testId={`spark-load-${r.name}`} />
            )}
          </div>
          <div className="fs-sparks__sub">
            {r.nodes?.length ? <span>{r.nodes.join(' + ')}</span> : null}
            {r.max_model_len ? <span>{fmtCtxShort(r.max_model_len)} {t('context')}</span> : null}
            {r.memory_gb ? <span>~{r.memory_gb} GB/Spark</span> : null}
            {r.context_verified === false && <span>{t('context not verified yet')}</span>}
          </div>
          {r.invalid && <p className="fs-vt__muted">{r.invalid}</p>}
          {conflict?.recipe === r.name && (
            <div className="fs-sparks__conflict" role="alert">
              <span>{t('{other} is using those Sparks. Unload it and load {name}?', { other: conflict.with.join(', '), name: r.title })}</span>
              <span className="fs-sparks__conflict-actions">
                <Button size="sm" variant="primary" label={t('Unload and load')} onClick={() => onStart(r.name, true)} testId={`spark-force-${r.name}`} />
                <Button size="sm" variant="ghost" label={t('Cancel')} onClick={onCancel} />
              </span>
            </div>
          )}
        </div>
      ))}
    </section>
  );
}

function stateLabel(state: string): string {
  switch (state) {
    case 'running':
      return t('serving');
    case 'starting':
      return t('loading…');
    case 'stopping':
      return t('unloading…');
    case 'failed':
      return t('failed');
    default:
      return state;
  }
}
