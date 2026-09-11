import { Download, RefreshCw, ShieldAlert, Stethoscope, Trash2, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button } from '../../components';
import { diagnosticsLogs, exportBackup, importBackup, wipe, WIPE_KINDS } from '../../adapters/account';
import { getJson } from '../../adapters/api';
import { t } from '../../i18n';
import { Select } from './fields';

/** Small local POST helper (BASE-03/OPS-01/OPS-05 cards below). Not added to
 *  adapters/account.ts on purpose: this file's PROPIOS scope for this lote is
 *  SystemExtras.tsx itself, not the shared adapters module. */
async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let reason = `${path}: ${r.status}`;
    try {
      const j = await r.json();
      if (j?.detail) reason = typeof j.detail === 'string' ? j.detail : reason;
    } catch {
      /* ignore */
    }
    throw new Error(reason);
  }
  return (await r.json()) as T;
}

/** The admin cards of the previous interface's System tab: logs, backup, the danger zone. */
export function SystemExtras({ say }: { say: (t: string) => void }) {
  return (
    <>
      <VersionCard />
      <SetupCard />
      <DoctorCard say={say} />
      <SafeModeCard say={say} />
      <ChaosCard say={say} />
      <LogsCard />
      <BackupCard say={say} />
      <DangerCard say={say} />
    </>
  );
}

interface ChaosDryRun { fixture: string; would_inject: string; expected: string; }

/**
 * EVAL-03: a "play this chaos fixture" card, dry-run only — never anything
 * that actually injects a failure from Studio. `POST /api/ops/chaos/
 * {fixture}` (contract: `{dry_run: true}` -> `{fixture, would_inject,
 * expected}`) is lote 67's route and does not exist in this checkout yet
 * (`routes/`, `src/` have no `ops/chaos` route as of this lote — verified
 * by grep, not assumed); this card is wired against that exact contract and
 * degrades to a clear "not available yet" message on 404 rather than a bare
 * fetch failure, so it starts working the moment that route lands with no
 * further Studio change needed. */
function ChaosCard({ say }: { say: (t: string) => void }) {
  const [fixture, setFixture] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ChaosDryRun | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  const run = () => {
    const name = fixture.trim();
    if (!name) return;
    setBusy(true);
    setErr(null);
    setResult(null);
    setUnavailable(false);
    fetch(`/api/ops/chaos/${encodeURIComponent(name)}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: true }),
    })
      .then(async (r) => {
        if (r.status === 404) {
          setUnavailable(true);
          return;
        }
        if (!r.ok) {
          let reason = `HTTP ${r.status}`;
          try {
            const j = await r.json();
            if (typeof j?.detail === 'string') reason = j.detail;
          } catch {
            /* ignore */
          }
          throw new Error(reason);
        }
        setResult((await r.json()) as ChaosDryRun);
      })
      .catch((e: Error) => setErr(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Chaos fixture (dry run)')}</h3>
      <p className="fs-set__help">{t('Ask what a chaos fixture would inject and what a correct system does about it — dry run only, this card never injects a real failure.')}</p>
      <div className="fs-set__inline">
        <input className="fs-field" placeholder={t('fixture name')} value={fixture} onChange={(e) => setFixture(e.target.value)} data-testid="chaos-fixture-input" />
        <Button size="sm" variant="secondary" label={t('Dry run')} loading={busy} disabled={!fixture.trim()} onClick={run} testId="chaos-fixture-run" />
      </div>
      {unavailable && (
        <p className="fs-set__help" data-tone="warn">
          {t('POST /api/ops/chaos/{fixture} does not exist on this server yet — this card is ready for it.', { fixture })}
        </p>
      )}
      {err && <p className="fs-set__err">{err}</p>}
      {result && (
        <ul className="fs-set__help" data-testid="chaos-fixture-result">
          <li><strong>{t('Would inject')}</strong>: {result.would_inject}</li>
          <li><strong>{t('Expected')}</strong>: {result.expected}</li>
        </ul>
      )}
    </div>
  );
}

/** SET-03: settings' effective-value / precedence view already exists
 *  (lote 9, `Settings.tsx`'s `data-changed` + `explain` machinery) — this is
 *  only the link INTO it from Diagnostics, so a person troubleshooting here
 *  is not left to go hunt for it. */
function SetupCard() {
  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Setup & settings')}</h3>
      <p className="fs-set__help">{t('Effective settings — what is actually in force, and why — live in the main Settings screen.')}</p>
      <div className="fs-set__row-end">
        <Button size="sm" variant="secondary" label={t('Open Settings')} onClick={() => { window.location.hash = '#/settings'; }} />
      </div>
    </div>
  );
}

interface DoctorFinding {
  area: string;
  name: string;
  state: 'ok' | 'warn' | 'fail' | 'unknown' | 'absent';
  detail: string;
  fix: string;
  facts?: { repair?: string; [k: string]: unknown };
}
interface DoctorReport {
  worst: string;
  checked_at: string;
  findings: DoctorFinding[];
}

const STATE_ORDER: Record<string, number> = { fail: 0, unknown: 1, warn: 2, absent: 3, ok: 4 };

/** BASE-03 / OPS-01: `src/doctor.py::run()` asked and answered, in Studio —
 *  it had no consumer here before this lote (see docs/spec/v2/MAPA_REUTILIZACION.md
 *  BASE-03/OPS-01/SET-01 rows). Reads the SAME `/api/doctor` the CLI
 *  (`python -m src.doctor --json`) and `routes/changesets_routes.py` already
 *  serve — no new report format, only a screen for the existing one. */
function DoctorCard({ say }: { say: (t: string) => void }) {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [repairing, setRepairing] = useState<string | null>(null);

  const load = () => {
    setErr(null);
    getJson<DoctorReport>('/api/doctor')
      .then(setReport)
      .catch((e: Error) => setErr(e.message));
  };
  useEffect(load, []);

  const repair = (name: string) => {
    setRepairing(name);
    postJson<{ ok: boolean; stderr?: string }>('/api/doctor/repair', { repair: name })
      .then((r) => {
        say(r.ok ? t('Repair finished.') : t('Repair did not succeed: {why}', { why: r.stderr || '?' }));
        load();
      })
      .catch((e: Error) => say(e.message))
      .finally(() => setRepairing(null));
  };

  const findings = [...(report?.findings ?? [])]
    .filter((f) => f.state !== 'ok')
    .sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state]);

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title fs-tools__cat">
        <span><Stethoscope size={16} aria-hidden /> {t('Doctor')}</span>
        <Button size="sm" variant="ghost" icon={RefreshCw} label={t('Re-check')} onClick={load} />
      </h3>
      <p className="fs-set__help">{t('What this machine can actually do, asked rather than assumed — the same report as `python -m src.doctor`.')}</p>
      {err && <p className="fs-set__err">{err}</p>}
      {!report && !err ? (
        <p className="fs-set__help">{t('Loading')}</p>
      ) : findings.length === 0 ? (
        <p className="fs-set__help">{t('Everything checked is working.')}</p>
      ) : (
        <ul className="fs-wipe">
          {findings.map((f) => (
            <li key={`${f.area}/${f.name}`} className="fs-wipe__row">
              <span>
                <strong>{f.state.toUpperCase()} — {f.area}/{f.name}</strong>
                <span className="fs-set__help">{f.detail}{f.fix ? ` — ${f.fix}` : ''}</span>
              </span>
              {f.facts?.repair && (
                <Button size="sm" variant="secondary" loading={repairing === f.facts.repair}
                        disabled={repairing !== null} label={t('Repair')}
                        onClick={() => repair(f.facts!.repair as string)} />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface SafeModeStatus {
  active: boolean;
  reason: string;
  disabled: Record<string, boolean>;
  quarantined_mcp_servers: string[];
  /** OPS-05 (lote 42): `src/safe_mode.py::status()` already returned this --
   *  the "why" for a quarantined server was in the API response the whole
   *  time, just never in this interface or rendered below. */
  mcp_fail_counts: Record<string, number>;
  core_available: boolean;
}

const SUBSYSTEM_LABEL: Record<string, string> = {
  mcp_external: 'External MCP servers',
  plugins_third_party: 'Third-party plugins',
  skills_third_party: 'Skills not written by the owner',
  scheduled_tasks: 'Scheduled / recurring tasks',
};

/** OPS-05 / QA-46: safe mode's status and one-at-a-time review, described in
 *  plain terms — which is off, why, and a button per item to turn it back on.
 *  Core (chat, files, settings) is never in this list; `src/safe_mode.py`
 *  never gates it. */
function SafeModeCard({ say }: { say: (t: string) => void }) {
  const [status, setStatus] = useState<SafeModeStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = () => {
    setErr(null);
    getJson<SafeModeStatus>('/api/safe-mode/status').then(setStatus).catch((e: Error) => setErr(e.message));
  };
  useEffect(load, []);

  const reactivateSubsystem = (subsystem: string) => {
    setBusy(subsystem);
    postJson('/api/safe-mode/reactivate', { subsystem }).then(() => { say(t('Reactivated.')); load(); })
      .catch((e: Error) => say(e.message)).finally(() => setBusy(null));
  };
  const reactivateServer = (id: string) => {
    setBusy(id);
    postJson('/api/safe-mode/reactivate', { mcp_server_id: id }).then(() => { say(t('Reactivated.')); load(); })
      .catch((e: Error) => say(e.message)).finally(() => setBusy(null));
  };

  if (err) return <div className="fs-set__card"><h3 className="fs-set__card-title">{t('Safe mode')}</h3><p className="fs-set__err">{err}</p></div>;
  if (!status) return <div className="fs-set__card"><h3 className="fs-set__card-title">{t('Safe mode')}</h3><p className="fs-set__help">{t('Loading')}</p></div>;

  const heldBack = Object.entries(status.disabled).filter(([, v]) => v);

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title"><ShieldAlert size={16} aria-hidden /> {t('Safe mode')}</h3>
      {!status.active && status.quarantined_mcp_servers.length === 0 ? (
        <p className="fs-set__help">{t('Not active. Core, external MCP servers, plugins, skills and scheduled tasks are all normal.')}</p>
      ) : (
        <>
          {status.active && (
            <p className="fs-set__help">
              {t('ACTIVE — {reason}. Chat, files and settings still work normally.', { reason: status.reason || '?' })}
            </p>
          )}
          {heldBack.length > 0 && (
            <ul className="fs-wipe">
              {heldBack.map(([key]) => (
                <li key={key} className="fs-wipe__row">
                  <span><strong>{t(SUBSYSTEM_LABEL[key] ?? key)}</strong> <span className="fs-set__help">{t('held back')}</span></span>
                  <Button size="sm" variant="secondary" loading={busy === key} disabled={busy !== null}
                          label={t('Turn back on')} onClick={() => reactivateSubsystem(key)} />
                </li>
              ))}
            </ul>
          )}
          {status.quarantined_mcp_servers.length > 0 && (
            <>
              <p className="fs-set__help">{t('MCP servers quarantined after repeated failures at startup:')}</p>
              <ul className="fs-wipe">
                {status.quarantined_mcp_servers.map((id) => (
                  <li key={id} className="fs-wipe__row">
                    <span>
                      {id}{' '}
                      <span className="fs-set__help">
                        {t('({n} consecutive failed connection attempts)', { n: status.mcp_fail_counts?.[id] ?? '?' })}
                      </span>
                    </span>
                    <Button size="sm" variant="secondary" loading={busy === id} disabled={busy !== null}
                            label={t('Reactivate')} onClick={() => reactivateServer(id)} />
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </div>
  );
}

/** BASE-01: `/api/version`'s app version plus, since lote 6, the running
 *  checkout's build (short commit sha + date, read straight out of `.git`)
 *  and the actually-served `/studio` HTML's mtime + content hash — the
 *  Diagnostics-screen "what am I actually running" line. */
interface VersionInfo {
  version: string;
  build: { sha: string | null; date: string | null };
  served_studio: {
    mtime: string | null;
    sha: string | null;
    /** The bundle the shell loads (`studio.js?v=<sha>`): the shell's own hash
     *  does not move when Studio is rebuilt, this one does. */
    bundle?: { mtime: string | null; sha: string | null };
  };
  /** BASE-01 / OPS-06: `src/api_version.py::client_adaptation_notice()`,
   *  wired into `/api/version` by lote 61 — null unless THIS request's own
   *  `X-Faustus-Client-Version` header (this same page, so it names Studio's
   *  own build) reads as supported-but-below-the-server's-current version.
   *  Shown so an administrator sees the soft warning before a client ever
   *  hits it as a broken response instead of a name. */
  client_adaptation_notice?: string | null;
}

function VersionCard() {
  const [info, setInfo] = useState<VersionInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    getJson<VersionInfo>('/api/version')
      .then((v) => {
        if (!cancelled) setInfo(v);
      })
      .catch((e: Error) => {
        if (!cancelled) setErr(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Version')}</h3>
      {err ? (
        <p className="fs-set__err">{err}</p>
      ) : !info ? (
        <p className="fs-set__help">{t('Loading')}</p>
      ) : (
        <p className="fs-set__help">
          {info.build.sha && info.served_studio.sha
            ? t('Version {v} · build {sha} ({date}) · Studio served: {served}', {
                v: info.version,
                sha: info.build.sha,
                date: info.build.date ?? '?',
                served: info.served_studio.bundle?.sha
                  ? `${info.served_studio.bundle.sha} (${info.served_studio.bundle.mtime ?? '?'})`
                  : info.served_studio.sha,
              })
            : t('Version {v}', { v: info.version })}
        </p>
      )}
      {info?.client_adaptation_notice && (
        <p className="fs-set__help" data-tone="warn" role="alert">
          {info.client_adaptation_notice}
        </p>
      )}
    </div>
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

interface BackupSnapshot { name: string; modified: string; }
interface VerifyReport {
  ok: boolean;
  problems: string[];
  restore_check?: { performed: boolean; databases: { name: string; ok: boolean; compared_to_manifest: boolean }[]; problems: string[] };
}

function BackupCard({ say }: { say: (t: string) => void }) {
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [verified, setVerified] = useState<VerifyReport | null>(null);

  /** OPS-03: proves the newest whole-`data/` snapshot would actually
   *  restore — `backup_service.verify_backup` extracts it to a throwaway
   *  directory, opens every database and compares its row counts against
   *  the manifest recorded at snapshot time, not just an integrity_check of
   *  the tarball. */
  const verifyLatest = () => {
    setVerifying(true);
    setVerified(null);
    getJson<{ snapshots: BackupSnapshot[] }>('/api/backup/snapshots')
      .then((r) => {
        const latest = r.snapshots[0];
        if (!latest) throw new Error(t('No snapshots yet.'));
        return postJson<VerifyReport>('/api/backup/verify', { name: latest.name });
      })
      .then(setVerified)
      .catch((e: Error) => say(e.message))
      .finally(() => setVerifying(false));
  };

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
      <div className="fs-set__row-end">
        <Button size="sm" variant="ghost" loading={verifying} label={t('Verify the latest snapshot restores')} onClick={verifyLatest} />
      </div>
      {verified && (
        <p className={verified.ok ? 'fs-set__help' : 'fs-set__err'}>
          {verified.ok
            ? t('Verified: the latest snapshot opens, restores and its row counts match the backup-time manifest.')
            : t('NOT verified: {problems}', {
                problems: [...verified.problems, ...(verified.restore_check?.problems ?? [])].join('; ') || '?',
              })}
        </p>
      )}
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
