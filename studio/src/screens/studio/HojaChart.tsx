import { useMemo } from 'react';
import { compactNumber as compact, niceTicks } from '../../lib/hojaModel';
import { getLang } from '../../i18n';

const compactNumber = (n: number): string => (getLang() === 'es' ? compact(n).replace('.', ',') : compact(n));
import type { HojaChartData } from '../../adapters/surfaces';

/**
 * A chart of a sheet range drawn as SVG (line, bar, pie, scatter). The data is the server's (`chart_data`): the
 * categories and the series values already read from the cells, so the chart follows the cells as they change.
 * Colours are the categorical tokens in hoja.css, which have a light and a dark set.
 */
const PAD = { l: 44, r: 12, t: 30, b: 34 };
const SERIES = 8;

function pathFor(points: [number, number][]): string {
  return points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
}

export default function HojaChart({ chart }: { chart: HojaChartData }) {
  const { w, h } = chart;
  const model = useMemo(() => {
    const series = chart.series.map((s) => ({ name: s.name, values: s.values }));
    const nums = series.flatMap((s) => s.values).filter((v): v is number => typeof v === 'number');
    if (chart.type === 'scatter') {
      const xs = chart.categories.map((c) => Number(c));
      const xv = xs.filter((x) => Number.isFinite(x));
      return { series, ymin: Math.min(0, ...nums), ymax: Math.max(...nums, 1), xmin: Math.min(...xv, 0), xmax: Math.max(...xv, 1) };
    }
    return { series, ymin: Math.min(0, ...nums, 0), ymax: Math.max(...nums, 1), xmin: 0, xmax: 1 };
  }, [chart]);
  const iw = w - PAD.l - PAD.r, ih = h - PAD.t - PAD.b;
  const empty = model.series.every((s) => s.values.every((v) => v == null));

  const body = () => {
    if (chart.type === 'pie') {
      const s = model.series[0];
      const vals = (s?.values ?? []).map((v) => (typeof v === 'number' && v > 0 ? v : 0));
      const total = vals.reduce((a, b) => a + b, 0);
      if (!total) return null;
      const cx = w / 2 - 60, cy = (h + PAD.t - 10) / 2, r = Math.min(ih, w - 150) / 2;
      let a0 = -Math.PI / 2;
      return (
        <g>
          {vals.map((v, i) => {
            if (!v) return null;
            const a1 = a0 + (v / total) * Math.PI * 2;
            const large = a1 - a0 > Math.PI ? 1 : 0;
            const d = vals.filter(Boolean).length === 1
              ? `M${cx - r},${cy} a${r},${r} 0 1,0 ${2 * r},0 a${r},${r} 0 1,0 ${-2 * r},0`
              : `M${cx},${cy} L${cx + r * Math.cos(a0)},${cy + r * Math.sin(a0)} A${r},${r} 0 ${large} 1 ${cx + r * Math.cos(a1)},${cy + r * Math.sin(a1)} Z`;
            a0 = a1;
            return <path key={i} d={d} className="fs-hj-slice" style={{ fill: `var(--hj-s${i % SERIES})` }}><title>{`${chart.categories[i] ?? i + 1}: ${v}`}</title></path>;
          })}
          {vals.map((v, i) => (
            <g key={'l' + i} transform={`translate(${w - 118},${PAD.t + 4 + i * 16})`}>
              <rect width="10" height="10" rx="2" style={{ fill: `var(--hj-s${i % SERIES})` }} />
              <text x="15" y="9" className="fs-hj-chart-t">{`${(chart.categories[i] ?? String(i + 1)).slice(0, 12)} ${Math.round((v / total) * 100)}%`}</text>
            </g>
          ))}
        </g>
      );
    }
    const ticks = niceTicks(model.ymin, model.ymax);
    const lo = ticks[0], hi = ticks[ticks.length - 1] || 1;
    const y = (v: number) => PAD.t + ih - ((v - lo) / (hi - lo || 1)) * ih;
    const n = Math.max(chart.categories.length, ...model.series.map((s) => s.values.length), 1);
    const axes = (
      <g>
        {ticks.map((tk) => (
          <g key={tk}>
            <line x1={PAD.l} x2={w - PAD.r} y1={y(tk)} y2={y(tk)} className="fs-hj-chart-grid" />
            <text x={PAD.l - 6} y={y(tk) + 3} textAnchor="end" className="fs-hj-chart-t">{compactNumber(tk)}</text>
          </g>
        ))}
      </g>
    );
    if (chart.type === 'scatter') {
      const xs = chart.categories.map((c) => Number(c));
      const xt = niceTicks(model.xmin, model.xmax);
      const xlo = xt[0], xhi = xt[xt.length - 1] || 1;
      const x = (v: number) => PAD.l + ((v - xlo) / (xhi - xlo || 1)) * iw;
      return (
        <g>
          {axes}
          {xt.map((tk) => <text key={'x' + tk} x={x(tk)} y={h - PAD.b + 14} textAnchor="middle" className="fs-hj-chart-t">{compactNumber(tk)}</text>)}
          {model.series.map((s, si) => s.values.map((v, i) => (typeof v === 'number' && Number.isFinite(xs[i])
            ? <circle key={`${si}-${i}`} cx={x(xs[i])} cy={y(v)} r="3.5" style={{ fill: `var(--hj-s${si % SERIES})` }} className="fs-hj-dot" /> : null)))}
        </g>
      );
    }
    const band = iw / n;
    const labelEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 56))));
    const labels = chart.categories.slice(0, n).map((c, i) => (i % labelEvery === 0
      ? <text key={'c' + i} x={PAD.l + band * (i + 0.5)} y={h - PAD.b + 14} textAnchor="middle" className="fs-hj-chart-t">{String(c).slice(0, 10)}</text> : null));
    if (chart.type === 'bar') {
      const k = Math.max(1, model.series.length);
      const bw = Math.min(40, (band * 0.76) / k);
      return (
        <g>
          {axes}{labels}
          {model.series.map((s, si) => s.values.map((v, i) => {
            if (typeof v !== 'number') return null;
            const x0 = PAD.l + band * i + (band - bw * k) / 2 + bw * si;
            const y0 = y(Math.max(v, 0)), y1 = y(Math.min(v, 0));
            return <rect key={`${si}-${i}`} x={x0} y={y0} width={Math.max(1, bw - 1)} height={Math.max(0.5, y1 - y0)} rx="2" style={{ fill: `var(--hj-s${si % SERIES})` }}><title>{`${chart.categories[i] ?? i + 1}: ${v}`}</title></rect>;
          }))}
        </g>
      );
    }
    return (
      <g>
        {axes}{labels}
        {model.series.map((s, si) => {
          const pts: [number, number][] = [];
          s.values.forEach((v, i) => { if (typeof v === 'number') pts.push([PAD.l + band * (i + 0.5), y(v)]); });
          return (
            <g key={si}>
              <path d={pathFor(pts)} className="fs-hj-line" style={{ stroke: `var(--hj-s${si % SERIES})` }} />
              {pts.length <= 40 && pts.map(([px, py], i) => <circle key={i} cx={px} cy={py} r="3" style={{ fill: `var(--hj-s${si % SERIES})` }} className="fs-hj-dot" />)}
            </g>
          );
        })}
      </g>
    );
  };

  return (
    <svg className="fs-hj-chart-svg" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={chart.title || chart.type} data-chart-type={chart.type}>
      <rect x="0.5" y="0.5" width={w - 1} height={h - 1} rx="8" className="fs-hj-chart-bg" />
      {chart.title && <text x={PAD.l} y="19" className="fs-hj-chart-title">{chart.title}</text>}
      {empty ? <text x={w / 2} y={h / 2} textAnchor="middle" className="fs-hj-chart-t">…</text> : body()}
      {chart.type !== 'pie' && model.series.length > 1 && (
        <g transform={`translate(${w - PAD.r - model.series.length * 70},12)`}>
          {model.series.map((s, si) => (
            <g key={si} transform={`translate(${si * 70},0)`}>
              <rect width="8" height="8" rx="2" y="-6" style={{ fill: `var(--hj-s${si % SERIES})` }} />
              <text x="12" y="2" className="fs-hj-chart-t">{(s.name ?? `#${si + 1}`).slice(0, 8)}</text>
            </g>
          ))}
        </g>
      )}
    </svg>
  );
}
