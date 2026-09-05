import { Copy, RefreshCw, Search, Wrench } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, IconButton, Skeleton, Toast } from '../../components';
import { delegateStatus, listDefs, MODE_HINT, SOURCE_HINT, type AgentDef, type DefCatalogue } from '../../adapters/workers';

/**
 * Agent definitions (agentDefs.js): what each agent on this machine may and
 * may not do. The cards show RESOLVED rules, never the raw frontmatter; a
 * file that would not load is listed with its reason, next to the ones that
 * did; the sentence a path rule cannot promise is printed above the list.
 */

const MODE_WORD: Record<AgentDef['mode'], string> = { coordinator: 'coordinador', worker: 'worker', reviewer: 'revisor' };
const SOURCE_WORD: Record<AgentDef['source'], string> = { builtin: 'integrado', user: 'tuyo', repo: 'de la carpeta' };

function sortDefs(rows: AgentDef[]): AgentDef[] {
  const order = { repo: 0, user: 1, builtin: 2 };
  return rows.slice().sort((a, b) => order[a.source] - order[b.source] || a.name.localeCompare(b.name));
}

export function Defs({ onUseAgent }: { onUseAgent: (slug: string) => void }) {
  const [data, setData] = useState<DefCatalogue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [toast, setToast] = useState<string | null>(null);

  const flash = (msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 3000);
  };

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await listDefs());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setData((d) => d ?? { agents: [], errors: [], max_depth: 1, depth_setting: 'agent_subagent_depth', shell_note: '' });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const visible = useMemo(() => {
    const rows = data?.agents ?? [];
    const needle = query.trim().toLowerCase();
    const hit = needle ? rows.filter((r) => r.slug.toLowerCase().includes(needle) || r.name.toLowerCase().includes(needle) || r.description.toLowerCase().includes(needle) || r.rules.some((x) => x.detail.toLowerCase().includes(needle))) : rows;
    return sortDefs(hit);
  }, [data, query]);

  const copy = async (slug: string) => {
    try {
      await navigator.clipboard.writeText(`"agent": "${slug}"`);
      flash('Slug copiado');
    } catch {
      flash('No he podido copiar — selecciona el slug y cópialo a mano.');
    }
  };

  const all = data?.agents ?? [];
  return (
    <div className="fs-def" data-testid="defs">
      <div className="fs-agents__intro">
        <p className="fs-prose">Un agente es un fichero: qué puede usar, qué puede tocar, dónde corre. Pon su slug en una tarea despachada y el worker arranca bajo él.</p>
        <p className="fs-agents__guard">
          <strong>Lo que una regla de ruta no puede prometer:</strong> {data?.shell_note || 'una regla de ruta gobierna las herramientas de fichero, no la shell de otro programa.'}
        </p>
      </div>
      <div className="fs-agents__toolbar">
        <label className="fs-agents__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder="Buscar definiciones…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Buscar definiciones de agente" />
        </label>
        {data && (
          <span className="fs-agents__counts">
            {all.length} definici{all.length === 1 ? 'ón' : 'ones'} · techo de delegación {data.max_depth} (<code>{data.depth_setting}</code>)
          </span>
        )}
        <span className="fs-agents__spacer" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label="Actualizar" onClick={() => void load()} />
      </div>
      {error && <div className="fs-wk__error">No he podido leer las definiciones: {error}</div>}
      {data && data.errors.length > 0 && (
        <div className="fs-def__errors">
          <p>
            {data.errors.length} fichero{data.errors.length === 1 ? '' : 's'} de definición no cargó{data.errors.length === 1 ? '' : 'aron'}. No están en vigor:
          </p>
          <ul>
            {data.errors.map((e, i) => (
              <li key={i}>
                <code>{e.path || e.slug}</code> — {e.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {data === null ? (
        <Skeleton label="Cargando las definiciones" height="160px" count={3} radius="panel" />
      ) : visible.length === 0 ? (
        <p className="fs-agents__empty">{all.length ? 'Ninguna definición coincide con esa búsqueda.' : 'No hay definiciones de agente: ni las integradas se pudieron leer.'}</p>
      ) : (
        <div className="fs-def__grid">
          {visible.map((d) => {
            const del = delegateStatus(d, data.max_depth);
            const where = [d.model && `modelo ${d.model}`, d.endpoint_id && `endpoint ${d.endpoint_id}`, d.runner && `runner ${d.runner}`].filter(Boolean).join(' · ');
            return (
              <article key={d.slug} className="fs-def__card" data-testid="def-card">
                <header className="fs-def__head">
                  <span className="fs-def__name">{d.name}</span>
                  <code className="fs-def__slug">{d.slug}</code>
                  <span className="fs-def__tag" data-mode={d.mode} title={MODE_HINT[d.mode]}>
                    {MODE_WORD[d.mode]}
                  </span>
                  <span className="fs-def__tag" data-source={d.source} title={SOURCE_HINT[d.source]}>
                    {SOURCE_WORD[d.source]}
                  </span>
                </header>
                {d.description && <p className="fs-def__desc">{d.description}</p>}
                {where && <p className="fs-def__route">{where}</p>}
                <ul className="fs-def__rules">
                  {d.rules.map((r, i) => (
                    <li key={i} className="fs-def__rule" data-effect={r.effect}>
                      <span className="fs-def__effect">{r.effect === 'deny' ? 'no' : 'sí'}</span>
                      <span className="fs-def__what">{r.what}</span>
                      <span className="fs-def__detail">{r.detail}</span>
                    </li>
                  ))}
                </ul>
                <p className="fs-def__delegate" data-yes={del.can || undefined}>
                  {del.label} — {del.detail}
                </p>
                {d.caveats.map((c, i) => (
                  <p key={i} className="fs-def__caveat">
                    {c}
                  </p>
                ))}
                <div className="fs-def__actions">
                  <Button size="sm" variant="secondary" icon={Wrench} label="Usar en un trabajo" onClick={() => onUseAgent(d.slug)} />
                  <code className="fs-def__usage">"agent": "{d.slug}"</code>
                  <IconButton icon={Copy} label={`Copiar el slug de ${d.name}`} size="sm" onClick={() => void copy(d.slug)} />
                </div>
              </article>
            );
          })}
        </div>
      )}
      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
