import { Wrench } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { EmptyState, Skeleton } from '../../components';
import { listTools, setDisabledTools, TOOL_META, type ToolFlag } from '../../adapters/account';
import { t } from '../../i18n';
import { Toggle } from './fields';

/**
 * Built-in tools: which ones the agent may use, by family. The route
 * replaces the whole disabled list, so every change re-reads first and
 * posts a list rebuilt from fresh state, as the previous interface did.
 */
export function ToolsSection({ say }: { say: (t: string) => void }) {
  const [tools, setTools] = useState<ToolFlag[] | null>(null);
  const [denied, setDenied] = useState(false);
  const reload = () =>
    listTools()
      .then(setTools)
      .catch(() => {
        setDenied(true);
        setTools([]);
      });
  useEffect(() => {
    void reload();
  }, []);

  const groups = useMemo(() => {
    const by = new Map<string, ToolFlag[]>();
    for (const tool of tools ?? []) {
      const cat = TOOL_META[tool.id]?.cat ?? 'Other';
      by.set(cat, [...(by.get(cat) ?? []), tool]);
    }
    return [...by.entries()];
  }, [tools]);

  const apply = async (changes: { id: string; enabled: boolean }[]) => {
    try {
      const latest = await listTools();
      const state = new Map(latest.map((x) => [x.id, x.enabled]));
      for (const c of changes) state.set(c.id, c.enabled);
      await setDisabledTools([...state.entries()].filter(([, on]) => !on).map(([id]) => id));
      setTools([...state.entries()].map(([id, enabled]) => ({ id, enabled })));
    } catch {
      say(t('Could not update the tools.'));
      void reload();
    }
  };

  if (denied) return <EmptyState icon={Wrench} title={t('Administrators only')} body={t('This account cannot change the tools.')} />;

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-tools">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-tools" className="fs-set__title">{t('Tools')}</h2>
          <p className="fs-prose">{t('What the agent may use. Off here is off for every model and every chat; MCP servers live under Integrations.')}</p>
        </div>
      </header>
      {tools === null ? (
        <Skeleton label={t('Loading')} count={4} height="44px" />
      ) : tools.length === 0 ? (
        <p className="fs-set__help">{t('No tools.')}</p>
      ) : (
        groups.map(([cat, items]) => {
          const on = items.filter((x) => x.enabled).length;
          return (
            <div key={cat} className="fs-set__card">
              <h3 className="fs-set__card-title fs-tools__cat">
                <span>
                  {t(cat)} <span className="fs-set__help">{on}/{items.length}</span>
                </span>
                <Toggle id={`tools-${cat}`} checked={on === items.length} onChange={(v) => void apply(items.map((x) => ({ id: x.id, enabled: v })))} label={t('all')} />
              </h3>
              <ul className="fs-tools">
                {items.map((tool) => {
                  const meta = TOOL_META[tool.id];
                  return (
                    <li key={tool.id} className="fs-tools__row">
                      <span className="fs-tools__text">
                        <strong>{meta ? t(meta.name) : tool.id}</strong>
                        <span className="fs-set__help">
                          {meta ? t(meta.desc) : ''} <code className="fs-tools__id">{tool.id}</code>
                        </span>
                      </span>
                      <Toggle id={`tool-${tool.id}`} checked={tool.enabled} onChange={(v) => void apply([{ id: tool.id, enabled: v }])} />
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })
      )}
    </section>
  );
}
