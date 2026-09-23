import { ChevronDown, Download, Lock, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import {
  getLibraryRules,
  getProjectRules,
  installProjectRules,
  uninstallProjectRules,
  type LibraryRule,
  type ProjectRulesData,
} from '../../adapters/projectRules';
import { t, tn } from '../../i18n';
import '../settings.css';

/**
 * Rules: what this project's folder tells the agent on its own — files
 * under `.faustus/rules`, `.agents/rules`, `.claude/rules` or `.cursor/
 * rules`, discovered and shown exactly as `project_instructions.block()`
 * would inject them — plus a library of rules any project can install.
 */

function installedIdOf(rule: LibraryRule): string {
  return `${rule.area}-${rule.topic}`;
}

export function ProjectRules({ workspace, say }: { workspace: string; say: (m: string) => void }) {
  const [data, setData] = useState<ProjectRulesData | null>(null);
  const [library, setLibrary] = useState<LibraryRule[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [previewOpen, setPreviewOpen] = useState(false);
  const [language, setLanguage] = useState('');

  const reload = () => {
    if (!workspace) {
      setData(null);
      return;
    }
    Promise.all([getProjectRules(workspace), library ? Promise.resolve(library) : getLibraryRules()])
      .then(([d, lib]) => {
        setData(d);
        setLibrary(lib);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(reload, [workspace]);

  const installedIds = useMemo(() => new Set((data?.project_rules ?? []).map((r) => r.id)), [data]);
  const languages = data?.languages ?? [];

  const filteredLibrary = useMemo(() => {
    if (!library) return [];
    return library.filter((r) => {
      if (r.applies_to.length === 0) return true;
      if (!language) return languages.length === 0 || r.applies_to.some((l) => languages.includes(l));
      return r.applies_to.includes(language);
    });
  }, [library, language, languages]);

  const install = async (id: string) => {
    setBusy(id);
    try {
      const out = await installProjectRules(workspace, [id]);
      const row = out.results[0];
      say(row?.status === 'installed' ? t('Installed {id}', { id }) : row?.reason || t('Could not install {id}', { id }));
      reload();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const uninstall = async (id: string) => {
    setBusy(id);
    try {
      await uninstallProjectRules(workspace, [id]);
      say(t('Removed {id}', { id }));
      reload();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  if (!workspace) {
    return <EmptyState title={t('No folder linked')} body={t('Link a folder to this project to discover its rule files and install library rules into it.')} />;
  }
  if (error && !data) {
    return <EmptyState title={t('Rules could not be loaded')} body={error} primaryAction={{ label: t('Retry'), onClick: reload }} />;
  }

  return (
    <div className="fs-set__section" data-testid="project-rules">
      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Detected languages')}</h3>
        {data === null ? (
          <Skeleton label={t('Loading')} count={1} height="24px" />
        ) : languages.length === 0 ? (
          <p className="fs-set__help">{t('No language detected yet.')}</p>
        ) : (
          <div className="fs-intg__kinds">
            {languages.map((l) => (
              <span key={l} className="fs-chip" data-on>{l}</span>
            ))}
          </div>
        )}
        {data && !data.trusted && (
          <p className="fs-set__help" data-tone="warn">
            <Lock size={12} aria-hidden="true" /> {t('This folder is not yet trusted — its own rule files are discovered but not injected until you trust it.')}
          </p>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Installed in this project')}</h3>
        {data === null ? (
          <Skeleton label={t('Loading')} count={2} height="36px" />
        ) : (data.project_rules ?? []).length === 0 ? (
          <p className="fs-set__help">{t('No rule files found under .faustus/rules, .agents/rules, .claude/rules or .cursor/rules.')}</p>
        ) : (
          <ul className="fs-set__list">
            {data.project_rules.map((r) => (
              <li key={`${r.origin}/${r.id}`} className="fs-set__row" data-testid={`project-rule-${r.id}`}>
                <span className="fs-tools__text">
                  <strong>{r.id}</strong>
                  <span className="fs-set__help">
                    <code className="fs-tools__id">{r.origin}</code>
                    {r.distance > 0 ? ` · ${tn(r.distance, '{n} folder up', '{n} folders up')}` : ''}
                    {r.error ? ` · ${r.error}` : ''}
                  </span>
                </span>
                {installedIds.has(r.id) && (
                  <Button variant="ghost" size="sm" icon={Trash2} label={t('Remove')} loading={busy === r.id} onClick={() => void uninstall(r.id)} />
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Rule library')}</h3>
        <p className="fs-set__help">{t('Installing copies the rule into this project\'s own .faustus/rules folder — it changes nothing until then.')}</p>
        <div className="fs-intg__kinds">
          <button type="button" className="fs-chip" data-on={!language || undefined} onClick={() => setLanguage('')}>{t('Any language')}</button>
          {languages.map((l) => (
            <button key={l} type="button" className="fs-chip" data-on={language === l || undefined} onClick={() => setLanguage(language === l ? '' : l)}>
              {l}
            </button>
          ))}
        </div>
        {library === null ? (
          <Skeleton label={t('Loading')} count={3} height="44px" />
        ) : filteredLibrary.length === 0 ? (
          <p className="fs-set__help">{t('No library rule matches.')}</p>
        ) : (
          <ul className="fs-set__list">
            {filteredLibrary.map((rule) => {
              const id = installedIdOf(rule);
              const installed = installedIds.has(id);
              return (
                <li key={rule.id} className="fs-set__row" data-testid={`project-rule-library-${rule.id}`}>
                  <span className="fs-tools__text" style={{ flex: 1 }}>
                    <strong>{rule.title}</strong>
                    <span className="fs-set__help">
                      {rule.summary}
                      {rule.applies_to.length ? ` · ${rule.applies_to.join(', ')}` : ` · ${t('universal')}`}
                    </span>
                  </span>
                  <span className="fs-set__row-actions">
                    {installed ? (
                      <Button variant="ghost" size="sm" icon={Trash2} label={t('Remove')} loading={busy === id} onClick={() => void uninstall(id)} />
                    ) : (
                      <Button variant="secondary" size="sm" icon={Download} label={t('Install')} loading={busy === id} onClick={() => void install(rule.id)} />
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="fs-set__card">
        <button type="button" className="fs-set__card-title fs-tools__cat" onClick={() => setPreviewOpen((v) => !v)} data-testid="project-rules-preview-toggle">
          <span>{t('What the agent receives')}</span>
          <ChevronDown size={14} aria-hidden="true" style={{ transform: previewOpen ? 'rotate(180deg)' : undefined }} />
        </button>
        {previewOpen && (
          data?.block ? (
            <pre className="fs-context" data-testid="project-rules-preview">{data.block}</pre>
          ) : (
            <p className="fs-set__help">{t('Nothing is injected yet.')}</p>
          )
        )}
      </div>
    </div>
  );
}
