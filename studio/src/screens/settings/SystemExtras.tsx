import { Download, RefreshCw, Trash2, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button } from '../../components';
import { diagnosticsLogs, exportBackup, importBackup, wipe, WIPE_KINDS } from '../../adapters/account';
import { t } from '../../i18n';
import { Select } from './fields';

/** The admin cards of the previous interface's System tab: logs, backup, the danger zone. */
export function SystemExtras({ say }: { say: (t: string) => void }) {
  return (
    <>
      <LogsCard />
      <BackupCard say={say} />
      <DangerCard say={say} />
    </>
  );
}

const LEVELS = ['ALL', 'INFO', 'WARNING', 'ERROR', 'DEBUG'];
const LIMITS = ['100', '200', '500', '1000'];

function LogsCard() {
  const [open, setOpen] = useState(false);
  const [lines, setLines] = useState<string[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [level, setLevel] = useState('ALL');
  const [limit, setLimit] = useState('200');
  const [q, setQ] = useState('');
  const [auto, setAuto] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  const load = () => {
    setErr(null);
    diagnosticsLogs(Number(limit))
      .then((l) => {
        setLines(l);
        window.requestAnimationFrame(() => {
          if (box.current) box.current.scrollTop = box.current.scrollHeight;
        });
      })
      .catch((e: Error) => setErr(e.message));
  };
  useEffect(() => {
    if (open) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, limit]);
  useEffect(() => {
    if (!open || !auto) return;
    const id = window.setInterval(load, 5000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, auto, limit]);

  const shown = (lines ?? []).filter((l) => (level === 'ALL' || l.includes(` - ${level} - `)) && (!q || l.toLowerCase().includes(q.toLowerCase())));
  const cls = (l: string) => (l.includes(' - ERROR - ') || l.includes(' - CRITICAL - ') ? 'error' : l.includes(' - WARNING - ') ? 'warning' : l.includes(' - DEBUG - ') ? 'debug' : l.includes(' - INFO - ') ? 'info' : undefined);

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title fs-tools__cat">
        <span>{t('Logs')}</span>
        <Button size="sm" variant="ghost" label={open ? t('Hide') : t('Show')} onClick={() => setOpen((o) => !o)} />
      </h3>
      <p className="fs-set__help">{t('The Faustus process log (data/logs/app.log), newest at the bottom.')}</p>
      {open && (
        <>
          <div className="fs-logs__bar">
            <input className="fs-field" placeholder={t('Search the logs…')} value={q} onChange={(e) => setQ(e.target.value)} aria-label={t('Search the logs')} />
            <Select id="log-level" value={level} options={LEVELS.map((v) => ({ value: v, label: v === 'ALL' ? t('All levels') : v }))} onChange={setLevel} />
            <Select id="log-limit" value={limit} options={LIMITS.map((v) => ({ value: v, label: t('last {n}', { n: v }) }))} onChange={setLimit} />
            <label className="fs-check">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> <span>{t('auto-refresh')}</span>
            </label>
            <Button size="sm" variant="ghost" icon={RefreshCw} label={t('Refresh')} onClick={load} />
          </div>
          {err && <p className="fs-set__err">{err}</p>}
          <div className="fs-logs" ref={box} role="log" aria-live="polite">
            {lines === null ? <p className="fs-set__help">{t('Loading')}</p> : shown.length === 0 ? <p className="fs-set__help">{t('Nothing matches.')}</p> : shown.map((l, i) => <div key={i} className="fs-logs__line" data-level={cls(l)}>{l}</div>)}
          </div>
        </>
      )}
    </div>
  );
}

function BackupCard({ say }: { say: (t: string) => void }) {
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Backup')}</h3>
      <p className="fs-set__help">{t('Export or import your data (memories, presets, settings, skills, preferences) as one JSON file. Importing merges with what is there.')}</p>
      <div className="fs-set__row-end">
        <Button size="sm" variant="secondary" icon={Download} label={t('Export the data')} loading={busy} onClick={() => {
          setBusy(true);
          exportBackup().then(() => say(t('Export downloaded.'))).catch((e: Error) => say(e.message)).finally(() => setBusy(false));
        }} />
        <Button size="sm" variant="secondary" icon={Upload} label={t('Import a file')} onClick={() => file.current?.click()} />
        <input
          ref={file}
          type="file"
          accept="application/json,.json"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = '';
            if (!f) return;
            setBusy(true);
            importBackup(f)
              .then((m) => say(m ? t('Imported: {what}', { what: m }) : t('Imported.')))
              .catch((e: Error) => say(e.message))
              .finally(() => setBusy(false));
          }}
        />
      </div>
    </div>
  );
}

function DangerCard({ say }: { say: (t: string) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (kind: string) => {
    const label = kind === '__all__' ? t('everything, in every category') : t(WIPE_KINDS.find((k) => k.kind === kind)?.label ?? kind).toLowerCase();
    if (!window.confirm(t('Delete {what}? This cannot be undone.', { what: label }))) return;
    if (!window.confirm(t('Really delete {what}?', { what: label }))) return;
    setBusy(kind);
    try {
      const kinds = kind === '__all__' ? WIPE_KINDS.map((k) => k.kind) : [kind];
      let total = 0;
      for (const k of kinds) total += await wipe(k);
      say(t('Deleted ({n} items).', { n: total }));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="fs-set__card fs-set__card--danger">
      <h3 className="fs-set__card-title">{t('Danger zone')}</h3>
      <p className="fs-set__help">{t('Irreversible. Each wipe targets one category; pick exactly what you want gone.')}</p>
      <ul className="fs-wipe">
        {[...WIPE_KINDS, { kind: '__all__', label: 'Delete everything', help: 'All the categories above, in one go.' }].map((k) => (
          <li key={k.kind} className="fs-wipe__row">
            <span>
              <strong>{t(k.label)}</strong>
              <span className="fs-set__help">{t(k.help)}</span>
            </span>
            <Button size="sm" variant={k.kind === '__all__' ? 'danger-solid' : 'danger'} icon={Trash2} label={t('Delete')} loading={busy === k.kind} disabled={busy !== null} onClick={() => void run(k.kind)} />
          </li>
        ))}
      </ul>
    </div>
  );
}
