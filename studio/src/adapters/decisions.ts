/** Calibrated thresholds for typed decisions (server side: `src/decision_calibration.py`), REST under /api/typed-decision. */

export interface CalibrationSample { confidence: number; correct: boolean }

export interface CalibrationResult {
  ok: boolean;
  threshold: number | null;
  accepted: number;
  errors: number;
  upperBound: number | null;
  coverage: number;
  samples: number;
  maxError: number;
  delta: number;
  auditRate: number;
  reason: string;
  saved?: { ok: boolean; reason?: string };
}

export interface StoredCalibration {
  field: string;
  threshold: number;
  upperBound: number | null;
  coverage: number;
  samples: number;
  maxError: number;
  delta: number;
  calibratedAt: number;
}

function resultFrom(j: Record<string, unknown>): CalibrationResult {
  const num = (v: unknown, d = 0) => (typeof v === 'number' && Number.isFinite(v) ? v : d);
  return {
    ok: j.ok === true,
    threshold: typeof j.threshold === 'number' ? j.threshold : null,
    accepted: num(j.accepted), errors: num(j.errors),
    upperBound: typeof j.upper_bound === 'number' ? j.upper_bound : null,
    coverage: num(j.coverage), samples: num(j.samples),
    maxError: num(j.max_error, 0.05), delta: num(j.delta, 0.05), auditRate: num(j.audit_rate, 0.02),
    reason: typeof j.reason === 'string' ? j.reason : '',
    saved: j.saved && typeof j.saved === 'object' ? (j.saved as { ok: boolean; reason?: string }) : undefined,
  };
}

/** Parse "confidence,correct" lines (correct: 1/0/true/false/yes/no, ok/wrong). Bad lines are skipped. */
export function parseSamples(text: string): CalibrationSample[] {
  const out: CalibrationSample[] = [];
  for (const line of text.split(/\r?\n/)) {
    // A decimal comma ("0,93 1", "0,93;1") must not be read as a separator: with `;` or a tab the commas
    // are decimals, a single "0.93, 1" is comma-separated, anything else splits on blanks.
    const trimmed = line.trim();
    const parts = (/[;\t]/.test(trimmed) ? trimmed.split(/\s*[;\t]\s*/)
      : /^[\d.]+\s*,\s*\S+$/.test(trimmed) ? trimmed.split(/\s*,\s*/)
      : trimmed.split(/\s+/)).filter(Boolean);
    if (parts.length < 2) continue;
    const confidence = Number(parts[0].replace(',', '.'));
    const flag = parts[1].toLowerCase();
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) continue;
    if (['1', 'true', 'yes', 'ok', 'si', 'sí', 'correct'].includes(flag)) out.push({ confidence, correct: true });
    else if (['0', 'false', 'no', 'wrong', 'error'].includes(flag)) out.push({ confidence, correct: false });
  }
  return out;
}

export async function calibrateDecision(body: { field: string; samples: CalibrationSample[]; maxError: number; delta: number; apply: boolean }): Promise<CalibrationResult> {
  const r = await fetch('/api/typed-decision/calibrate', {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ field: body.field, samples: body.samples, max_error: body.maxError, delta: body.delta, apply: body.apply }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return resultFrom((await r.json()) as Record<string, unknown>);
}

export async function loadCalibrations(): Promise<StoredCalibration[]> {
  const r = await fetch('/api/typed-decision/calibration', { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const j = (await r.json()) as { calibrations?: Record<string, Record<string, unknown>> };
  return Object.entries(j.calibrations ?? {}).map(([field, e]) => ({
    field, threshold: Number(e.threshold), upperBound: typeof e.upper_bound === 'number' ? e.upper_bound : null,
    coverage: Number(e.coverage) || 0, samples: Number(e.samples) || 0, maxError: Number(e.max_error) || 0.05,
    delta: Number(e.delta) || 0.05, calibratedAt: Number(e.calibrated_at) || 0,
  }));
}

export async function forgetCalibration(field: string): Promise<void> {
  const r = await fetch(`/api/typed-decision/calibration/${encodeURIComponent(field)}`, { method: 'DELETE', credentials: 'same-origin' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
}
