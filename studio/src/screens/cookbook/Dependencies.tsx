import { Check, ChevronDown, ChevronUp, Hammer, Package as PackageIcon, RefreshCw, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Dialog, Skeleton } from '../../components';
import { installLocalPackage, installSystemDeps, isLocal, listPackages, rebuildEngine, useCookbookState, type Package, type Server } from '../../adapters/cookbook';
import { pickRecipe, recipeCommands, recipesForBackend, RECIPE_BACKENDS, type Variant } from '../../lib/cookbook/recipes';
import { venvPython } from '../../lib/cookbook/serve';
import { t, tn } from '../../i18n';
import { launchServe, targetFor } from './actions';
import { CopyButton } from './parts';

/**
 * Dependencies: what each engine needs on the selected server and whether
 * it is there — installed, partial (a CPU-only wheel on a GPU box), or
 * missing — with the install as a task, the per-engine recipe (pip or
 * docker), the OS packages llama.cpp compiles with, and a rebuild lever.
 */

const CATEGORY_ORDER = ['LLM', 'Image', 'Tools', 'System'];

export function Dependencies({ server, hwBackend, say, highlight, onTask }: { server: Server | null; hwBackend: string; say: (m: string) => void; highlight?: string | null; onTask: () => void }) {
  const state = useCookbookState();
  const [packages, setPackages] = useState<Package[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(highlight ?? null);
  const [variant, setVariant] = useState<Variant>('pip');
  const [modelHint, setModelHint] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [rebuild, setRebuild] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setPackages(null);
      setError(null);
      try {
        const out = await listPackages(server, signal);
        if (!signal?.aborted) setPackages(out);
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      }
    },
    [server],
  );
  useEffect(() => {
    const ac = new AbortController();
    void load(ac.signal);
    return () => ac.abort();
  }, [load]);
  useEffect(() => {
    if (highlight) {
      setOpen(highlight);
      window.setTimeout(() => document.querySelector(`[data-pkg="${highlight}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 100);
    }
  }, [highlight]);

  const groups = useMemo(() => {
    const by = new Map<string, Package[]>();
    for (const p of packages ?? []) by.set(p.category, [...(by.get(p.category) ?? []), p]);
    return [...by.entries()].sort((a, b) => (CATEGORY_ORDER.indexOf(a[0]) === -1 ? 99 : CATEGORY_ORDER.indexOf(a[0])) - (CATEGORY_ORDER.indexOf(b[0]) === -1 ? 99 : CATEGORY_ORDER.indexOf(b[0])));
  }, [packages]);

  const target = useMemo(() => targetFor(state.env, server), [state.env, server]);
  const remote = Boolean(server && !isLocal(server));

  const install = async (p: Package, upgrade = false) => {
    setBusy(p.name);
    try {
      if (p.target === 'local' && !remote && !upgrade) {
        const r = await installLocalPackage(p.pip);
        if (r.ok === false) throw new Error(String(r.error || t('Install failed')));
        say(t('{name} installed', { name: p.name }));
        await load();
        return;
      }
      const win = target.platform === 'windows';
      const py = win ? 'python' : venvPython({ platform: target.platform, remoteHost: target.host, env: target.env, envPath: target.envPath, hwBackend, hostPlatform: state.env.hostPlatform });
      const pipArgs = p.pip.split(/\s+/).filter(Boolean).map((a) => (/[^A-Za-z0-9_.\-\[\]=<>,+:/@]/.test(a) ? `'${a.replace(/'/g, "'\\''")}'` : a)).join(' ');
      const cmd = `${py} -m pip install${upgrade ? ' -U' : ''} ${pipArgs}`;
      await launchServe({ shortName: `pip ${p.name}`, repo: p.name.replace(/\s+/g, '_'), cmd, target, hwBackend, dep: true });
      say(t('{verb} {name} on {where}…', { verb: upgrade ? t('Updating') : t('Installing'), name: p.name, where: target.name }));
      onTask();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const runRecipe = async (backend: string) => {
    const recipe = pickRecipe(backend, modelHint);
    const cmds = recipeCommands(recipe, variant);
    if (!cmds.length) return;
    setBusy(`recipe-${backend}`);
    try {
      const py = venvPython({ platform: target.platform, remoteHost: target.host, env: target.env, envPath: target.envPath, hwBackend, hostPlatform: state.env.hostPlatform });
      const cmd = cmds.map((line) => (target.env === 'venv' ? line.replace(/^python(?:3)?\s+-m\s+pip\b/, `${py} -m pip`) : line)).join(' && ');
      await launchServe({ shortName: `${backend} setup`, repo: `${backend} setup`, cmd, target, hwBackend, dep: true });
      say(t('Running {name} setup on {where}…', { name: backend, where: target.name }));
      onTask();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const sysDeps = async (pkgs: string[]) => {
    setBusy('sys');
    try {
      const r = await installSystemDeps(pkgs, server);
      if (r.ok === false) throw new Error(String(r.error || t('Install failed')));
      say(t('System packages installed'));
      await load();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const doRebuild = async (update: boolean) => {
    setBusy('rebuild');
    try {
      const r = await rebuildEngine(server, update);
      if (r.ok === false) throw new Error(String(r.error || t('Rebuild failed')));
      say(t('Cached llama.cpp build cleared; the next launch recompiles.'));
      setRebuild(false);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const missingSys = (packages ?? []).flatMap((p) => Object.entries(p.system_prereqs_status ?? {}).filter(([, ok]) => !ok).map(([k]) => k));
  const uniqueMissing = [...new Set(missingSys)];

  return (
    <div className="fs-ck__deps" data-testid="cookbook-deps">
      <div className="fs-ck__toolbar">
        <p className="fs-muted">
          {t('Checked on {where}', { where: target.name })}
          {target.envPath ? ` · ${target.env} ${target.envPath}` : ` · ${t('no venv set (Servers)')}`}
        </p>
        <span className="fs-spacer" />
        {uniqueMissing.length > 0 && <Button variant="secondary" size="sm" icon={Hammer} label={t('Install {list}', { list: uniqueMissing.join(', ') })} loading={busy === 'sys'} onClick={() => void sysDeps(uniqueMissing)} />}
        <Button variant="ghost" size="sm" icon={Hammer} label={t('Rebuild llama.cpp')} onClick={() => setRebuild(true)} />
        <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Recheck')} onClick={() => void load()} />
      </div>
      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!packages && <Skeleton label={t('Checking packages')} count={5} height="44px" radius="panel" />}
      {groups.map(([cat, list]) => (
        <section key={cat} className="fs-ck__group">
          <h3 className="fs-ck__h">{t(cat)}</h3>
          <ul className="fs-ck__list">
            {list.map((p) => {
              const isOpen = open === p.name;
              const hasRecipe = RECIPE_BACKENDS.has(p.name);
              const state2 = p.installed ? (p.partial ? 'partial' : 'ok') : 'missing';
              return (
                <li key={p.name} className="fs-ck__item" data-pkg={p.name} data-open={isOpen || undefined} data-highlight={highlight === p.name || undefined}>
                  <div className="fs-ck__item-row">
                    <span className="fs-ck__dep-state" data-state={state2} aria-label={state2 === 'ok' ? t('Installed') : state2 === 'partial' ? t('Partial') : t('Missing')}>
                      {state2 === 'ok' ? <Check size={12} aria-hidden="true" /> : state2 === 'partial' ? '!' : <X size={12} aria-hidden="true" />}
                    </span>
                    <button type="button" className="fs-ck__item-main" onClick={() => setOpen(isOpen ? null : p.name)} aria-expanded={isOpen}>
                      <PackageIcon size={14} aria-hidden="true" />
                      <span className="fs-ck__item-name">{p.name}</span>
                      {p.version && <span className="fs-muted">{String(p.version)}</span>}
                      <span className="fs-ck__item-meta">{p.desc}</span>
                      {isOpen ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                    </button>
                    {p.kind === 'system' ? (
                      !p.installed && <Button size="sm" variant="secondary" label={t('Install')} loading={busy === 'sys'} onClick={() => void sysDeps([p.name])} />
                    ) : p.installed ? (
                      (p.pip_update_available || p.partial) && <Button size="sm" variant="secondary" label={p.partial ? t('Reinstall') : t('Update')} loading={busy === p.name} onClick={() => void install(p, true)} />
                    ) : (
                      p.pip && <Button size="sm" variant="primary" label={t('Install')} loading={busy === p.name} onClick={() => void install(p)} />
                    )}
                  </div>
                  {isOpen && (
                    <div className="fs-ck__item-body">
                      {p.partial_reason && (
                        <p className="fs-notice" data-tone="warning">
                          {p.partial_reason}
                        </p>
                      )}
                      {p.install_hint && <p className="fs-muted">{p.install_hint}</p>}
                      {p.update_note && <p className="fs-muted">{p.update_note}</p>}
                      {p.pip && (
                        <p className="fs-muted">
                          pip: <code>{p.pip}</code>
                        </p>
                      )}
                      {p.system_prereqs && p.system_prereqs.length > 0 && (
                        <p className="fs-muted">
                          {t('Needs')}: {p.system_prereqs.map((s) => `${s} ${p.system_prereqs_status?.[s] ? '✓' : '✗'}`).join(' · ')}
                        </p>
                      )}
                      {hasRecipe && (
                        <div className="fs-ck__recipe">
                          <div className="fs-inline">
                            <div className="fs-seg" role="radiogroup" aria-label={t('Recipe variant')}>
                              {(['pip', 'docker'] as Variant[]).map((v) => (
                                <button key={v} type="button" role="radio" aria-checked={variant === v} onClick={() => setVariant(v)} disabled={v === 'docker' && !recipesForBackend(p.name).some((r) => r.variants.docker)}>
                                  {v}
                                </button>
                              ))}
                            </div>
                            {recipesForBackend(p.name).length > 1 && (
                              <select className="fs-field" value={modelHint} onChange={(e) => setModelHint(e.target.value)} aria-label={t('Recipe for')}>
                                <option value="">{t('Any model')}</option>
                                {recipesForBackend(p.name)
                                  .filter((r) => r.label !== recipesForBackend(p.name)[recipesForBackend(p.name).length - 1].label)
                                  .map((r) => (
                                    <option key={r.label} value={r.label}>
                                      {r.label}
                                    </option>
                                  ))}
                              </select>
                            )}
                          </div>
                          <pre className="fs-ck__recipe-cmds">{recipeCommands(pickRecipe(p.name, modelHint), variant).join('\n')}</pre>
                          <div className="fs-inline">
                            <Button size="sm" variant="primary" label={t('Run on {where}', { where: target.name })} loading={busy === `recipe-${p.name}`} onClick={() => void runRecipe(p.name)} />
                            <CopyButton text={recipeCommands(pickRecipe(p.name, modelHint), variant).join('\n')} say={say} />
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      {packages && (
        <p className="fs-muted">
          {tn(packages.filter((p) => p.installed).length, '{n} of {total} installed', '{n} of {total} installed#', { total: packages.length })}
        </p>
      )}

      {rebuild && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setRebuild(false);
          }}
          title={t('Rebuild llama.cpp on {where}?', { where: target.name })}
          description={t('This clears the cached llama-server build. The next launch recompiles or installs a matching prebuilt — and picks up CUDA/HIP if a toolchain is now present.')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setRebuild(false)} />
              <Button variant="secondary" size="sm" label={t('Update source and rebuild')} loading={busy === 'rebuild'} onClick={() => void doRebuild(true)} />
              <Button variant="primary" size="sm" label={t('Rebuild')} loading={busy === 'rebuild'} onClick={() => void doRebuild(false)} />
            </>
          }
        >
          <p className="fs-prose">{t('Nothing is downloaded now; it happens at the next llama.cpp launch.')}</p>
        </Dialog>
      )}
    </div>
  );
}
