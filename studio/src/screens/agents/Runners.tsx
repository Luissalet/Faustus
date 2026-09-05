import { Copy, Download, RefreshCw, Search, Wrench } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, IconButton, Skeleton, Toast } from '../../components';
import { launchRunner, listRunners, workerStatus, type Runner, type RunnerCatalogue } from '../../adapters/workers';
import { t } from '../../i18n';

/**
 * Agent runners (agentRunners.js): the CLI agents this machine can run AS A
 * WORKER. Three honesty rules travel with it: the licence word is printed
 * verbatim (open / subscription / unknown, never a guess); "installed" and
 * "can be a worker" are two facts shown as two; and the sentence this
 * feature must not hide — Faustus's guard cannot see inside another agent's
 * shell — is printed above the table, not in a tooltip.
 */

const LICENCE_HINT: Record<Runner['licence'], string> = {
  open: 'Open licence: you can run it without buying an account.',
  subscription: 'Needs a paid account with its vendor.',
  unknown: 'Faustus has not established a licence for this one. It says so rather than guess.',
};

const LICENCE_WORD: Record<Runner['licence'], string> = { open: 'open', subscription: 'subscription', unknown: 'unknown' };

function sortRunners(rows: Runner[]): Runner[] {
  return rows.slice().sort((a, b) => {
    if (a.installed !== b.installed) return a.installed ? -1 : 1;
    if (a.runnable_as_worker !== b.runnable_as_worker) return a.runnable_as_worker ? -1 : 1;
    if (a.invocation_known !== b.invocation_known) return a.invocation_known ? -1 : 1;
    return a.label.localeCompare(b.label);
  });
}

export function Runners({ onUseRunner }: { onUseRunner: (key: string) => void }) {
  const [data, setData] = useState<RunnerCatalogue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [log, setLog] = useState('');
  const [launching, setLaunching] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const flash = (msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 3000);
  };

  const load = useCallback(async (refresh: boolean) => {
    setError(null);
    try {
      setData(await listRunners(refresh));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setData((d) => d ?? { runners: [], enabled: true, guard_note: '', installed_count: 0, runnable_count: 0 });
    }
  }, []);

  useEffect(() => {
    void load(true);
    return () => abort.current?.abort();
  }, [load]);

  const visible = useMemo(() => {
    const rows = data?.runners ?? [];
    const needle = query.trim().toLowerCase();
    const hit = needle ? rows.filter((r) => r.key.toLowerCase().includes(needle) || r.label.toLowerCase().includes(needle) || r.aliases.some((a) => a.toLowerCase().includes(needle))) : rows;
    return sortRunners(hit);
  }, [data, query]);

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      flash(t('Command copied'));
    } catch {
      flash(t('Could not copy — select the command and copy it by hand.'));
    }
  };

  /* `ollama launch <key>` INSTALLS software: only ever a button the person pressed. */
  const launch = async (key: string) => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setLaunching(key);
    setLog(`$ ollama launch ${key}\n`);
    try {
      await launchRunner(key, (line) => setLog((l) => `${l}${line}\n`), controller.signal);
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) flash(`${t('The launch failed')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLaunching(null);
      await load(true);
    }
  };

  const all = data?.runners ?? [];
  return (
    <div className="fs-run" data-testid="runners">
      <div className="fs-agents__intro">
        <p className="fs-prose">{t('Any of these can be one of Faustus\'s workers: the checkpoint before, the diff after, Faustus\'s own verification and the honest proof — around an agent Faustus did not write.')}</p>
        <p className="fs-agents__guard">
          <strong>{t('What this cannot promise:')}</strong> {data?.guard_note || t('an external agent runs its own shell; Faustus does not see the commands it runs.')} {t('Every job that uses one says so in its verdict and carries')} <code>external_agent_unguarded</code> {t('in its proof.')}
        </p>
        {data && (
          <p className="fs-agents__note" data-on={data.enabled || undefined}>
            {data.enabled ? (
              <>{t('External agent runners are')} <strong>{t('on')}</strong>: {t('a dispatched task may name a')} <code>runner</code>, {t('and that agent does the work.')}</>
            ) : (
              <>{t('External agent runners are')} <strong>{t('off')}</strong> (<code>agent_external_runners</code>, {t('Settings → Agent')}). {t('It ships off because it runs third-party binaries on this machine. A dispatched task naming a')} <code>runner</code> {t('is refused with that reason until you turn it on.')}</>
            )}
          </p>
        )}
      </div>
      <div className="fs-agents__toolbar">
        <label className="fs-agents__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder={t('Search agents…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search agent runners')} />
        </label>
        {data && (
          <span className="fs-agents__counts">
            {t('{a} known · {b} installed · {c} usable as a worker right now', { a: all.length, b: data.installed_count, c: data.runnable_count })}
          </span>
        )}
        <span className="fs-agents__spacer" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} onClick={() => void load(true)} />
      </div>
      {error && <div className="fs-wk__error">{t('Could not read the agent runners')}: {error}</div>}
      {data === null ? (
        <Skeleton label={t('Loading the agent runners')} height="40px" count={4} />
      ) : (
        <div className="fs-run__table-wrap">
          <table className="fs-run__table">
            <thead>
              <tr>
                <th>{t('Agent')}</th>
                <th>{t('Licence')}</th>
                <th>{t('On this machine')}</th>
                <th>{t('As a worker')}</th>
                <th>{t('Install / launch')}</th>
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 && (
                <tr>
                  <td className="fs-run__empty" colSpan={5}>
                    {all.length ? t('No agent matches that search.') : t('No agent runners: this machine has no Ollama that knows any, and the built-in table could not be read.')}
                  </td>
                </tr>
              )}
              {visible.map((r) => {
                const w = workerStatus(r);
                return (
                  <tr key={r.key} className="fs-run__row" data-installed={r.installed || undefined} data-testid="runner-row">
                    <td className="fs-run__name-cell">
                      <span className="fs-run__name">{r.label}</span> <code className="fs-run__key">{r.key}</code>
                      {r.aliases.length > 0 && <span className="fs-run__aliases" title={t('Also known as')}>{r.aliases.join(', ')}</span>}
                      {r.notes && <span className="fs-run__notes">{r.notes}</span>}
                    </td>
                    <td>
                      <span className="fs-run__licence" data-licence={r.licence} title={t(LICENCE_HINT[r.licence])}>
                        {t(LICENCE_WORD[r.licence])}
                      </span>
                    </td>
                    <td>
                      <span className="fs-run__installed" data-yes={r.installed || undefined}>{r.installed ? t('installed') : t('not installed')}</span>
                      {r.version && <span className="fs-run__version">{r.version}</span>}
                      {r.gate && r.gate !== 'none' && (
                        <span className="fs-run__gate" title={r.gate_note}>
                          {t('guard')}: {r.gate}
                        </span>
                      )}
                    </td>
                    <td>
                      <span className="fs-run__worker" data-yes={w.can || undefined}>{w.label}</span>
                      <span className="fs-run__worker-detail">{w.detail}</span>
                      {w.can && <Button size="sm" variant="ghost" icon={Wrench} label={t('Use in a job')} onClick={() => onUseRunner(r.key)} />}
                    </td>
                    <td className="fs-run__launch-cell">
                      <code className="fs-run__command">{r.launch_command}</code>
                      <span className="fs-run__launch-actions">
                        <IconButton icon={Copy} label={t('Copy the launch command for {name}', { name: r.label })} size="sm" onClick={() => void copy(r.launch_command)} />
                        <Button size="sm" variant="secondary" icon={Download} label={t('Launch')} loading={launching === r.key} disabled={launching !== null && launching !== r.key} onClick={() => void launch(r.key)} title={t('Runs "{cmd}" on this machine and shows its output', { cmd: r.launch_command })} />
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {log && (
        <pre className="fs-run__log" data-testid="runner-log">
          {log}
        </pre>
      )}
      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
