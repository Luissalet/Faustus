import { Check, Download, Search, SlidersHorizontal, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  explainSelector,
  getSelectorSettings,
  installLibrarySkills,
  listSkillLibrary,
  saveSelectorSettings,
  uninstallLibrarySkills,
  type LibrarySkill,
  type SelectorCandidate,
  type SelectorMode,
  type SelectorSettings,
} from '../../adapters/skillLibrary';
import { t, tn } from '../../i18n';
import '../settings.css';

/**
 * Skill library (`src/skill_library.py`): bundled `SKILL.md` procedures
 * anyone can install into their own store, plus the hybrid selector
 * (`src/skills_runtime/selector.py`) that decides which published skills
 * join a request and why.
 */

function LibraryCard({ skill, busy, onInstall, onUninstall }: { skill: LibrarySkill; busy: boolean; onInstall: () => void; onUninstall: () => void }) {
  if (skill.error) {
    return (
      <li className="fs-set__row" data-testid={`library-skill-${skill.slug}`}>
        <span className="fs-tools__text">
          <strong>{skill.slug}</strong>
          <span className="fs-set__help" data-tone="bad">{skill.error}</span>
        </span>
      </li>
    );
  }
  return (
    <li className="fs-set__row" data-testid={`library-skill-${skill.slug}`}>
      <span className="fs-tools__text" style={{ flex: 1 }}>
        <strong>{skill.name || skill.slug}</strong>
        {skill.category && <span className="fs-chip" data-on style={{ marginInlineStart: 6 }}>{skill.category}</span>}
        <span className="fs-set__help">
          {skill.description}
          {skill.words ? ` · ${t('{n} words', { n: skill.words })}` : ''}
        </span>
      </span>
      <span className="fs-set__row-actions">
        {skill.installed ? (
          <Button variant="ghost" size="sm" icon={Trash2} label={t('Uninstall')} loading={busy} onClick={onUninstall} testId={`library-uninstall-${skill.slug}`} />
        ) : (
          <Button variant="secondary" size="sm" icon={Download} label={t('Install')} loading={busy} onClick={onInstall} testId={`library-install-${skill.slug}`} />
        )}
      </span>
    </li>
  );
}

function SelectorPanel() {
  const [settings, setSettings] = useState<SelectorSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failedStatus, setFailedStatus] = useState<number | 'other' | null>(null);

  const [query, setQuery] = useState('');
  const [candidates, setCandidates] = useState<SelectorCandidate[] | null>(null);
  const [explainBusy, setExplainBusy] = useState(false);
  const [explainError, setExplainError] = useState('');

  const reload = () =>
    getSelectorSettings()
      .then((s) => { setSettings(s); setFailedStatus(null); })
      .catch((e: unknown) => setFailedStatus((e as { status?: number })?.status ?? 'other'));
  useEffect(() => {
    void reload();
  }, []);

  const save = async (patch: Partial<SelectorSettings>) => {
    if (!settings) return;
    const next = { ...settings, ...patch };
    setSettings(next);
    setSaving(true);
    setError(null);
    try {
      setSettings(await saveSelectorSettings(patch));
    } catch (e) {
      setError((e as Error).message);
      void reload();
    } finally {
      setSaving(false);
    }
  };

  const runExplain = async () => {
    if (!query.trim()) return;
    setExplainBusy(true);
    setExplainError('');
    try {
      setCandidates(await explainSelector(query.trim()));
    } catch (e) {
      setExplainError((e as Error).message);
    } finally {
      setExplainBusy(false);
    }
  };

  if (failedStatus === 401 || failedStatus === 403) {
    return <EmptyState tone="denied" title={t('Administrators only')} body={t('This account cannot change the selector.')} />;
  }

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title fs-tools__cat">
        <span><SlidersHorizontal size={14} aria-hidden="true" /> {t('Selector')}</span>
      </h3>
      <p className="fs-set__help">{t('How a query is ranked against every published skill: a semantic score, a lexical (word) score, whether the query touches the skill\'s trigger phrases, and a prior from past outcomes.')}</p>
      {settings === null ? (
        <Skeleton label={t('Loading')} count={2} height="36px" />
      ) : (
        <>
          <div className="fs-set__grid2">
            <div className="fs-set__field">
              <label className="fs-set__label" htmlFor="selector-mode">{t('Mode')}</label>
              <select id="selector-mode" className="fs-field" value={settings.mode} onChange={(e) => void save({ mode: e.target.value as SelectorMode })} disabled={saving}>
                <option value="hybrid">{t('Hybrid (semantic + lexical + trigger)')}</option>
                <option value="lexical">{t('Lexical only')}</option>
              </select>
            </div>
            <div className="fs-set__field">
              <label className="fs-set__label" htmlFor="selector-threshold">{t('Threshold')}</label>
              <input id="selector-threshold" className="fs-field" type="number" min={0} max={1} step={0.01} value={settings.threshold} onChange={(e) => void save({ threshold: Number(e.target.value) })} disabled={saving} />
            </div>
            <div className="fs-set__field">
              <label className="fs-set__label" htmlFor="selector-w-sem">{t('Weight: semantic')}</label>
              <input id="selector-w-sem" className="fs-field" type="number" min={0} step={0.05} value={settings.weights.semantic} onChange={(e) => void save({ weights: { ...settings.weights, semantic: Number(e.target.value) } })} disabled={saving} />
            </div>
            <div className="fs-set__field">
              <label className="fs-set__label" htmlFor="selector-w-lex">{t('Weight: lexical')}</label>
              <input id="selector-w-lex" className="fs-field" type="number" min={0} step={0.05} value={settings.weights.lexical} onChange={(e) => void save({ weights: { ...settings.weights, lexical: Number(e.target.value) } })} disabled={saving} />
            </div>
            <div className="fs-set__field">
              <label className="fs-set__label" htmlFor="selector-w-trig">{t('Weight: trigger')}</label>
              <input id="selector-w-trig" className="fs-field" type="number" min={0} step={0.05} value={settings.weights.trigger} onChange={(e) => void save({ weights: { ...settings.weights, trigger: Number(e.target.value) } })} disabled={saving} />
            </div>
          </div>
          {error && <p className="fs-set__help" data-tone="bad">{error}</p>}
        </>
      )}

      <h4 className="fs-set__card-title">{t('Explain')}</h4>
      <p className="fs-set__help">{t('Type a message and see how every skill would score against it right now — unfiltered by the threshold above.')}</p>
      <div className="fs-set__search">
        <Search size={14} aria-hidden="true" />
        <input
          type="search"
          placeholder={t('Type a message…')}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void runExplain(); }}
          aria-label={t('Query')}
          data-testid="selector-explain-query"
        />
      </div>
      <Button variant="secondary" size="sm" label={t('Explain')} loading={explainBusy} disabled={!query.trim() || explainBusy} onClick={() => void runExplain()} testId="selector-explain-run" />
      {explainError && <p className="fs-set__help" data-tone="bad">{explainError}</p>}
      {candidates && (
        candidates.length === 0 ? (
          <p className="fs-set__help">{t('No candidate skills.')}</p>
        ) : (
          <ul className="fs-set__list" data-testid="selector-explain-results">
            {candidates.map((c) => (
              <li key={c.name} className="fs-set__row">
                <span className="fs-tools__text">
                  <strong>{c.name}</strong>
                  <span className="fs-set__help">
                    {t('Semantic')} {c._selector.semantic.toFixed(2)} · {t('Lexical')} {c._selector.lexical.toFixed(2)} · {t('Trigger')} {c._selector.trigger.toFixed(2)} · {t('Prior')} {c._selector.prior.toFixed(2)} · {t('Score')} <strong>{c._selector.score.toFixed(3)}</strong>
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )
      )}
    </div>
  );
}

export function SkillLibraryPanel({ say }: { say: (t: string) => void }) {
  const [skills, setSkills] = useState<LibrarySkill[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('');
  const [busy, setBusy] = useState('');

  const reload = () =>
    listSkillLibrary()
      .then((rows) => { setSkills(rows); setError(null); })
      .catch((e: Error) => setError(e.message));
  useEffect(() => {
    void reload();
  }, []);

  const categories = useMemo(() => [...new Set((skills ?? []).map((s) => s.category).filter((c): c is string => !!c))].sort(), [skills]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (skills ?? []).filter((s) => {
      if (category && s.category !== category) return false;
      if (!needle) return true;
      return (s.name ?? s.slug).toLowerCase().includes(needle) || (s.description ?? '').toLowerCase().includes(needle);
    });
  }, [skills, query, category]);

  const install = async (slug: string) => {
    setBusy(slug);
    try {
      const out = await installLibrarySkills([slug]);
      const row = out.results[0];
      say(row?.status === 'installed' ? t('Installed {name}', { name: row.name ?? slug }) : row?.reason || t('Could not install {slug}', { slug }));
      await reload();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const uninstall = async (slug: string) => {
    setBusy(slug);
    try {
      await uninstallLibrarySkills([slug]);
      say(t('Uninstalled {slug}', { slug }));
      await reload();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const installCategory = async (cat: string) => {
    const slugs = filtered.filter((s) => s.category === cat && !s.installed).map((s) => s.slug);
    if (!slugs.length) return;
    setBusy(`cat:${cat}`);
    try {
      const out = await installLibrarySkills(slugs);
      const installed = out.results.filter((r) => r.status === 'installed').length;
      say(tn(installed, 'Installed {n} skill', 'Installed {n} skills'));
      await reload();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  if (error && !skills) {
    return <EmptyState title={t('The library could not be loaded')} body={error} primaryAction={{ label: t('Retry'), onClick: () => void reload() }} />;
  }

  return (
    <div className="fs-set__section" data-testid="skills-library">
      <div className="fs-set__card">
        <div className="fs-set__search">
          <Search size={14} aria-hidden="true" />
          <input type="search" placeholder={t('Search the library…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search')} data-testid="library-search" />
        </div>
        <div className="fs-intg__kinds">
          <button type="button" className="fs-chip" data-on={!category || undefined} onClick={() => setCategory('')}>{t('All categories')}</button>
          {categories.map((c) => (
            <button key={c} type="button" className="fs-chip" data-on={category === c || undefined} onClick={() => setCategory(category === c ? '' : c)}>
              {c}
            </button>
          ))}
        </div>
        {category && (
          <Button variant="secondary" size="sm" icon={Check} label={t('Install all of {category}', { category })} loading={busy === `cat:${category}`} onClick={() => void installCategory(category)} />
        )}
        {skills === null ? (
          <Skeleton label={t('Loading')} count={5} height="52px" />
        ) : filtered.length === 0 ? (
          <p className="fs-set__help">{t('No skills match.')}</p>
        ) : (
          <ul className="fs-set__list">
            {filtered.map((s) => (
              <LibraryCard key={s.slug} skill={s} busy={busy === s.slug} onInstall={() => void install(s.slug)} onUninstall={() => void uninstall(s.slug)} />
            ))}
          </ul>
        )}
      </div>

      <SelectorPanel />
    </div>
  );
}
