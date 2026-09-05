import { Copy, RefreshCw, Search, Wrench } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, IconButton, Skeleton, Toast } from '../../components';
import { delegateStatus, listDefs, MODE_HINT, SOURCE_HINT, type AgentDef, type DefCatalogue } from '../../adapters/workers';
import { t, tn } from '../../i18n';

/**
 * Agent definitions (agentDefs.js): what each agent on this machine may and
 * may not do. The cards show RESOLVED rules, never the raw frontmatter; a
 * file that would not load is listed with its reason, next to the ones that
 * did; the sentence a path rule cannot promise is printed above the list.
 */

const MODE_WORD: Record<AgentDef['mode'], string> = { coordinator: 'coordinator', worker: 'worker', reviewer: 'reviewer' };
const SOURCE_WORD: Record<AgentDef['source'], string> = { builtin: 'built-in', user: 'yours', repo: 'the folder\'s' };

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
      flash(t('Slug copied'));
    } catch {
      flash(t('Could not copy — select the slug and copy it by hand.'));
    }
  };

  const all = data?.agents ?? [];
  return (
    <div className="fs-def" data-testid="defs">
      <div className="fs-agents__intro">
        <p className="fs-prose">{t('An agent is a file: what it may use, what it may touch, where it runs. Put its slug on a dispatched task and the worker starts under it.')}</p>
        <p className="fs-agents__guard">
          <strong>{t('What a path rule cannot promise:')}</strong> {data?.shell_note || t('a path rule governs the file tools, not another program\'s shell.')}
        </p>
      </div>
      <div className="fs-agents__toolbar">
        <label className="fs-agents__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder={t('Search definitions…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search agent definitions')} />
        </label>
        {data && (
          <span className="fs-agents__counts">
            {tn(all.length, '{n} definition', '{n} definitions')} · {t('delegation depth ceiling')} {data.max_depth} (<code>{data.depth_setting}</code>)
          </span>
        )}
        <span className="fs-agents__spacer" />
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} onClick={() => void load()} />
      </div>
      {error && <div className="fs-wk__error">{t('Could not read the definitions')}: {error}</div>}
      {data && data.errors.length > 0 && (
        <div className="fs-def__errors">
          <p>
            {tn(data.errors.length, '{n} definition file did not load. It is not in force:', '{n} definition files did not load. They are not in force:')}
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
        <Skeleton label={t('Loading the definitions')} height="160px" count={3} radius="panel" />
      ) : visible.length === 0 ? (
        <p className="fs-agents__empty">{all.length ? t('No definition matches that search.') : t('No agent definitions: not even the built-ins could be read.')}</p>
      ) : (
        <div className="fs-def__grid">
          {visible.map((d) => {
            const del = delegateStatus(d, data.max_depth);
            const where = [d.model && `${t('model')} ${d.model}`, d.endpoint_id && `endpoint ${d.endpoint_id}`, d.runner && `runner ${d.runner}`].filter(Boolean).join(' · ');
            return (
              <article key={d.slug} className="fs-def__card" data-testid="def-card">
                <header className="fs-def__head">
                  <span className="fs-def__name">{d.name}</span>
                  <code className="fs-def__slug">{d.slug}</code>
                  <span className="fs-def__tag" data-mode={d.mode} title={t(MODE_HINT[d.mode])}>
                    {t(MODE_WORD[d.mode])}
                  </span>
                  <span className="fs-def__tag" data-source={d.source} title={t(SOURCE_HINT[d.source])}>
                    {t(SOURCE_WORD[d.source])}
                  </span>
                </header>
                {d.description && <p className="fs-def__desc">{d.description}</p>}
                {where && <p className="fs-def__route">{where}</p>}
                <ul className="fs-def__rules">
                  {d.rules.map((r, i) => (
                    <li key={i} className="fs-def__rule" data-effect={r.effect}>
                      <span className="fs-def__effect">{r.effect === 'deny' ? t('no') : t('yes')}</span>
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
                  <Button size="sm" variant="secondary" icon={Wrench} label={t('Use in a job')} onClick={() => onUseAgent(d.slug)} />
                  <code className="fs-def__usage">"agent": "{d.slug}"</code>
                  <IconButton icon={Copy} label={t('Copy the slug of {name}', { name: d.name })} size="sm" onClick={() => void copy(d.slug)} />
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
