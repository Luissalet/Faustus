import { Copy, Download, RefreshCw, Search, Wrench } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, IconButton, Skeleton, Toast } from '../../components';
import { launchRunner, listRunners, workerStatus, type Runner, type RunnerCatalogue } from '../../adapters/workers';

/**
 * Agent runners (agentRunners.js): the CLI agents this machine can run AS A
 * WORKER. Three honesty rules travel with it: the licence word is printed
 * verbatim (open / subscription / unknown, never a guess); "installed" and
 * "can be a worker" are two facts shown as two; and the sentence this
 * feature must not hide — Faustus's guard cannot see inside another agent's
 * shell — is printed above the table, not in a tooltip.
 */

const LICENCE_HINT: Record<Runner['licence'], string> = {
  open: 'Licencia abierta: puedes correrlo sin comprar una cuenta.',
  subscription: 'Necesita una cuenta de pago con su proveedor.',
  unknown: 'Faustus no ha establecido la licencia de este. Lo dice en vez de adivinar.',
};

const LICENCE_WORD: Record<Runner['licence'], string> = { open: 'abierta', subscription: 'suscripción', unknown: 'desconocida' };

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
      flash('Comando copiado');
    } catch {
      flash('No he podido copiar — selecciona el comando y cópialo a mano.');
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
      if (!(e instanceof DOMException && e.name === 'AbortError')) flash(`El lanzamiento falló: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLaunching(null);
      await load(true);
    }
  };

  const all = data?.runners ?? [];
  return (
    <div className="fs-run" data-testid="runners">
      <div className="fs-agents__intro">
        <p className="fs-prose">Cualquiera de estos puede ser uno de los workers de Faustus: el punto de control antes, la comparación después, la verificación de Faustus y la prueba honesta — alrededor de un agente que Faustus no escribió.</p>
        <p className="fs-agents__guard">
          <strong>Lo que esto no puede prometer:</strong> {data?.guard_note || 'un agente externo corre su propia shell; Faustus no ve los comandos que ejecuta.'} Cada trabajo que usa uno lo dice en su veredicto y lleva <code>external_agent_unguarded</code> en su prueba.
        </p>
        {data && (
          <p className="fs-agents__note" data-on={data.enabled || undefined}>
            {data.enabled ? (
              <>Los agent runners externos están <strong>activados</strong>: una tarea despachada puede nombrar un <code>runner</code>, y ese agente hace el trabajo.</>
            ) : (
              <>Los agent runners externos están <strong>desactivados</strong> (<code>agent_external_runners</code>, Ajustes → Agente). Viene apagado porque ejecuta binarios de terceros en esta máquina. Una tarea despachada que nombre un <code>runner</code> se rechaza con ese motivo hasta que lo actives.</>
            )}
          </p>
        )}
      </div>
      <div className="fs-agents__toolbar">
        <label className="fs-agents__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder="Buscar agentes…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Buscar agent runners" />
        </label>
        {data && (
          <span className="fs-agents__counts">
            {all.length} conocidos · {data.installed_count} instalados · {data.runnable_count} usables como worker ahora mismo
          </span>
        )}
        <span className="fs-agents__spacer" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label="Actualizar" onClick={() => void load(true)} />
      </div>
      {error && <div className="fs-wk__error">No he podido leer los agent runners: {error}</div>}
      {data === null ? (
        <Skeleton label="Cargando los agent runners" height="40px" count={4} />
      ) : (
        <div className="fs-run__table-wrap">
          <table className="fs-run__table">
            <thead>
              <tr>
                <th>Agente</th>
                <th>Licencia</th>
                <th>En esta máquina</th>
                <th>Como worker</th>
                <th>Instalar / lanzar</th>
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 && (
                <tr>
                  <td className="fs-run__empty" colSpan={5}>
                    {all.length ? 'Ningún agente coincide con esa búsqueda.' : 'No hay agent runners: esta máquina no tiene un Ollama que conozca ninguno, y la tabla integrada no se pudo leer.'}
                  </td>
                </tr>
              )}
              {visible.map((r) => {
                const w = workerStatus(r);
                return (
                  <tr key={r.key} className="fs-run__row" data-installed={r.installed || undefined} data-testid="runner-row">
                    <td className="fs-run__name-cell">
                      <span className="fs-run__name">{r.label}</span> <code className="fs-run__key">{r.key}</code>
                      {r.aliases.length > 0 && <span className="fs-run__aliases" title="También conocido como">{r.aliases.join(', ')}</span>}
                      {r.notes && <span className="fs-run__notes">{r.notes}</span>}
                    </td>
                    <td>
                      <span className="fs-run__licence" data-licence={r.licence} title={LICENCE_HINT[r.licence]}>
                        {LICENCE_WORD[r.licence]}
                      </span>
                    </td>
                    <td>
                      <span className="fs-run__installed" data-yes={r.installed || undefined}>{r.installed ? 'instalado' : 'no instalado'}</span>
                      {r.version && <span className="fs-run__version">{r.version}</span>}
                      {r.gate && r.gate !== 'none' && (
                        <span className="fs-run__gate" title={r.gate_note}>
                          guardia: {r.gate}
                        </span>
                      )}
                    </td>
                    <td>
                      <span className="fs-run__worker" data-yes={w.can || undefined}>{w.label}</span>
                      <span className="fs-run__worker-detail">{w.detail}</span>
                      {w.can && <Button size="sm" variant="ghost" icon={Wrench} label="Usar en un trabajo" onClick={() => onUseRunner(r.key)} />}
                    </td>
                    <td className="fs-run__launch-cell">
                      <code className="fs-run__command">{r.launch_command}</code>
                      <span className="fs-run__launch-actions">
                        <IconButton icon={Copy} label={`Copiar el comando de ${r.label}`} size="sm" onClick={() => void copy(r.launch_command)} />
                        <Button size="sm" variant="secondary" icon={Download} label="Lanzar" loading={launching === r.key} disabled={launching !== null && launching !== r.key} onClick={() => void launch(r.key)} title={`Ejecuta «${r.launch_command}» en esta máquina y muestra su salida`} />
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
