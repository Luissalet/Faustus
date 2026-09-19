/**
 * Chart fence spec: what a model writes into a ```chart``` (or
 * ```faustus-chart```) fenced block, and the pure validator that turns raw
 * fence text into a spec `ChartBlock.tsx` can draw — or a reason it can't.
 *
 * Kept deliberately small and JSON-shaped (see docs/ui/charts.md) so a
 * model can produce it without a schema round-trip, and deliberately
 * capped so a malformed or hostile block can never blow up the DOM or the
 * layout: invalid input is always `{ok:false}`, never a throw.
 */

export type ChartType = 'bar' | 'line' | 'pie' | 'area';

export interface ChartSeries {
  name: string;
  values: number[];
}

export interface ChartSpec {
  type: ChartType;
  title?: string;
  x?: string[];
  series: ChartSeries[];
  unit?: string;
  stacked?: boolean;
}

export type ChartValidation =
  | { ok: true; spec: ChartSpec }
  | { ok: false; reason: string };

/** Caps — kept in one place so docs/tests/renderer all cite the same numbers. */
export const CHART_MAX_SERIES = 12;
export const CHART_MAX_POINTS = 200;
export const CHART_MAX_PIE_SLICES = 40;
export const CHART_MAX_STRING_LEN = 60;

const TYPES: ChartType[] = ['bar', 'line', 'pie', 'area'];

function clipString(value: unknown, fallback: string): string {
  const s = typeof value === 'string' ? value : fallback;
  return s.length > CHART_MAX_STRING_LEN ? s.slice(0, CHART_MAX_STRING_LEN) : s;
}

function isFiniteNumber(n: unknown): n is number {
  return typeof n === 'number' && Number.isFinite(n);
}

/**
 * Parses and validates one chart fence body. Never throws: bad JSON, a
 * missing field, a wrong type, or a spec over any cap all come back as
 * `{ok:false, reason}` so the caller can fall back to a plain code block.
 */
export function parseChartSpec(raw: string): ChartValidation {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return { ok: false, reason: 'not valid JSON' };
  }
  return validateChartSpec(data);
}

export function validateChartSpec(data: unknown): ChartValidation {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    return { ok: false, reason: 'chart spec must be a JSON object' };
  }
  const obj = data as Record<string, unknown>;

  const type = obj.type;
  if (typeof type !== 'string' || !TYPES.includes(type as ChartType)) {
    return { ok: false, reason: 'type must be one of bar, line, pie, area' };
  }

  const rawSeries = obj.series;
  if (!Array.isArray(rawSeries) || rawSeries.length === 0) {
    return { ok: false, reason: 'series must be a non-empty array' };
  }
  if (rawSeries.length > CHART_MAX_SERIES) {
    return { ok: false, reason: `at most ${CHART_MAX_SERIES} series` };
  }

  const series: ChartSeries[] = [];
  for (const raw of rawSeries) {
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
      return { ok: false, reason: 'each series must be an object' };
    }
    const s = raw as Record<string, unknown>;
    const values = s.values;
    if (!Array.isArray(values) || values.length === 0) {
      return { ok: false, reason: 'each series needs a non-empty values array' };
    }
    if (values.length > CHART_MAX_POINTS) {
      return { ok: false, reason: `at most ${CHART_MAX_POINTS} points per series` };
    }
    const nums: number[] = [];
    for (const v of values) {
      if (!isFiniteNumber(v)) return { ok: false, reason: 'series values must be finite numbers' };
      nums.push(v);
    }
    series.push({ name: clipString(s.name, `series ${series.length + 1}`), values: nums });
  }

  // Every series must share a length — a chart with ragged series has no
  // honest way to line up x labels or a legend.
  const len = series[0].values.length;
  if (!series.every((s) => s.values.length === len)) {
    return { ok: false, reason: 'all series must have the same number of values' };
  }

  let x: string[] | undefined;
  if (obj.x !== undefined) {
    if (!Array.isArray(obj.x)) return { ok: false, reason: 'x must be an array of labels' };
    if (obj.x.length !== len) return { ok: false, reason: 'x must have one label per value' };
    x = obj.x.map((v) => clipString(v, ''));
  }

  if (type === 'pie') {
    if (series[0].values.length > CHART_MAX_PIE_SLICES) {
      return { ok: false, reason: `at most ${CHART_MAX_PIE_SLICES} pie slices` };
    }
    if (series[0].values.some((v) => v < 0)) {
      return { ok: false, reason: 'pie values must not be negative' };
    }
  }

  let title: string | undefined;
  if (obj.title !== undefined) {
    if (typeof obj.title !== 'string') return { ok: false, reason: 'title must be a string' };
    title = clipString(obj.title, '');
  }

  let unit: string | undefined;
  if (obj.unit !== undefined) {
    if (typeof obj.unit !== 'string') return { ok: false, reason: 'unit must be a string' };
    unit = clipString(obj.unit, '');
  }

  let stacked: boolean | undefined;
  if (obj.stacked !== undefined) {
    if (typeof obj.stacked !== 'boolean') return { ok: false, reason: 'stacked must be a boolean' };
    if (type !== 'bar' && type !== 'area') return { ok: false, reason: 'stacked only applies to bar/area' };
    stacked = obj.stacked;
  }

  const spec: ChartSpec = { type: type as ChartType, series };
  if (title !== undefined) spec.title = title;
  if (x !== undefined) spec.x = x;
  if (unit !== undefined) spec.unit = unit;
  if (stacked !== undefined) spec.stacked = stacked;
  return { ok: true, spec };
}

/** The fence languages that trigger chart parsing. */
export const CHART_FENCE_LANGS = new Set(['chart', 'faustus-chart']);
