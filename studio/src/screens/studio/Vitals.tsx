import { EyeOff, RefreshCw } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Button, IconButton, Popover } from '../../components';
import {
  fmtCtx,
  firstModel,
  gb,
  gbInt,
  isMulti,
  level,
  mbytes,
  mib2gb,
  pct,
  placementText,
  poolOf,
  releaseOrphan,
  refreshUsage,
  setUsageVisible,
  shortGpuName,
  spilling,
  untilText,
  useClock,
  useUsage,
  worstLevel,
  type Gpu,
  type Usage,
} from '../../adapters/usage';
import { reconnectServices, SERVICE_LABEL, useServiceHealth, type ServiceState } from '../../adapters/services';
import { locale, t, tn } from '../../i18n';
import './vitals.css';

/**
 * The machine's vitals, in the Studio header.
 *
 * The previous interface said it in words — `GPU 18% · 5.5/28G · 44° · no
 * model · RAM 43%` — and the words were the problem: eleven tokens to scan
 * for the one that matters. Here the three things worth a glance are drawn,
 * not written: a trace of GPU utilisation over the last two minutes (busy
 * or idle, and for how long), one tank per card for VRAM (the cards are
 * different sizes, so the tanks are), and the temperature as the only
 * number that turns colour. The loaded model is named; a PCIe spill —
 * the failure every other gauge hides — is the one thing that shouts.
 *
 * Everything the old panel knew is in the popover, with the same sources.
 */
export function Vitals({ busy }: { busy: boolean }) {
  const { last, history, visible, intervalMs } = useUsage(busy);

  if (!visible) return null;

  const pool = poolOf(last);
  const gpus = last?.gpu ?? [];
  const model = firstModel(last);
  const spill = spilling(last);
  const lvl = worstLevel(last);
  const util = pool.util;

  const trigger = (
    <button
      type="button"
      className="fs-vitals"
      data-level={lvl || undefined}
      data-spill={spill || undefined}
      data-empty={!last || undefined}
      aria-label={t('Live usage: GPU, VRAM, loaded model, RAM')}
      title={t('Live usage (click for the detail)')}
      data-testid="vitals"
    >
      {last ? (
        <>
          {gpus.length > 0 ? (
            <>
              <Trace samples={history} />
              <span className="fs-vitals__pct">{pct(util)}</span>
              <span className="fs-vitals__tanks" aria-hidden="true">
                {gpus.map((g) => (
                  <Tank key={g.index} gpu={g} />
                ))}
              </span>
              <span className="fs-vitals__vram">
                {mib2gb(pool.mem_used)}
                <span className="fs-vitals__of">/{gbInt(pool.mem_total)} GB</span>
              </span>
              {pool.temp != null && (
                <span className="fs-vitals__temp" data-level={pool.temp >= 85 ? 'hot' : pool.temp >= 75 ? 'warm' : undefined}>
                  {Math.round(pool.temp)}°
                </span>
              )}
            </>
          ) : (
            last.ram?.total && (
              <>
                <span className="fs-vitals__pct">{t('RAM')} {pct(last.ram.percent)}</span>
                <span className="fs-vitals__tanks" aria-hidden="true">
                  <span className="fs-vitals__tank" data-level={level(last.ram.percent) || undefined}>
                    <span style={{ inlineSize: `${Math.min(100, last.ram.percent)}%` }} />
                  </span>
                </span>
              </>
            )
          )}
          {spill ? (
            <span className="fs-vitals__spill">{t('PCIe spill')}</span>
          ) : model ? (
            <span className="fs-vitals__model" title={model.name}>
              {model.name.split(':')[0]}
              {model.gpu_pct < 100 && <span className="fs-vitals__of"> {model.gpu_pct}% GPU</span>}
            </span>
          ) : last.ollama?.reachable ? (
            <span className="fs-vitals__model fs-vitals__of">{t('no model')}</span>
          ) : last.ollama ? (
            <span className="fs-vitals__model" data-off="">
              {t('Ollama offline')}
            </span>
          ) : null}
        </>
      ) : (
        <span className="fs-vitals__of">{t('usage: n/a')}</span>
      )}
    </button>
  );

  return (
    <Popover trigger={trigger} align="end" className="fs-vt" testId="vitals-panel">
      <Panel d={last} intervalMs={intervalMs} busy={busy} />
    </Popover>
  );
}

/* ── the trace ───────────────────────────────────────────────────────── */

const W = 56;
const H = 18;

function Trace({ samples }: { samples: number[] }) {
  const pts = samples.length >= 2 ? samples : [samples[0] ?? 0, samples[0] ?? 0];
  const n = pts.length;
  const step = W / Math.max(1, n - 1);
  const y = (v: number) => H - 1.5 - (Math.max(0, Math.min(100, v)) / 100) * (H - 3);
  const line = pts.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `M0,${H} L${line.replace(/ /g, ' L')} L${W},${H} Z`;
  return (
    <svg className="fs-vitals__trace" viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden="true" data-note="guard-ok: a live chart drawn from data, not an icon">
      <defs>
        <linearGradient id="fs-vitals-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="currentColor" stopOpacity="0.35" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#fs-vitals-fill)" />
      <polyline points={line} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={(n - 1) * step} cy={y(pts[n - 1])} r="1.8" fill="currentColor" />
    </svg>
  );
}

/** One tank per card, as wide as the card is big. */
function Tank({ gpu }: { gpu: Gpu }) {
  const total = gpu.mem_total ?? 0;
  const p = total ? Math.max(0, Math.min(100, ((gpu.mem_used ?? 0) / total) * 100)) : 0;
  return (
    <span className="fs-vitals__tank" data-level={level(p) || undefined} style={{ flexGrow: Math.max(1, total / 1024) }} title={`#${gpu.index} ${shortGpuName(gpu.name)}`}>
      <span style={{ inlineSize: `${p}%` }} />
    </span>
  );
}

/* ── the panel ───────────────────────────────────────────────────────── */

function Meter({ value, total, level: lv }: { value: number; total: number; level?: string }) {
  const p = total ? Math.max(0, Math.min(100, (value / total) * 100)) : 0;
  return (
    <span className="fs-vt__meter" data-level={lv ?? (level(p) || undefined)}>
      <span style={{ inlineSize: `${p.toFixed(1)}%` }} />
    </span>
  );
}

function Row({ label, value, meter, title, muted, wide }: { label: string; value: string; meter?: { value: number; total: number; level?: string }; title?: string; muted?: boolean; wide?: boolean }) {
  return (
    <div className="fs-vt__row" data-wide={wide || undefined} title={title}>
      <span className="fs-vt__label">{label}</span>
      {meter ? <Meter {...meter} /> : wide ? null : <span />}
      <span className="fs-vt__val" data-muted={muted || undefined}>
        {value}
      </span>
    </div>
  );
}

function Section({ title, aside, children }: { title: string; aside?: string; children: ReactNode }) {
  return (
    <section className="fs-vt__section">
      <h3 className="fs-vt__h">
        {title}
        {aside && <span className="fs-vt__aside">{aside}</span>}
      </h3>
      {children}
    </section>
  );
}

const STATE_MARK: Record<string, string> = { ok: '●', warn: '▲', bad: '■', no_data: '·' };

function Panel({ d, intervalMs, busy }: { d: Usage | null; intervalMs: number; busy: boolean }) {
  const [spinning, setSpinning] = useState(false);
  // Radix unmounts the content when the popover closes, so the clock only
  // runs while someone is looking: keep-alive countdowns and "updated" stay honest.
  const now = useClock(true);
  return (
    <div className="fs-vt__body">
      <header className="fs-vt__head">
        <span className="fs-vt__title">
          <span className="fs-vt__dot" data-level={worstLevel(d) || undefined} data-busy={busy || undefined} />
          {t('Live usage')}
        </span>
        <span className="fs-vt__actions">
          <IconButton
            icon={RefreshCw}
            label={t('Refresh now')}
            size="sm"
            onClick={() => {
              setSpinning(true);
              void refreshUsage().finally(() => setSpinning(false));
            }}
            disabled={spinning}
          />
          <IconButton icon={EyeOff} label={t('Hide from the header (/usage on brings it back)')} size="sm" onClick={() => setUsageVisible(false)} />
        </span>
      </header>
      {!d ? (
        <p className="fs-vt__muted">{t('No usage data: the server did not answer /api/system/usage.')}</p>
      ) : (
        <>
          <HealthSection d={d} />
          <ServicesSection />
          <GpuSection d={d} />
          <OrphansSection d={d} />
          <OllamaSection d={d} />
          <SharedSection d={d} />
          <HostSection d={d} />
          <p className="fs-vt__foot">
            {t('updated {time}', { time: new Date((d.ts ?? now / 1000) * 1000).toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit', second: '2-digit' }) })}
            {' · '}
            {busy ? t('every {n}s while streaming', { n: intervalMs / 1000 }) : t('every {n}s', { n: intervalMs / 1000 })}
          </p>
        </>
      )}
    </div>
  );
}

function HealthSection({ d }: { d: Usage }) {
  const h = d.health;
  if (!h || !Array.isArray(h.components)) return null;
  const score = Number(h.score) || 0;
  const lv = score >= 60 ? '' : score >= 40 ? 'warm' : 'hot';
  return (
    <Section title={t('Health')} aside={t('{a} of {b} signals reporting', { a: h.reporting ?? 0, b: h.of ?? h.components.length })}>
      <Row label={t('Score')} meter={{ value: score, total: 100, level: lv }} value={`${score}/100 · ${h.grade ?? '—'}`} />
      {h.components.map((c) => {
        const state = c.state ?? 'no_data';
        const nodata = state === 'no_data';
        return (
          <Row
            key={c.name}
            wide
            label={c.label ?? c.name}
            title={c.why}
            muted={nodata}
            value={`${STATE_MARK[state] ?? '·'} ${nodata ? t('no data source yet') : c.value != null && c.value !== '' ? String(c.value) : '—'}`}
          />
        );
      })}
      {h.collected === false && <p className="fs-vt__muted">{t('Nothing has been measured yet: this zero is an absence, not a bad reading.')}</p>}
    </Section>
  );
}

const SERVICE_MARK: Record<ServiceState, string> = { ok: '●', degraded: '▲', down: '■', disabled: '·', unknown: '·' };

/**
 * The services around the model: is the vector store actually there, is web
 * search answering, do the endpoints list any models.
 *
 * This is the readout for the failure that never announces itself — Docker
 * closed, so RAG quietly fell back to keyword search and answers just got
 * worse. Reconnect re-establishes the ChromaDB-backed stores and re-probes,
 * which recovers that case without restarting Faustus.
 */
function ServicesSection() {
  const { health, allowed, error } = useServiceHealth(true);
  const [fixing, setFixing] = useState(false);
  const [said, setSaid] = useState<string | null>(null);

  // Not an admin: nothing to show, and nothing worth an error either.
  if (!allowed) return null;
  if (!health && !error) return null;

  const bad = health ? health.services.filter((s) => s.status === 'degraded' || s.status === 'down') : [];

  return (
    <Section
      title={t('Services')}
      aside={health ? (bad.length ? tn(bad.length, '{n} needs attention', '{n} need attention') : t('all good')) : undefined}
    >
      {error && !health && <p className="fs-vt__muted">{t('Could not read the services: {why}', { why: error })}</p>}
      {health?.services.map((s) => (
        <Row
          key={s.name}
          wide
          label={t(SERVICE_LABEL[s.name] ?? s.name)}
          title={s.detail}
          muted={s.status === 'disabled'}
          value={`${SERVICE_MARK[s.status]} ${s.detail}`}
        />
      ))}
      {bad.map(
        (s) =>
          s.hint && (
            <p key={`${s.name}-hint`} className="fs-vt__muted fs-vt__indent">
              {s.hint.text}
              {s.hint.command && <code className="fs-vt__cmd">{s.hint.command}</code>}
            </p>
          ),
      )}
      {bad.length > 0 && (
        <p className="fs-vt__act">
          <Button
            variant="ghost"
            size="sm"
            label={t('Reconnect')}
            loading={fixing}
            onClick={() => {
              setFixing(true);
              setSaid(null);
              void reconnectServices()
                .then((r) => {
                  const back = r?.recovery?.reconnected ?? [];
                  setSaid(back.length ? t('Back: {names}.', { names: back.join(', ') }) : t('Nothing came back; the detail above is the current answer.'));
                })
                .catch((err: Error) => setSaid(err.message))
                .finally(() => setFixing(false));
            }}
          />
          {said && <span className="fs-vt__muted">{said}</span>}
        </p>
      )}
    </Section>
  );
}

function CardModels({ g, gpus }: { g: Gpu; gpus: Gpu[] }) {
  const models = g.models ?? [];
  if (!models.length) return <p className="fs-vt__muted fs-vt__indent">{t('no model on this card')}</p>;
  return (
    <ul className="fs-vt__models fs-vt__indent">
      {models.map((m, i) => {
        const others = gpus.filter((o) => o !== g && o.index !== g.index && (o.models ?? []).some((x) => x.name === m.name)).map((o) => `#${o.index}`);
        return (
          <li key={`${m.name}-${i}`}>
            {m.name || '?'}
            {m.bytes != null && ` · ${gb(m.bytes)} GB`}
            {others.length > 0 && ` · ${t('split with {list}', { list: others.join(', ') })}`}
          </li>
        );
      })}
    </ul>
  );
}

function Card({ g, gpus, withModels }: { g: Gpu; gpus: Gpu[]; withModels: boolean }) {
  const multi = gpus.length > 1;
  return (
    <div className="fs-vt__card">
      <h4 className="fs-vt__card-h">
        {multi && <span className="fs-vt__idx">#{g.index}</span>}
        {(multi ? shortGpuName(g.name) : g.name) || 'GPU'}
      </h4>
      <Row label={t('Util')} meter={{ value: g.util ?? 0, total: 100 }} value={pct(g.util)} />
      <Row label={t('VRAM')} meter={{ value: g.mem_used ?? 0, total: g.mem_total ?? 1 }} value={`${mib2gb(g.mem_used)} / ${mib2gb(g.mem_total)} GB`} />
      {g.power != null && <Row label={t('Power')} meter={{ value: g.power, total: g.power_limit ?? g.power ?? 1 }} value={`${Math.round(g.power)} W${g.power_limit ? ` / ${Math.round(g.power_limit)} W` : ''}`} />}
      {g.temp != null && <Row label={t('Temp')} value={`${Math.round(g.temp)} °C`} />}
      {withModels && <CardModels g={g} gpus={gpus} />}
    </div>
  );
}

function GpuSection({ d }: { d: Usage }) {
  const gpus = d.gpu ?? [];
  if (!gpus.length) {
    return (
      <Section title="GPU">
        <p className="fs-vt__muted">
          {t('nvidia-smi unavailable')}
          {d.errors?.length ? ` — ${d.errors.join('; ')}` : ''}
        </p>
      </Section>
    );
  }
  if (!isMulti(d)) {
    return (
      <Section title="GPU">
        {gpus.map((g) => (
          <Card key={g.index} g={g} gpus={gpus} withModels={false} />
        ))}
      </Section>
    );
  }
  const pool = poolOf(d);
  return (
    <Section title={t('GPUs ({n})', { n: pool.count })} aside={t('the pool, then each card')}>
      <Row label={t('Util')} meter={{ value: pool.util ?? 0, total: 100 }} value={`${pct(pool.util)} ${t('max')}${pool.util_avg != null ? ` · ${pct(pool.util_avg)} ${t('avg')}` : ''}`} />
      <Row label={t('VRAM')} meter={{ value: pool.mem_used ?? 0, total: pool.mem_total ?? 1 }} value={`${mib2gb(pool.mem_used)} / ${mib2gb(pool.mem_total)} GB`} />
      {pool.power != null && <Row label={t('Power')} meter={{ value: pool.power, total: pool.power_limit ?? pool.power ?? 1 }} value={`${Math.round(pool.power)} W${pool.power_limit ? ` / ${Math.round(pool.power_limit)} W` : ''}`} />}
      {pool.temp != null && <Row label={t('Temp')} value={`${Math.round(pool.temp)} °C ${t('max')}`} />}
      {gpus.map((g) => (
        <Card key={g.index} g={g} gpus={gpus} withModels />
      ))}
    </Section>
  );
}

function OrphansSection({ d }: { d: Usage }) {
  const list = d.orphans ?? [];
  const [state, setState] = useState<Record<number, 'busy' | string>>({});
  if (!list.length) return null;
  const total = list.reduce((a, o) => a + (o.bytes ?? 0), 0);
  return (
    <Section title={tn(list.length, 'Orphaned runner', 'Orphaned runners')} aside={t('no Ollama server owns them')}>
      <div className="fs-vt__warn">
        {list.map((o) => (
          <div key={o.pid} className="fs-vt__orphan">
            <span>
              {o.name || t('runner')} · pid {o.pid}
              {o.bytes != null && ` · ${gb(o.bytes)} GB`}
              {o.gpus?.length ? ` · #${o.gpus.join(', #')}` : ''}
              {state[o.pid] && state[o.pid] !== 'busy' && <span className="fs-vt__err"> — {state[o.pid]}</span>}
            </span>
            <Button
              size="sm"
              variant="secondary"
              label={t('Release')}
              loading={state[o.pid] === 'busy'}
              title={t('Kill this runner and free its VRAM (it is re-checked as orphaned first)')}
              onClick={() => {
                setState((s) => ({ ...s, [o.pid]: 'busy' }));
                releaseOrphan(o.pid)
                  .then(() => setState((s) => ({ ...s, [o.pid]: '' })))
                  .catch((e: Error) => setState((s) => ({ ...s, [o.pid]: e.message })));
              }}
            />
          </div>
        ))}
        <p className="fs-vt__muted">
          {t('{size} that every other gauge counts as "other". Ollama restarted without its runners; releasing them is safe — the next request loads the model again.', { size: total ? `${gb(total)} GB` : t('VRAM') })}
        </p>
      </div>
    </Section>
  );
}

function OllamaSection({ d }: { d: Usage }) {
  const o = d.ollama;
  if (!o) {
    return (
      <Section title="Ollama">
        <p className="fs-vt__muted">{t('no data source yet')}</p>
      </Section>
    );
  }
  const models = o.models ?? [];
  return (
    <Section title="Ollama" aside={`${(o.base ?? '').replace(/^https?:\/\//, '')}${o.reachable ? '' : ` · ${t('unreachable')}`}`}>
      {models.length ? (
        models.map((m) => (
          <div key={m.name} className="fs-vt__card">
            <h4 className="fs-vt__card-h">
              {m.name}
              <span className="fs-vt__aside">
                {m.parameter_size ?? ''} {m.quantization ?? ''}
              </span>
            </h4>
            <Row label="GPU/CPU" meter={{ value: m.gpu_pct, total: 100, level: m.gpu_pct < 100 ? 'warm' : '' }} value={`${m.gpu_pct}% GPU / ${m.cpu_pct}% CPU`} />
            <Row label={t('Size')} value={t('{a} GB ({b} GB in VRAM)', { a: gb(m.size), b: gb(m.size_vram) })} />
            {m.placement != null && <Row label={t('Placement')} value={placementText(m, d)} />}
            <Row label={t('Context')} value={t('{n} tokens', { n: fmtCtx(m.context_length) })} />
            <Row label={t('Keep-alive')} value={untilText(m.expires_at) || '—'} />
          </div>
        ))
      ) : (
        <p className="fs-vt__muted">{o.reachable ? t('No model loaded (ollama ps is empty).') : t('Cannot reach Ollama.')}</p>
      )}
    </Section>
  );
}

function SharedSection({ d }: { d: Usage }) {
  const gm = d.gpu_mem;
  if (!gm?.supported) return null;
  const om = gm.ollama ?? {};
  const frac = Math.round((om.shared_fraction ?? 0) * 100);
  const steps = d.sysmem_fallback?.steps ?? [];
  return (
    <Section title={t('Shared GPU memory')} aside={t('system RAM, over PCIe')}>
      <Row label={t('Runner')} value={t('{mb} MB · {p}% of its GPU memory', { mb: mbytes(om.shared), p: frac })} />
      <Row label={t('Everything')} value={`${mbytes(gm.total_shared)} MB`} />
      {om.spilling ? (
        <div className="fs-vt__warn">
          <p>
            <b>{t('Weights are paging over PCIe.')}</b>{' '}
            {t('The card ran out of room and the CUDA driver put part of the model in system memory instead of failing, so it is being read at ~25 GB/s instead of ~500. Nothing else shows this: VRAM, GPU% and ollama ps all still look healthy.')}
          </p>
          {steps.length > 0 && (
            <ol className="fs-vt__steps">
              {steps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          )}
          <p className="fs-vt__muted">{t('Or shrink the context in the model settings so the model fits.')}</p>
        </div>
      ) : (
        <p className="fs-vt__muted">{t('A CUDA process always parks a few hundred MB here — that is not a spill.')}</p>
      )}
    </Section>
  );
}

function HostSection({ d }: { d: Usage }) {
  if (d.ram?.total) {
    return (
      <Section title={t('Host')}>
        <Row label={t('RAM')} meter={{ value: d.ram.used, total: d.ram.total }} value={`${gb(d.ram.used)} / ${gb(d.ram.total)} GB (${pct(d.ram.percent)})`} />
        {d.cpu?.percent != null && <Row label="CPU" meter={{ value: d.cpu.percent, total: 100 }} value={`${pct(d.cpu.percent)}${d.cpu.count ? ` · ${tn(d.cpu.count, '{n} thread', '{n} threads')}` : ''}`} />}
      </Section>
    );
  }
  const why = (d.errors ?? []).filter((e) => String(e).startsWith('psutil'));
  return (
    <Section title={t('Host')}>
      <p className="fs-vt__muted">
        {t('no data source yet')}
        {why.length ? ` — ${why.join('; ')}` : ''}
      </p>
    </Section>
  );
}
