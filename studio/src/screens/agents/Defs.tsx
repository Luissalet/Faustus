import { Copy, RefreshCw, Search, ShieldAlert, SlidersHorizontal, Wrench } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Dialog, IconButton, Skeleton, Toast } from '../../components';
import { delegateStatus, listDefs, MODE_HINT, SOURCE_HINT, type AgentDef, type DefCatalogue } from '../../adapters/workers';
import {
  AgentProfileRefusal,
  loadAgentProfiles,
  resolveEffective,
  type AgentProfile,
  type EffectiveConfig,
} from '../../adapters/agents';
import { ProfileLint } from './ProfileLint';
import { t, tn } from '../../i18n';

/**
 * Agent definitions (agentDefs.js): what each agent on this machine may and
 * may not do. The cards show RESOLVED rules, never the raw frontmatter; a
 * file that would not load is listed with its reason, next to the ones that
 * did; the sentence a path rule cannot promise is printed above the list.
 *
 * The profiles half (src/agent_profiles/) rides on the same cards: how far
 * each agent pushes by default, what it declares it can do, and the four
 * versioned policies it references. None of that is a permission, and the
 * card says so where it would otherwise be read as one.
 *
 * "Effective configuration" is the button that answers the question the
 * frontmatter cannot: what a run would ACTUALLY start from once the eight
 * levels of the precedence have been walked — the value, the level that put
 * it there, and what was discarded on the way.
 */

const MODE_WORD: Record<AgentDef['mode'], string> = { coordinator: 'coordinator', worker: 'worker', reviewer: 'reviewer' };
const SOURCE_WORD: Record<AgentDef['source'], string> = { builtin: 'built-in', user: 'yours', repo: 'the folder\'s' };

function sortDefs(rows: AgentDef[]): AgentDef[] {
  const order = { repo: 0, user: 1, builtin: 2 };
  return rows.slice().sort((a, b) => order[a.source] - order[b.source] || a.name.localeCompare(b.name));
}

/** A short list as one line, or nothing at all. Empty renders no row: a label
 * with a dash after it is noise on every card that never used the field. */
function Words({ label, words }: { label: string; words: string[] }) {
  if (!words.length) return null;
  return (
    <p className="fs-def__route">
      <span className="fs-def__what">{label}</span> {words.join(', ')}
    </p>
  );
}

/**
 * The effective configuration, as the resolver explains it.
 *
 * Three columns because three questions get asked in this order: what is it
 * set to, who set it, and what did somebody else want instead. A table that
 * answered only the first would send its reader back to the definition file
 * to re-derive the other two.
 *
 * Which is why `from` is the one column that may never be the one that falls
 * off the edge. The dialog widens for this table (agents.css), the columns
 * are declared rather than measured, and a digest or a work root breaks
 * inside its own cell instead of pushing the level out of view.
 */
function Effective({ config, name, onClose }: { config: EffectiveConfig | null; name: string; onClose: () => void }) {
  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={t('Effective configuration of {name}', { name })}
      testId="def-effective"
    >
      {config === null ? (
        <div className="fs-def__effective">
          <Skeleton label={t('Resolving the effective configuration')} height="120px" count={2} radius="panel" />
        </div>
      ) : (
        <div className="fs-def__effective">
          <p className="fs-prose">
            {t('What a run would start from, once the precedence has been walked. Nothing was executed.')}
          </p>
          <p className="fs-def__route">
            <span className="fs-def__what">{t('completion mode')}</span> {config.completionMode}
            {' · '}
            {config.completionReason || config.completionSource}
          </p>
          <p className="fs-def__route">
            <span className="fs-def__what">{t('revision')}</span> <code className="fs-def__digest">{config.revision || '—'}</code>
          </p>
          <table className="fs-def__table">
            <thead>
              <tr>
                <th>{t('field')}</th>
                <th>{t('value')}</th>
                <th>{t('from')}</th>
              </tr>
            </thead>
            <tbody>
              {config.rows.map((row) => (
                <tr key={row.field}>
                  <td className="fs-def__cell-field">{row.field}</td>
                  <td className="fs-def__cell-value">
                    {row.value || '—'}
                    {row.discarded.map((lost, i) => (
                      <span key={i} className="fs-def__discarded">
                        {t('discarded')}: {lost.level} → {lost.value} — {lost.why}
                      </span>
                    ))}
                  </td>
                  <td className="fs-def__cell-from">{row.level || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {config.caveats.length > 0 && (
            <div>
              <p className="fs-def__route">{t('Caveats')}</p>
              {config.caveats.map((c, i) => (
                <p key={i} className="fs-def__caveat">
                  {c}
                </p>
              ))}
            </div>
          )}
          {config.degraded.length > 0 && (
            <div>
              <p className="fs-def__route">{t('Integrations that were not available')}</p>
              {config.degraded.map((d, i) => (
                <p key={i} className="fs-def__caveat">
                  {d}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}

export function Defs({ onUseAgent }: { onUseAgent: (slug: string) => void }) {
  const [data, setData] = useState<DefCatalogue | null>(null);
  const [profiles, setProfiles] = useState<Map<string, AgentProfile>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [toast, setToast] = useState<string | null>(null);
  const [effectiveFor, setEffectiveFor] = useState<string | null>(null);
  const [effective, setEffective] = useState<EffectiveConfig | null>(null);
  const [lintOpen, setLintOpen] = useState(false);

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
    try {
      // The profile fields are additive: a build whose profiles API is not
      // there yet still shows every definition, just without them.
      setProfiles(await loadAgentProfiles());
    } catch {
      setProfiles(new Map());
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const showEffective = useCallback(async (slug: string) => {
    setEffectiveFor(slug);
    setEffective(null);
    try {
      setEffective(await resolveEffective(slug));
    } catch (e) {
      setEffectiveFor(null);
      flash(
        e instanceof AgentProfileRefusal
          ? `${e.path}: ${e.message}`
          : t('Could not read the effective configuration'),
      );
    }
  }, []);

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
        <Button
          variant="secondary"
          size="sm"
          icon={ShieldAlert}
          label={t('Lint')}
          title={t('Legal-but-suspicious profile and workflow configurations')}
          onClick={() => setLintOpen(true)}
          testId="def-lint-open"
        />
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
            const profile = profiles.get(d.slug);
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
                  {profile?.defaultCompletionMode && (
                    <span className="fs-def__tag" title={t('How far it pushes when nothing overrides it. Depth, never permission.')}>
                      {profile.defaultCompletionMode}
                    </span>
                  )}
                </header>
                {d.description && <p className="fs-def__desc">{d.description}</p>}
                {where && <p className="fs-def__route">{where}</p>}
                {profile && (
                  <>
                    <Words label={t('can do')} words={profile.capabilities} />
                    <Words label={t('specialties')} words={profile.specialties} />
                    <Words label={t('tags')} words={profile.tags} />
                    <Words label={t('good for')} words={profile.preferredTasks} />
                    <Words label={t('avoids')} words={profile.avoidTasks} />
                    <p className="fs-def__route">
                      <span className="fs-def__what">{t('profiles')}</span>
                      {t('verification')} {profile.profiles.verification || '—'} · {t('context')}{' '}
                      {profile.profiles.context || '—'} · {t('budget')} {profile.profiles.budget || '—'} ·{' '}
                      {t('collaboration')} {profile.profiles.collaboration || '—'}
                    </p>
                    <p className="fs-def__route">
                      <span className="fs-def__what">{t('hands back')}</span>
                      {profile.profiles.output || t('no output contract declared')}
                    </p>
                    {profile.inherits.length > 0 && (
                      <p className="fs-def__route">
                        <span className="fs-def__what">{t('inherits')}</span> {profile.inherits.join(' → ')}
                      </p>
                    )}
                  </>
                )}
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
                {profile && (profile.capabilities.length > 0 || profile.defaultCompletionMode) && (
                  <p className="fs-def__caveat">
                    {t('A declared capability is not a tool and a completion mode is not a permission: what this agent may actually do is the list above.')}
                  </p>
                )}
                {d.caveats.map((c, i) => (
                  <p key={i} className="fs-def__caveat">
                    {c}
                  </p>
                ))}
                <div className="fs-def__actions">
                  <Button size="sm" variant="secondary" icon={Wrench} label={t('Use in a job')} onClick={() => onUseAgent(d.slug)} />
                  <Button
                    size="sm"
                    variant="ghost"
                    icon={SlidersHorizontal}
                    label={t('Effective configuration')}
                    onClick={() => void showEffective(d.slug)}
                  />
                  <code className="fs-def__usage">"agent": "{d.slug}"</code>
                  <IconButton icon={Copy} label={t('Copy the slug of {name}', { name: d.name })} size="sm" onClick={() => void copy(d.slug)} />
                </div>
              </article>
            );
          })}
        </div>
      )}
      {effectiveFor && (
        <Effective
          config={effective}
          name={effectiveFor}
          onClose={() => {
            setEffectiveFor(null);
            setEffective(null);
          }}
        />
      )}
      {toast && <Toast>{toast}</Toast>}
      {lintOpen && <ProfileLint onClose={() => setLintOpen(false)} />}
    </div>
  );
}
