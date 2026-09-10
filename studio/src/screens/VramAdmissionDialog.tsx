import { useEffect, useState } from 'react';
import { Button, Dialog } from '../components';
import { fmtGb } from '../adapters/localModels';
import { resolveAdmission, type AdmissionAction, type VramBlocked } from '../adapters/vramAdmission';
import { t, tn } from '../i18n';

/**
 * "No room in VRAM — unload which?" (OBJ-1).
 *
 * A loader wants a model that does not fit next to what is resident. Instead
 * of loading it anyway (Ollama would, with layers on the CPU and weights paged
 * over PCIe, ten times slower and silent about it) it asks. This is the
 * question: what is inside and how much each holds, what the new one needs,
 * tick what to unload. The new model loads only once the ticked ones are gone.
 *
 * The figures are honest about what they are: a footprint we have measured
 * (weights + KV cache) says so; one we have not is a floor, and says so too.
 */
export function VramAdmissionDialog({ blocked, onDone, say, onDecide }: {
  blocked: VramBlocked | null;
  onDone: () => void;
  say?: (text: string, tone?: 'ok' | 'warn') => void;
  /**
   * Where the answer goes. By default to the loader's ticket
   * (`/api/local-models/admission/{ticket}`, a run waiting on the server);
   * the Load button in Settings has no waiting loader and answers by doing
   * the unloads itself, so it passes its own handler.
   */
  onDecide?: (action: AdmissionAction, names: string[]) => Promise<void>;
}) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<AdmissionAction | null>(null);

  // A fresh question comes with its own suggestion ticked: the smallest set
  // of residents, biggest first, that frees what is missing.
  useEffect(() => {
    setPicked(new Set(blocked?.suggestion ?? []));
    setBusy(null);
  }, [blocked?.ticket]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!blocked) return null;

  const frees = blocked.residents.filter((r) => picked.has(r.name)).reduce((n, r) => n + r.inVramBytes, 0);
  const enough = frees >= blocked.shortfallBytes;

  const decide = async (action: AdmissionAction) => {
    setBusy(action);
    try {
      const names = action === 'unload' ? [...picked] : [];
      if (onDecide) await onDecide(action, names);
      else await resolveAdmission(blocked.ticket, action, names);
      onDone();
    } catch (err) {
      setBusy(null);
      say?.((err as Error).message || t('The answer did not reach the server.'), 'warn');
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => { if (!open && !busy) void decide('cancel'); }}
      title={t('No room in VRAM for {model}', { model: blocked.model })}
      testId="vram-admission"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel the load')} disabled={busy !== null} onClick={() => void decide('cancel')} />
          <span className="fs-spacer" />
          <Button variant={blocked.forcedLoadDangerous ? 'danger' : 'secondary'} size="sm" label={blocked.forcedLoadDangerous ? t('Load anyway (dangerous)') : t('Load anyway')} loading={busy === 'proceed'} disabled={busy !== null && busy !== 'proceed'} title={t('It will spill to CPU and PCIe: much slower, and it can take the machine down if memory runs out.')} onClick={() => void decide('proceed')} />
          <Button variant="primary" size="sm" label={t('Unload and continue')} loading={busy === 'unload'} disabled={picked.size === 0 || (busy !== null && busy !== 'unload')} testId="vram-admission-unload" onClick={() => void decide('unload')} />
        </>
      }
    >
      <div className="fs-dialog__body fs-vram">
        <p>
          {blocked.measured
            ? t('{model} needs {need} — {size} of weights plus {kv} of KV cache — and there is {free} free next to what is loaded.', {
                model: blocked.model, need: fmtGb(blocked.needBytes), size: fmtGb(blocked.sizeBytes), kv: fmtGb(blocked.kvBytes), free: fmtGb(blocked.budgetAlongsideBytes),
              })
            : t('{model} needs at least {need} — its {size} of weights, plus a KV cache that has never been measured on this machine — and there is {free} free next to what is loaded.', {
                model: blocked.model, need: fmtGb(blocked.needBytes), size: fmtGb(blocked.sizeBytes), free: fmtGb(blocked.budgetAlongsideBytes),
              })}
          {' '}
          {t('Short by {n}.', { n: fmtGb(blocked.shortfallBytes) })}
        </p>
        <p className="fs-muted">
          {blocked.gpuCount > 1
            ? t('{total} of VRAM across {n} GPUs. Ollama would still load it, spilling to the CPU and PCIe: much slower, and with two large models inside the machine can run out of memory.', { total: fmtGb(blocked.vramTotalBytes), n: blocked.gpuCount })
            : t('{total} of VRAM. Ollama would still load it, spilling to the CPU and PCIe: much slower, and with two large models inside the machine can run out of memory.', { total: fmtGb(blocked.vramTotalBytes) })}
        </p>
        {blocked.ramAvailableBytes != null && (
          <p className="fs-muted" data-tone={blocked.forcedLoadDangerous ? 'bad' : undefined} data-testid="vram-admission-ram">
            {blocked.forcedLoadDangerous
              ? t('Loading anyway would put {spill} in system RAM, and only {free} of {total} is free: that is the shape of the crash of 08-09-2026. Unload something instead.', { spill: fmtGb(blocked.shortfallBytes), free: fmtGb(blocked.ramAvailableBytes), total: fmtGb(blocked.ramTotalBytes ?? 0) })
              : t('Loading anyway would put {spill} in system RAM ({free} of {total} free).', { spill: fmtGb(blocked.shortfallBytes), free: fmtGb(blocked.ramAvailableBytes), total: fmtGb(blocked.ramTotalBytes ?? 0) })}
          </p>
        )}
        <h3 className="fs-vram__h">{tn(blocked.residents.length, '{n} model loaded — tick what to unload', '{n} models loaded — tick what to unload')}</h3>
        <ul className="fs-vram__list" data-testid="vram-admission-residents">
          {blocked.residents.map((r) => (
            <li key={r.name} className="fs-vram__row">
              <label className="fs-vram__label">
                <input
                  type="checkbox"
                  checked={picked.has(r.name)}
                  onChange={(e) => setPicked((cur) => { const next = new Set(cur); if (e.target.checked) next.add(r.name); else next.delete(r.name); return next; })}
                  aria-label={t('Unload {name}', { name: r.name })}
                />
                <span className="fs-vram__name">{r.name}</span>
              </label>
              <span className="fs-vram__meta">
                {fmtGb(r.inVramBytes)} {t('in VRAM')}
                {r.spillBytes > 0 ? ` · ${t('{n} spilled to RAM', { n: fmtGb(r.spillBytes) })}` : ''}
                {r.ctx ? ` · ${t('{n} tokens of context', { n: r.ctx.toLocaleString() })}` : ''}
              </span>
            </li>
          ))}
        </ul>
        <p className={enough ? 'fs-vram__sum' : 'fs-vram__sum fs-vram__sum--short'} aria-live="polite">
          {picked.size === 0
            ? t('Nothing ticked.')
            : enough
              ? t('Unloading the ticked models frees {n}: enough.', { n: fmtGb(frees) })
              : t('Unloading the ticked models frees {n}: still {m} short.', { n: fmtGb(frees), m: fmtGb(blocked.shortfallBytes - frees) })}
        </p>
        <p className="fs-muted">{t('The new model loads only once the ticked ones are really gone from memory.')}</p>
      </div>
    </Dialog>
  );
}
