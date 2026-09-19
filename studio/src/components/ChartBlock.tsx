import { useId, useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { t } from '../i18n';
import type { ChartSpec } from '../lib/chartSpec';

export interface ChartBlockProps {
  spec: ChartSpec;
}

/**
 * A chart fence (```chart``` / ```faustus-chart```, see docs/ui/charts.md
 * and lib/chartSpec.ts), drawn as a pure inline SVG — no charting library,
 * same "no new dependency" posture as components/MermaidView.tsx. Colors
 * come from the theme tokens (tokens.css) so it works in light and dark
 * with no work of its own; axes use a classic "nice numbers" tick step so
 * gridlines land on round values instead of whatever the data happens to be.
 */

/** The theme tokens cycled across series, in order. Five distinct hues
 * already used across Studio (brand/info/success/warning/danger) plus a
 * `color-mix` second pass for anything past that — never a literal color. */
const PALETTE = [
  'var(--fs-brand)',
  'var(--fs-info)',
  'var(--fs-success)',
  'var(--fs-warning)',
  'var(--fs-danger)',
];

function colorFor(i: number): string {
  const base = PALETTE[i % PALETTE.length];
  const pass = Math.floor(i / PALETTE.length);
  if (pass === 0) return base;
  return `color-mix(in srgb, ${base} ${Math.max(35, 70 - pass * 15)}%, var(--fs-text-3))`;
}

/** "Nice numbers" axis step (Heckbert): a tick step that lands on 1/2/5 * 10^n
 * so gridlines read as round numbers rather than the data's own noise. */
function niceStep(range: number, targetTicks: number): number {
  if (range <= 0) return 1;
  const raw = range / Math.max(1, targetTicks);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return step * mag;
}

function niceTicks(min: number, max: number, targetTicks = 5): number[] {
  const lo = Math.min(0, min);
  const hi = Math.max(0, max);
  const step = niceStep(hi - lo || 1, targetTicks);
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= end + step / 2; v += step) ticks.push(Math.round(v * 1e9) / 1e9);
  return ticks;
}

function fmtNumber(n: number): string {
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(2).replace(/\.?0+$/, '');
}

const W = 640;
const H = 320;
const PAD_L = 52;
const PAD_R = 16;
const PAD_T = 20;
const PAD_B = 36;

function DataTable({ spec }: { spec: ChartSpec }) {
  const labels = spec.x ?? spec.series[0].values.map((_, i) => String(i + 1));
  return (
    <div className="fs-chart__tablewrap" role="region" aria-label={t('Chart data')} tabIndex={0}>
      <table className="fs-chart__table">
        <thead>
          <tr>
            <th scope="col">{spec.type === 'pie' ? t('Slice') : t('X')}</th>
            {spec.series.map((s) => (
              <th key={s.name} scope="col">{s.name}{spec.unit ? ` (${spec.unit})` : ''}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {labels.map((label, i) => (
            <tr key={i}>
              <td>{label}</td>
              {spec.series.map((s) => (
                <td key={s.name}>{fmtNumber(s.values[i])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AxesChart({ spec, uid }: { spec: ChartSpec; uid: string }) {
  const n = spec.series[0].values.length;
  const labels = spec.x ?? Array.from({ length: n }, (_, i) => String(i + 1));
  const stacked = !!spec.stacked && (spec.type === 'bar' || spec.type === 'area');

  const perIndexMax = Array.from({ length: n }, (_, i) =>
    stacked
      ? spec.series.reduce((sum, s) => sum + Math.max(0, s.values[i]), 0)
      : Math.max(...spec.series.map((s) => s.values[i])),
  );
  const perIndexMin = Array.from({ length: n }, (_, i) =>
    stacked ? Math.min(0, ...spec.series.map((s) => Math.min(0, s.values[i])))
      : Math.min(...spec.series.map((s) => s.values[i])),
  );
  const dataMax = Math.max(0, ...perIndexMax);
  const dataMin = Math.min(0, ...perIndexMin);
  const ticks = niceTicks(dataMin, dataMax);
  const axisMin = ticks[0];
  const axisMax = ticks[ticks.length - 1];
  const span = axisMax - axisMin || 1;

  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;
  const yFor = (v: number) => PAD_T + plotH - ((v - axisMin) / span) * plotH;
  const zeroY = yFor(0);

  const slotW = plotW / n;
  const xFor = (i: number) => PAD_L + slotW * (i + 0.5);

  const isBar = spec.type === 'bar';
  const barGroupW = slotW * 0.7;
  const barW = isBar ? barGroupW / (stacked ? 1 : spec.series.length) : 0;

  // Cumulative top/bottom per series/index for stacked line & area charts,
  // computed once up front — nothing below mutates state while rendering.
  const cumTop: number[][] = spec.series.map(() => new Array(n).fill(0));
  const cumBottom: number[][] = spec.series.map(() => new Array(n).fill(0));
  if (!isBar) {
    for (let i = 0; i < n; i += 1) {
      let running = 0;
      for (let si = 0; si < spec.series.length; si += 1) {
        const v = spec.series[si].values[i];
        if (stacked) {
          cumBottom[si][i] = running;
          running += v;
          cumTop[si][i] = running;
        } else {
          cumBottom[si][i] = 0;
          cumTop[si][i] = v;
        }
      }
    }
  }

  const lineD = (si: number): string =>
    cumTop[si].map((top, i) => `${i === 0 ? 'M' : 'L'}${xFor(i).toFixed(1)},${yFor(top).toFixed(1)}`).join(' ');

  const areaD = (si: number): string => {
    const top = cumTop[si].map((v, i) => `${i === 0 ? 'M' : 'L'}${xFor(i).toFixed(1)},${yFor(v).toFixed(1)}`).join(' ');
    const base = cumBottom[si]
      .map((v, i) => `L${xFor(i).toFixed(1)},${(stacked ? yFor(v) : zeroY).toFixed(1)}`)
      .reverse()
      .join(' ');
    return `${top} ${base} Z`;
  };

  return (
    <svg
      className="fs-chart__svg"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-labelledby={`${uid}-title`}
      aria-describedby={`${uid}-desc`}
    >
      <title id={`${uid}-title`}>{spec.title || t('Chart')}</title>
      <desc id={`${uid}-desc`}>
        {t('{type} chart with {n} series over {m} points.')
          .replace('{type}', spec.type)
          .replace('{n}', String(spec.series.length))
          .replace('{m}', String(n))}
      </desc>

      {/* Gridlines + y ticks */}
      <g className="fs-chart__grid">
        {ticks.map((tv) => (
          <g key={tv}>
            <line x1={PAD_L} x2={W - PAD_R} y1={yFor(tv)} y2={yFor(tv)} />
            <text x={PAD_L - 8} y={yFor(tv)} textAnchor="end" dominantBaseline="middle">
              {fmtNumber(tv)}{spec.unit ? ` ${spec.unit}` : ''}
            </text>
          </g>
        ))}
      </g>

      {/* Zero / base axis */}
      <line className="fs-chart__axis" x1={PAD_L} x2={W - PAD_R} y1={zeroY} y2={zeroY} />
      <line className="fs-chart__axis" x1={PAD_L} x2={PAD_L} y1={PAD_T} y2={H - PAD_B} />

      {/* X labels */}
      <g className="fs-chart__xlabels">
        {labels.map((label, i) => (
          <text key={i} x={xFor(i)} y={H - PAD_B + 16} textAnchor="middle">
            {label.length > 10 ? `${label.slice(0, 9)}…` : label}
          </text>
        ))}
      </g>

      {/* Series */}
      {isBar
        ? (() => {
            const barStackAcc = new Array(n).fill(0);
            return spec.series.map((s, si) => {
              if (!stacked) barStackAcc.fill(0);
              return s.values.map((v, i) => {
                let top: number;
                let bottom: number;
                if (stacked) {
                  bottom = barStackAcc[i];
                  barStackAcc[i] += v;
                  top = barStackAcc[i];
                } else {
                  top = Math.max(0, v);
                  bottom = Math.min(0, v);
                }
                const x = stacked ? PAD_L + slotW * i + (slotW - barGroupW) / 2 : xFor(i) - barGroupW / 2 + si * barW;
                const y = yFor(Math.max(top, bottom));
                const h = Math.max(0.5, Math.abs(yFor(top) - yFor(bottom)));
                return (
                  <rect
                    key={`${si}-${i}`}
                    x={x}
                    y={y}
                    width={Math.max(1, barW - 2)}
                    height={h}
                    fill={colorFor(si)}
                    rx={1.5}
                  >
                    <title>{`${s.name} · ${labels[i]}: ${fmtNumber(v)}${spec.unit ? ` ${spec.unit}` : ''}`}</title>
                  </rect>
                );
              });
            });
          })()
        : spec.series.map((s, si) => (
            <g key={si}>
              {spec.type === 'area' && (
                <path d={areaD(si)} fill={colorFor(si)} fillOpacity={0.28} stroke="none" />
              )}
              <path
                d={lineD(si)}
                fill="none"
                stroke={colorFor(si)}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            </g>
          ))}

      {/* Hover targets with tooltips for line/area points (kept separate from
          the path so <title> attaches per-point, not once for the whole line). */}
      {!isBar &&
        spec.series.map((s, si) =>
          s.values.map((v, i) => (
            <circle
              key={`${si}-${i}`}
              className="fs-chart__point"
              cx={xFor(i)}
              cy={yFor(cumTop[si][i])}
              r={3}
              fill={colorFor(si)}
            >
              <title>{`${s.name} · ${labels[i]}: ${fmtNumber(v)}${spec.unit ? ` ${spec.unit}` : ''}`}</title>
            </circle>
          )),
        )}
    </svg>
  );
}

function PieChart({ spec, uid }: { spec: ChartSpec; uid: string }) {
  const values = spec.series[0].values;
  const labels = spec.x ?? values.map((_, i) => String(i + 1));
  const total = values.reduce((a, b) => a + b, 0);
  const cx = W / 2 - 70;
  const cy = H / 2;
  const r = Math.min(H, W / 2) / 2 - 14;

  let angle = -Math.PI / 2;
  const slices = values.map((v, i) => {
    const frac = total > 0 ? v / total : 0;
    const start = angle;
    const sweep = frac * Math.PI * 2;
    angle += sweep;
    const end = angle;
    const large = sweep > Math.PI ? 1 : 0;
    const x1 = cx + r * Math.cos(start);
    const y1 = cy + r * Math.sin(start);
    const x2 = cx + r * Math.cos(end);
    const y2 = cy + r * Math.sin(end);
    const d = total > 0
      ? `M${cx},${cy} L${x1.toFixed(2)},${y1.toFixed(2)} A${r},${r} 0 ${large} 1 ${x2.toFixed(2)},${y2.toFixed(2)} Z`
      : '';
    const pct = total > 0 ? (frac * 100).toFixed(1) : '0.0';
    return { d, i, pct, label: labels[i], v };
  });

  return (
    <svg
      className="fs-chart__svg"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-labelledby={`${uid}-title`}
      aria-describedby={`${uid}-desc`}
    >
      <title id={`${uid}-title`}>{spec.title || t('Chart')}</title>
      <desc id={`${uid}-desc`}>
        {t('Pie chart with {n} slices.').replace('{n}', String(values.length))}
      </desc>
      {slices.map((s) => (
        <path key={s.i} d={s.d} fill={colorFor(s.i)}>
          <title>{`${s.label}: ${fmtNumber(s.v)}${spec.unit ? ` ${spec.unit}` : ''} (${s.pct}%)`}</title>
        </path>
      ))}
      <g className="fs-chart__legend" transform={`translate(${W - 150}, ${PAD_T})`}>
        {slices.slice(0, 14).map((s, row) => (
          <g key={s.i} transform={`translate(0, ${row * 18})`}>
            <rect width={10} height={10} rx={2} fill={colorFor(s.i)} />
            <text x={16} y={9}>
              {s.label.length > 16 ? `${s.label.slice(0, 15)}…` : s.label} · {s.pct}%
            </text>
          </g>
        ))}
      </g>
    </svg>
  );
}

function Legend({ spec }: { spec: ChartSpec }) {
  if (spec.type === 'pie' || spec.series.length < 2) return null;
  return (
    <div className="fs-chart__legend2">
      {spec.series.map((s, i) => (
        <span key={s.name} className="fs-chart__legenditem">
          <span className="fs-chart__swatch" style={{ background: colorFor(i) }} aria-hidden="true" />
          {s.name}
        </span>
      ))}
    </div>
  );
}

export function ChartBlock({ spec }: ChartBlockProps) {
  const uid = useId().replace(/:/g, 'x');
  const [showData, setShowData] = useState(false);
  return (
    <div className="fs-chart" data-testid="chart-block">
      {spec.title && <div className="fs-chart__title">{spec.title}</div>}
      {spec.type === 'pie' ? <PieChart spec={spec} uid={uid} /> : <AxesChart spec={spec} uid={uid} />}
      <Legend spec={spec} />
      <button
        type="button"
        className="fs-chart__toggle"
        aria-expanded={showData}
        onClick={() => setShowData((v) => !v)}
      >
        {showData ? <ChevronUp size={13} aria-hidden="true" /> : <ChevronDown size={13} aria-hidden="true" />}
        {showData ? t('Hide data') : t('Show data')}
      </button>
      {showData && <DataTable spec={spec} />}
    </div>
  );
}
