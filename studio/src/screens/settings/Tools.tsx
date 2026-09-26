import { ChevronDown, Plus, Search, ShieldAlert, Sparkles, Star, Trash2, Wrench } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import { listTools, setDisabledTools, TOOL_META, type ToolFlag } from '../../adapters/account';
import {
  addLifecycleHooksPreset,
  emptyHook,
  lifecycleHooksLog,
  loadLifecycleHooks,
  matchSummary,
  saveLifecycleHooks,
  testLifecycleHooks,
  type Hook,
  type HookAction,
  type HookEvent,
  type HookLogEntry,
  type HookMatch,
  type HookRunResult,
  type LifecycleHooksData,
} from '../../adapters/lifecycleHooks';
import {
  dryRunTool,
  getToolDescriptor,
  listToolArgRules,
  listToolCatalog,
  loadFavoriteTools,
  saveFavoriteTools,
  saveToolArgRules,
  testToolArgRule,
  type ArgumentIssue,
  type CatalogEntry,
  type CatalogTool,
  type DryRunResult,
  type ToolArgRule,
  type ToolArgRuleAction,
  type ToolArgRuleOp,
  type ToolArgRuleTestResult,
} from '../../adapters/tools';
import { t } from '../../i18n';
import { Select, Toggle } from './fields';

/**
 * Built-in tools: which ones the agent may use, by family. The route
 * replaces the whole disabled list, so every change re-reads first and
 * posts a list rebuilt from fresh state, as the previous interface did.
 */
export function ToolsSection({ say }: { say: (t: string) => void }) {
  const [tools, setTools] = useState<ToolFlag[] | null>(null);
  // ACT-06: a 401/403 here means "sign in as an admin", not "nothing to
  // show" or "the server is down" — those get their own EmptyState tone
  // below instead of collapsing into the same generic "Administrators
  // only" text every OTHER failure (network error, 500) used to get too.
  const [failedStatus, setFailedStatus] = useState<number | 'other' | null>(null);
  const reload = () =>
    listTools()
      .then((t) => { setTools(t); setFailedStatus(null); })
      .catch((e: unknown) => {
        setFailedStatus((e as { status?: number })?.status ?? 'other');
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

  if (failedStatus === 401 || failedStatus === 403) {
    return <EmptyState tone="denied" title={t('Administrators only')} body={t('This account cannot change the tools.')} />;
  }
  if (failedStatus === 426) {
    return <EmptyState tone="incompatible" title={t('This client is out of date')} body={t('Update Faustus before changing the tools.')} />;
  }
  if (failedStatus === 'other') {
    return <EmptyState icon={Wrench} tone="error" title={t('Could not read the tools.')} body={t('GET /api/tools failed.')} primaryAction={{ label: t('Try again'), onClick: () => void reload() }} />;
  }

  return (
    <>
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
      <ToolCatalogPanel say={say} />
      <ArgRulesPanel say={say} />
      <LifecycleHooksPanel say={say} />
    </>
  );
}

/* ── Lifecycle hooks (src/lifecycle_hooks.py) — small automations attached to
 * a point in the agent's turn (session start, before/after a tool call, turn
 * end, before compaction): run a command and feed its output back to the
 * model, inject a fixed note, or warn. Same admin-only gate, same "replace
 * the whole list" save convention as the argument rules above. ── */

const HOOK_EVENT_LABEL: Record<HookEvent, string> = {
  session_start: 'Session start',
  turn_start: 'Turn start',
  pre_tool: 'Before a tool call',
  post_tool: 'After a tool call',
  turn_end: 'Turn end',
  pre_compact: 'Before compaction',
};

const HOOK_ACTION_LABEL: Record<HookAction, string> = {
  command: 'Run a command',
  inject: 'Inject a note',
  warn: 'Warn',
};

function HookRow({ hook, onToggle, onEdit, onDelete }: { hook: Hook; onToggle: (v: boolean) => void; onEdit: () => void; onDelete: () => void }) {
  const summary = matchSummary(hook.match);
  return (
    <li className="fs-set__row" data-testid={`lifecycle-hook-row-${hook.id}`}>
      <span className="fs-tools__text">
        <strong>{hook.name}</strong>
        <span className="fs-set__help">
          {t(HOOK_EVENT_LABEL[hook.event])} → {t(HOOK_ACTION_LABEL[hook.action])}
          {summary ? ` · ${summary}` : ''} · <code className="fs-tools__id">{hook.scope}</code>
        </span>
      </span>
      <span className="fs-set__row-actions">
        <Toggle id={`hook-${hook.id}`} checked={hook.enabled} onChange={onToggle} label={t('enabled')} />
        <Button variant="ghost" size="sm" label={t('Edit')} onClick={onEdit} />
        <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} onClick={onDelete} />
      </span>
    </li>
  );
}

function HookForm({ draft, setDraft, isNew, saving, formError, onSave, onCancel }: {
  draft: Hook;
  setDraft: (h: Hook) => void;
  isNew: boolean;
  saving: boolean;
  formError: string;
  onSave: () => void;
  onCancel: () => void;
}) {
  const setMatch = (key: keyof HookMatch, value: string) => setDraft({ ...draft, match: { ...draft.match, [key]: value || undefined } });
  return (
    <div className="fs-set__form" data-testid="lifecycle-hook-form">
      <div className="fs-set__grid2">
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-id">{t('Hook id')}</label>
          <input id="hook-id" className="fs-field" value={draft.id} disabled={!isNew} onChange={(e) => setDraft({ ...draft, id: e.target.value })} placeholder="format-py" />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-name">{t('Name')}</label>
          <input id="hook-name" className="fs-field" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-event">{t('Event')}</label>
          <Select id="hook-event" value={draft.event} options={Object.entries(HOOK_EVENT_LABEL).map(([value, label]) => ({ value, label: t(label) }))} onChange={(v) => setDraft({ ...draft, event: v as HookEvent })} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-action">{t('Action')}</label>
          <Select id="hook-action" value={draft.action} options={Object.entries(HOOK_ACTION_LABEL).map(([value, label]) => ({ value, label: t(label) }))} onChange={(v) => setDraft({ ...draft, action: v as HookAction })} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-scope">{t('Scope')}</label>
          <Select id="hook-scope" value={draft.scope} options={[{ value: 'global', label: t('Global') }, { value: 'project', label: t('This project only') }]} onChange={(v) => setDraft({ ...draft, scope: v as Hook['scope'] })} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-timeout">{t('Timeout (seconds)')}</label>
          <input id="hook-timeout" className="fs-field" type="number" min={1} max={120} value={draft.timeout_s ?? ''} placeholder={t('default')} onChange={(e) => setDraft({ ...draft, timeout_s: e.target.value ? Number(e.target.value) : null })} />
        </div>
      </div>
      <p className="fs-set__label">{t('Match (leave a field blank to match every call of this event; tool/path accept a | -separated list of globs)')}</p>
      <div className="fs-set__grid2">
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-match-tool">{t('Tool')}</label>
          <input id="hook-match-tool" className="fs-field" placeholder="edit_file|write_file" value={draft.match.tool ?? ''} onChange={(e) => setMatch('tool', e.target.value)} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-match-path">{t('Path glob')}</label>
          <input id="hook-match-path" className="fs-field" placeholder="*.py" value={draft.match.path ?? ''} onChange={(e) => setMatch('path', e.target.value)} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-match-command">{t('Command matches (regex)')}</label>
          <input id="hook-match-command" className="fs-field" value={draft.match.command ?? ''} onChange={(e) => setMatch('command', e.target.value)} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-match-text">{t('Message matches (regex)')}</label>
          <input id="hook-match-text" className="fs-field" value={draft.match.text ?? ''} onChange={(e) => setMatch('text', e.target.value)} />
        </div>
      </div>
      {draft.action === 'command' ? (
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-command">{t('Command ({placeholders} available)')}</label>
          <textarea id="hook-command" className="fs-field fs-set__pre" rows={2} value={draft.command ?? ''} onChange={(e) => setDraft({ ...draft, command: e.target.value })} />
        </div>
      ) : (
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="hook-text">{t('Text')}</label>
          <textarea id="hook-text" className="fs-field fs-set__pre" rows={2} value={draft.text ?? ''} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
        </div>
      )}
      <div className="fs-set__field">
        <label className="fs-set__label" htmlFor="hook-note">{t('Note (shown to the model)')}</label>
        <input id="hook-note" className="fs-field" value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} />
      </div>
      {formError && <p className="fs-set__help" data-tone="bad">{formError}</p>}
      <div className="fs-set__row-actions">
        <Button variant="primary" size="sm" label={t('Save hook')} onClick={onSave} loading={saving} disabled={saving} />
        <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onCancel} />
      </div>
    </div>
  );
}

function LifecycleHooksPanel({ say }: { say: (t: string) => void }) {
  const [data, setData] = useState<LifecycleHooksData | null>(null);
  const [failedStatus, setFailedStatus] = useState<number | 'other' | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Hook>(emptyHook());
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState('');
  const [presetBusy, setPresetBusy] = useState('');

  const [testEvent, setTestEvent] = useState<HookEvent>('post_tool');
  const [testTool, setTestTool] = useState('');
  const [testPath, setTestPath] = useState('');
  const [testCommand, setTestCommand] = useState('');
  const [testBusy, setTestBusy] = useState(false);
  const [testMatched, setTestMatched] = useState<string[] | null>(null);
  const [testRun, setTestRun] = useState<HookRunResult[] | null>(null);
  const [testError, setTestError] = useState('');

  const [logOpen, setLogOpen] = useState(false);
  const [log, setLog] = useState<HookLogEntry[] | null>(null);

  const reload = () =>
    loadLifecycleHooks()
      .then((d) => { setData(d); setFailedStatus(null); })
      .catch((e: unknown) => {
        setFailedStatus((e as { status?: number })?.status ?? 'other');
        setData({ hooks: [], presets: [], events: [], actions: [] });
      });
  useEffect(() => {
    void reload();
  }, []);

  const startAdd = () => {
    setEditingId('');
    setDraft(emptyHook());
    setFormError('');
  };
  const startEdit = (hook: Hook) => {
    setEditingId(hook.id);
    setDraft(hook);
    setFormError('');
  };
  const cancelEdit = () => {
    setEditingId(null);
    setDraft(emptyHook());
    setFormError('');
  };

  const persist = async (hooks: Hook[]): Promise<boolean> => {
    setSaving(true);
    setFormError('');
    try {
      const saved = await saveLifecycleHooks(hooks);
      setData((cur) => (cur ? { ...cur, hooks: saved } : cur));
      return true;
    } catch (e: unknown) {
      setFormError((e as Error)?.message || t('Could not save the hook.'));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const saveDraft = async () => {
    if (!data) return;
    const id = draft.id.trim();
    if (!id || !draft.name.trim()) {
      setFormError(t('Hook id and name are required.'));
      return;
    }
    const isNew = editingId === '';
    if (isNew && data.hooks.some((h) => h.id === id)) {
      setFormError(t('A hook with this id already exists.'));
      return;
    }
    const normalized: Hook = { ...draft, id };
    const next = isNew ? [...data.hooks, normalized] : data.hooks.map((h) => (h.id === editingId ? normalized : h));
    if (await persist(next)) {
      cancelEdit();
      say(t('Hook saved.'));
    }
  };

  const toggleHook = async (hook: Hook, enabled: boolean) => {
    if (!data) return;
    await persist(data.hooks.map((h) => (h.id === hook.id ? { ...h, enabled } : h)));
  };

  const removeHook = async (id: string) => {
    if (!data) return;
    await persist(data.hooks.filter((h) => h.id !== id));
  };

  const addPreset = async (presetId: string) => {
    setPresetBusy(presetId);
    try {
      const hooks = await addLifecycleHooksPreset(presetId);
      setData((cur) => (cur ? { ...cur, hooks } : cur));
      say(t('Preset added.'));
    } catch (e: unknown) {
      say((e as Error)?.message || t('Could not add the preset.'));
    } finally {
      setPresetBusy('');
    }
  };

  const runTry = async (run: boolean) => {
    setTestBusy(true);
    setTestError('');
    setTestMatched(null);
    setTestRun(null);
    try {
      const ctx: Record<string, unknown> = {};
      if (testTool.trim()) ctx.tool = testTool.trim();
      if (testPath.trim()) ctx.path = testPath.trim();
      if (testCommand.trim()) ctx.command = testCommand.trim();
      const out = await testLifecycleHooks(testEvent, ctx, run);
      if (out.matched) setTestMatched(out.matched);
      if (out.results) setTestRun(out.results);
    } catch (e: unknown) {
      setTestError((e as Error)?.message || t('Could not run the test.'));
    } finally {
      setTestBusy(false);
    }
  };

  const loadLog = () => {
    setLogOpen((v) => !v);
    if (!log) void lifecycleHooksLog(50).then(setLog).catch(() => setLog([]));
  };

  if (failedStatus === 401 || failedStatus === 403) {
    return <EmptyState tone="denied" title={t('Administrators only')} body={t('This account cannot change lifecycle hooks.')} />;
  }
  if (failedStatus === 'other') {
    return <EmptyState icon={Sparkles} tone="error" title={t('Could not read the lifecycle hooks.')} body={t('GET /api/lifecycle-hooks failed.')} primaryAction={{ label: t('Try again'), onClick: () => void reload() }} />;
  }

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-lifecycle-hooks">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-lifecycle-hooks" className="fs-set__title">{t('Lifecycle hooks')}</h2>
          <p className="fs-prose">{t('Small automations attached to a point in the agent\'s turn — run a command and feed its output back, inject a fixed note, or warn. A hook never denies or blocks a call by itself; that is what argument rules are for.')}</p>
        </div>
        {editingId === null && <Button variant="secondary" size="sm" icon={Plus} label={t('Add hook')} onClick={startAdd} />}
      </header>

      <div className="fs-set__card">
        {data === null ? (
          <Skeleton label={t('Loading')} count={2} height="44px" />
        ) : data.hooks.length === 0 && editingId === null ? (
          <p className="fs-set__help">{t('No lifecycle hooks yet — add one or start from a preset below.')}</p>
        ) : (
          <ul className="fs-set__list">
            {data.hooks.map((hook) => (
              <HookRow key={hook.id} hook={hook} onToggle={(v) => void toggleHook(hook, v)} onEdit={() => startEdit(hook)} onDelete={() => void removeHook(hook.id)} />
            ))}
          </ul>
        )}
        {editingId !== null && (
          <HookForm draft={draft} setDraft={setDraft} isNew={editingId === ''} saving={saving} formError={formError} onSave={() => void saveDraft()} onCancel={cancelEdit} />
        )}
      </div>

      {data && data.presets.length > 0 && (
        <div className="fs-set__card">
          <h3 className="fs-set__card-title">{t('Presets')}</h3>
          <p className="fs-set__help">{t('Adding a preset already installed does nothing — safe to click again.')}</p>
          <div className="fs-intg__kinds fs-hooks__presets">
            {data.presets.map((p) => (
              <Button
                key={p.preset_id}
                variant="secondary"
                size="sm"
                label={data.hooks.some((h) => h.id === p.preset_id) ? t('Installed') : p.description}
                disabled={data.hooks.some((h) => h.id === p.preset_id)}
                loading={presetBusy === p.preset_id}
                onClick={() => void addPreset(p.preset_id)}
                title={p.description}
              />
            ))}
          </div>
        </div>
      )}

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Try')}</h3>
        <p className="fs-set__help">{t('Check which hooks would fire for an event, or actually run them.')}</p>
        <div className="fs-set__grid2">
          <div className="fs-set__field">
            <label className="fs-set__label" htmlFor="hook-try-event">{t('Event')}</label>
            <Select id="hook-try-event" value={testEvent} options={Object.entries(HOOK_EVENT_LABEL).map(([value, label]) => ({ value, label: t(label) }))} onChange={(v) => setTestEvent(v as HookEvent)} />
          </div>
          <div className="fs-set__field">
            <label className="fs-set__label" htmlFor="hook-try-tool">{t('Tool')}</label>
            <input id="hook-try-tool" className="fs-field" value={testTool} onChange={(e) => setTestTool(e.target.value)} />
          </div>
          <div className="fs-set__field">
            <label className="fs-set__label" htmlFor="hook-try-path">{t('Path')}</label>
            <input id="hook-try-path" className="fs-field" value={testPath} onChange={(e) => setTestPath(e.target.value)} />
          </div>
          <div className="fs-set__field">
            <label className="fs-set__label" htmlFor="hook-try-command">{t('Command')}</label>
            <input id="hook-try-command" className="fs-field" value={testCommand} onChange={(e) => setTestCommand(e.target.value)} />
          </div>
        </div>
        <div className="fs-set__row-actions">
          <Button variant="secondary" size="sm" label={t('Show matches')} onClick={() => void runTry(false)} loading={testBusy} disabled={testBusy} testId="lifecycle-hook-try-match" />
          <Button variant="primary" size="sm" label={t('Run')} onClick={() => void runTry(true)} loading={testBusy} disabled={testBusy} testId="lifecycle-hook-try-run" />
        </div>
        {testError && <p className="fs-set__help" data-tone="bad">{testError}</p>}
        {testMatched && (
          <p className="fs-set__help" data-testid="lifecycle-hook-try-result">
            {testMatched.length ? t('Would fire: {ids}', { ids: testMatched.join(', ') }) : t('Nothing would fire.')}
          </p>
        )}
        {testRun && (
          <ul className="fs-set__list">
            {testRun.map((r, i) => (
              <li key={i} className="fs-set__row">
                <span className="fs-tools__text">
                  <strong>{r.name}</strong>
                  <span className="fs-set__help" data-tone={r.ok ? 'ok' : 'bad'}>
                    {r.ok ? t('ok') : t('failed')} · {r.duration_ms}ms{r.timed_out ? ` · ${t('timed out')}` : ''}
                  </span>
                  {r.output && <span className="fs-set__help">{r.output.slice(0, 400)}</span>}
                  {r.error && <span className="fs-set__help" data-tone="bad">{r.error.slice(0, 400)}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="fs-set__card">
        <button type="button" className="fs-set__card-title fs-tools__cat" onClick={loadLog} data-testid="lifecycle-hook-log-toggle">
          <span>{t('Recent runs')}</span>
          <ChevronDown size={14} aria-hidden="true" style={{ transform: logOpen ? 'rotate(180deg)' : undefined }} />
        </button>
        {logOpen && (
          log === null ? (
            <Skeleton label={t('Loading')} count={2} height="30px" />
          ) : log.length === 0 ? (
            <p className="fs-set__help">{t('No runs logged yet.')}</p>
          ) : (
            <ul className="fs-set__list">
              {log.map((r, i) => (
                <li key={i} className="fs-set__row">
                  <span className="fs-tools__text">
                    <strong>{r.name}</strong>
                    <span className="fs-set__help" data-tone={r.ok ? 'ok' : 'bad'}>
                      {new Date(r.ts * 1000).toLocaleString()} · {r.event} · {r.action} · {r.ok ? t('ok') : t('failed')}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )
        )}
      </div>
    </section>
  );
}

/* ── Argument rules (src/tool_arg_policy.py) — restrict a tool call by its
 * ARGUMENTS, not just its name: "web_fetch is fine, but only to these
 * domains", "write_file is fine, but only under /workspace". Separate card,
 * separate data source (`/api/tool-arg-rules`), same as the catalog above. */

const ARG_RULE_OPS: { value: ToolArgRuleOp; label: string }[] = [
  { value: 'equals', label: 'equals' },
  { value: 'one_of', label: 'one of' },
  { value: 'prefix', label: 'starts with' },
  { value: 'not_prefix', label: 'does not start with' },
  { value: 'regex', label: 'matches regex' },
  { value: 'max_len', label: 'max length' },
  { value: 'domain_in', label: 'domain is one of' },
];

const ARG_RULE_ACTIONS: { value: ToolArgRuleAction; label: string }[] = [
  { value: 'deny', label: 'Deny' },
  { value: 'ask', label: 'Ask for approval' },
];

/** `value` round-trips through a single text input: a list-shaped op
 * (one_of/domain_in) is comma-separated in the form and parsed back into an
 * array before saving; every other op stores/edits a plain string (max_len
 * is coerced to a number on save). */
function valueToText(op: ToolArgRuleOp, value: unknown): string {
  if (Array.isArray(value)) return value.join(', ');
  if (value === null || value === undefined) return '';
  return String(value);
}

function textToValue(op: ToolArgRuleOp, text: string): unknown {
  if (op === 'one_of' || op === 'domain_in') {
    return text.split(',').map((s) => s.trim()).filter(Boolean);
  }
  if (op === 'max_len') {
    const n = Number(text.trim());
    return Number.isFinite(n) ? n : text.trim();
  }
  return text;
}

function emptyRule(): ToolArgRule {
  return { id: '', tool: '', arg: '', op: 'prefix', value: '', action: 'deny', note: '' };
}

function ArgRulesPanel({ say }: { say: (t: string) => void }) {
  const [rules, setRules] = useState<ToolArgRule[] | null>(null);
  const [failedStatus, setFailedStatus] = useState<number | 'other' | null>(null);
  const [draft, setDraft] = useState<ToolArgRule>(emptyRule());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState('');

  const [testTool, setTestTool] = useState('');
  const [testArgsJson, setTestArgsJson] = useState('{}');
  const [testResult, setTestResult] = useState<ToolArgRuleTestResult | null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const [testError, setTestError] = useState('');

  const reload = () =>
    listToolArgRules()
      .then((r) => { setRules(r); setFailedStatus(null); })
      .catch((e: unknown) => {
        setFailedStatus((e as { status?: number })?.status ?? 'other');
        setRules([]);
      });
  useEffect(() => {
    void reload();
  }, []);

  const startEdit = (rule: ToolArgRule) => {
    setEditingId(rule.id);
    setDraft({ ...rule, value: valueToText(rule.op, rule.value) as unknown as ToolArgRule['value'] });
    setFormError('');
  };

  const startAdd = () => {
    setEditingId('');
    setDraft(emptyRule());
    setFormError('');
  };

  const cancelEdit = () => {
    setEditingId(null);
    setDraft(emptyRule());
    setFormError('');
  };

  const persist = async (next: ToolArgRule[]) => {
    setSaving(true);
    setFormError('');
    try {
      const saved = await saveToolArgRules(next);
      setRules(saved);
      // The Test box answers against the SAVED rules: a verdict computed
      // before this save no longer describes them.
      setTestResult(null);
      return true;
    } catch (e: unknown) {
      setFormError((e as Error)?.message || t('Could not save the rule.'));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const saveDraft = async () => {
    if (!rules) return;
    const id = draft.id.trim();
    if (!id || !draft.tool.trim() || !draft.arg.trim()) {
      setFormError(t('Rule id, tool and argument are required.'));
      return;
    }
    const normalized: ToolArgRule = { ...draft, id, value: textToValue(draft.op, String(draft.value ?? '')) };
    const isNew = editingId === '';
    if (isNew && rules.some((r) => r.id === id)) {
      setFormError(t('A rule with this id already exists.'));
      return;
    }
    const next = isNew ? [...rules, normalized] : rules.map((r) => (r.id === editingId ? normalized : r));
    if (await persist(next)) {
      cancelEdit();
      say(t('Argument rule saved.'));
    }
  };

  const remove = async (id: string) => {
    if (!rules) return;
    await persist(rules.filter((r) => r.id !== id));
  };

  const runTest = async () => {
    setTestBusy(true);
    setTestError('');
    setTestResult(null);
    try {
      const args = JSON.parse(testArgsJson || '{}');
      setTestResult(await testToolArgRule(testTool.trim(), args));
    } catch (e: unknown) {
      setTestError(e instanceof SyntaxError ? t('Arguments must be valid JSON.') : ((e as Error)?.message || t('Could not run the test.')));
    } finally {
      setTestBusy(false);
    }
  };

  if (failedStatus === 401 || failedStatus === 403) {
    return <EmptyState tone="denied" title={t('Administrators only')} body={t('This account cannot change argument rules.')} />;
  }
  if (failedStatus === 'other') {
    return <EmptyState icon={ShieldAlert} tone="error" title={t('Could not read the argument rules.')} body={t('GET /api/tool-arg-rules failed.')} primaryAction={{ label: t('Try again'), onClick: () => void reload() }} />;
  }

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-tool-arg-rules">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-tool-arg-rules" className="fs-set__title">{t('Argument rules')}</h2>
          <p className="fs-prose">{t('Restrict a tool by its arguments — a domain, a path prefix, a pattern — not just its name. The first matching rule denies the call or asks for your approval.')}</p>
        </div>
        {editingId === null && <Button variant="secondary" size="sm" icon={Plus} label={t('Add rule')} onClick={startAdd} />}
      </header>

      <div className="fs-set__card">
        {rules === null ? (
          <Skeleton label={t('Loading')} count={2} height="44px" />
        ) : rules.length === 0 && editingId === null ? (
          <p className="fs-set__help">{t('No argument rules. Every tool call is only gated by name.')}</p>
        ) : (
          <ul className="fs-set__list">
            {rules.map((rule) => (
              <li key={rule.id} className="fs-set__row">
                <span className="fs-tools__text">
                  <strong>{rule.id}</strong>
                  <span className="fs-set__help">
                    <code className="fs-tools__id">{rule.tool}</code> · {rule.arg} {ARG_RULE_OPS.find((o) => o.value === rule.op)?.label ?? rule.op}{' '}
                    <code className="fs-tools__id">{valueToText(rule.op, rule.value)}</code>
                    {' → '}{ARG_RULE_ACTIONS.find((a) => a.value === rule.action)?.label ?? rule.action}
                    {rule.note ? ` — ${rule.note}` : ''}
                  </span>
                </span>
                <span className="fs-set__row-actions">
                  <Button variant="ghost" size="sm" label={t('Edit')} onClick={() => startEdit(rule)} />
                  <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} onClick={() => void remove(rule.id)} />
                </span>
              </li>
            ))}
          </ul>
        )}

        {editingId !== null && (
          <div className="fs-set__form" data-testid="tool-arg-rule-form">
            <div className="fs-set__grid2">
              <div className="fs-set__field">
                <label className="fs-set__label" htmlFor="argrule-id">{t('Rule id')}</label>
                <input id="argrule-id" className="fs-field" value={draft.id} disabled={editingId !== ''} onChange={(e) => setDraft({ ...draft, id: e.target.value })} />
              </div>
              <div className="fs-set__field">
                <label className="fs-set__label" htmlFor="argrule-tool">{t('Tool (exact name or glob)')}</label>
                <input id="argrule-tool" className="fs-field" placeholder="mcp__github__*" value={draft.tool} onChange={(e) => setDraft({ ...draft, tool: e.target.value })} />
              </div>
              <div className="fs-set__field">
                <label className="fs-set__label" htmlFor="argrule-arg">{t('Argument (dotted path)')}</label>
                <input id="argrule-arg" className="fs-field" placeholder="options.path" value={draft.arg} onChange={(e) => setDraft({ ...draft, arg: e.target.value })} />
              </div>
              <div className="fs-set__field">
                <label className="fs-set__label" htmlFor="argrule-op">{t('Condition')}</label>
                <Select id="argrule-op" value={draft.op} options={ARG_RULE_OPS.map((o) => ({ value: o.value, label: t(o.label) }))} onChange={(v) => setDraft({ ...draft, op: v as ToolArgRuleOp })} />
              </div>
              <div className="fs-set__field">
                <label className="fs-set__label" htmlFor="argrule-value">{t('Value')}</label>
                <input id="argrule-value" className="fs-field" placeholder={draft.op === 'one_of' || draft.op === 'domain_in' ? 'a.com, b.com' : ''} value={String(draft.value ?? '')} onChange={(e) => setDraft({ ...draft, value: e.target.value as unknown as ToolArgRule['value'] })} />
              </div>
              <div className="fs-set__field">
                <label className="fs-set__label" htmlFor="argrule-action">{t('Action')}</label>
                <Select id="argrule-action" value={draft.action} options={ARG_RULE_ACTIONS.map((a) => ({ value: a.value, label: t(a.label) }))} onChange={(v) => setDraft({ ...draft, action: v as ToolArgRuleAction })} />
              </div>
            </div>
            <div className="fs-set__field">
              <label className="fs-set__label" htmlFor="argrule-note">{t('Note (shown to the model)')}</label>
              <input id="argrule-note" className="fs-field" value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} />
            </div>
            {formError && <p className="fs-set__help" data-tone="bad">{formError}</p>}
            <div className="fs-set__row-actions">
              <Button variant="primary" size="sm" label={t('Save rule')} onClick={() => void saveDraft()} loading={saving} disabled={saving} />
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={cancelEdit} />
            </div>
          </div>
        )}
      </div>

      <div className="fs-set__card">
        <h3 className="fs-set__card-title">{t('Test')}</h3>
        <p className="fs-set__help">{t('Check a tool and arguments against the SAVED rules — nothing runs.')}</p>
        <div className="fs-set__grid2">
          <div className="fs-set__field">
            <label className="fs-set__label" htmlFor="argrule-test-tool">{t('Tool')}</label>
            <input id="argrule-test-tool" className="fs-field" value={testTool} onChange={(e) => setTestTool(e.target.value)} />
          </div>
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="argrule-test-args">{t('Arguments (JSON)')}</label>
          <textarea id="argrule-test-args" className="fs-field fs-set__pre" rows={3} value={testArgsJson} onChange={(e) => setTestArgsJson(e.target.value)} />
        </div>
        <Button variant="secondary" size="sm" label={t('Test')} onClick={() => void runTest()} loading={testBusy} disabled={testBusy || !testTool.trim()} testId="tool-arg-rule-test-run" />
        {testError && <p className="fs-set__help" data-tone="bad">{testError}</p>}
        {testResult && (
          <p className="fs-set__help" data-tone={testResult.allowed ? 'ok' : 'bad'} data-testid="tool-arg-rule-test-result">
            {testResult.allowed
              ? t('Allowed.')
              : `${t('Blocked')} (${testResult.action}) — ${testResult.message ?? ''}`}
          </p>
        )}
      </div>
    </section>
  );
}

/* ── Catalog: one descriptor per tool (native, fence or live MCP), with a
 * safe "try arguments" dry-run (TOOL-01, TOOL-03 partial). Separate section,
 * separate data source (`src/tool_registry.py` via `/api/tools/catalog`) —
 * the enable/disable list above is untouched. ── */

const EXECUTOR_FILTERS = ['', 'native', 'fence', 'mcp'] as const;
type ExecutorFilter = (typeof EXECUTOR_FILTERS)[number];

const EXECUTOR_LABEL: Record<Exclude<ExecutorFilter, ''>, string> = {
  native: 'Native',
  fence: 'Fence',
  mcp: 'MCP',
};

const EFFECT_LABEL: Record<string, string> = {
  read: 'Read',
  write: 'Write',
  execute: 'Execute',
  external: 'External',
  sensitive: 'Sensitive',
  control: 'Control',
};

function matchesExecutor(tool: CatalogTool, filter: ExecutorFilter): boolean {
  if (!filter) return true;
  if (filter === 'mcp') return tool.executor.startsWith('mcp:');
  return tool.executor === filter;
}

function ToolCatalogPanel({ say }: { say: (t: string) => void }) {
  const [rows, setRows] = useState<CatalogTool[] | null>(null);
  const [failedStatus, setFailedStatus] = useState<number | 'other' | null>(null);
  const [query, setQuery] = useState('');
  const [effectFilter, setEffectFilter] = useState('');
  const [executorFilter, setExecutorFilter] = useState<ExecutorFilter>('');
  const [favorites, setFavorites] = useState<Set<string>>(() => loadFavoriteTools());
  const [selected, setSelected] = useState<string | null>(null);

  const load = () =>
    listToolCatalog()
      .then((d) => { setRows(d.tools); setFailedStatus(null); })
      .catch((e: unknown) => {
        setFailedStatus((e as { status?: number })?.status ?? 'other');
        setRows([]);
      });
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const effects = useMemo(() => [...new Set((rows ?? []).map((r) => r.effect_class))].sort(), [rows]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (rows ?? [])
      .filter((r) => !needle || r.name.toLowerCase().includes(needle) || r.description.toLowerCase().includes(needle))
      .filter((r) => !effectFilter || r.effect_class === effectFilter)
      .filter((r) => matchesExecutor(r, executorFilter))
      .sort((a, b) => {
        const fav = Number(favorites.has(b.name)) - Number(favorites.has(a.name));
        return fav || a.name.localeCompare(b.name);
      });
  }, [rows, query, effectFilter, executorFilter, favorites]);

  const toggleFavorite = (name: string) => {
    setFavorites((cur) => {
      const next = new Set(cur);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      saveFavoriteTools(next);
      return next;
    });
  };

  if (failedStatus === 401 || failedStatus === 403) {
    return <EmptyState tone="denied" title={t('Administrators only')} body={t('This account cannot see the tool catalog.')} />;
  }
  if (failedStatus === 426) {
    return <EmptyState tone="incompatible" title={t('This client is out of date')} body={t('Update Faustus before browsing the tool catalog.')} />;
  }
  if (failedStatus === 'other') {
    return <EmptyState icon={ShieldAlert} tone="error" title={t('Could not read the tool catalog.')} body={t('GET /api/tools/catalog failed.')} primaryAction={{ label: t('Try again'), onClick: () => void load() }} />;
  }

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-tool-catalog">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-tool-catalog" className="fs-set__title">{t('Tool catalog')}</h2>
          <p className="fs-prose">{t('Every tool the agent can be offered, with its schema, effects, timeout and MCP status in one place. Try arguments below without running anything.')}</p>
        </div>
      </header>

      <div className="fs-set__card">
        <div className="fs-set__search">
          <Search size={14} aria-hidden="true" />
          <input
            type="search"
            placeholder={t('Search tools…')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label={t('Search')}
            data-testid="tool-catalog-search"
          />
        </div>
        <div className="fs-intg__kinds">
          <button type="button" className="fs-chip" data-on={!effectFilter || undefined} onClick={() => setEffectFilter('')}>
            {t('All effects')}
          </button>
          {effects.map((effect) => (
            <button key={effect} type="button" className="fs-chip" data-on={effectFilter === effect || undefined} onClick={() => setEffectFilter(effectFilter === effect ? '' : effect)}>
              {t(EFFECT_LABEL[effect] ?? effect)}
            </button>
          ))}
        </div>
        <div className="fs-intg__kinds">
          {EXECUTOR_FILTERS.map((ex) => (
            <button key={ex || 'all'} type="button" className="fs-chip" data-on={executorFilter === ex || undefined} onClick={() => setExecutorFilter(ex)}>
              {ex ? t(EXECUTOR_LABEL[ex]) : t('All executors')}
            </button>
          ))}
        </div>

        {rows === null ? (
          <Skeleton label={t('Loading')} count={5} height="52px" />
        ) : filtered.length === 0 ? (
          <p className="fs-set__help">{t('No tools match.')}</p>
        ) : (
          <ul className="fs-tools">
            {filtered.map((tool) => (
              <li key={tool.name} className="fs-tools__row">
                <button
                  type="button"
                  className="fs-intg__main"
                  onClick={() => setSelected(selected === tool.name ? null : tool.name)}
                  data-testid={`tool-catalog-row-${tool.name}`}
                >
                  <span className="fs-intg__kind">{t(EFFECT_LABEL[tool.effect_class] ?? tool.effect_class)}</span>
                  <span className="fs-tools__text">
                    <strong>{tool.name} <span className="fs-set__help">v{tool.version}</span></strong>
                    <span className="fs-set__help">
                      {tool.description || t('No description.')} <code className="fs-tools__id">{tool.executor}</code>
                    </span>
                  </span>
                  {tool.mcp && (
                    <span className="fs-set__help" data-tone={tool.mcp.status === 'connected' ? 'ok' : tool.mcp.status === 'error' ? 'bad' : undefined}>
                      <span className="fs-intg__dot" data-on={tool.mcp.status === 'connected' || undefined} /> {tool.mcp.status}
                    </span>
                  )}
                </button>
                <IconStar on={favorites.has(tool.name)} onClick={() => toggleFavorite(tool.name)} label={t('Favorite')} />
              </li>
            ))}
          </ul>
        )}
      </div>

      {selected && <ToolTryPanel name={selected} onClose={() => setSelected(null)} say={say} />}
    </section>
  );
}

function IconStar({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button type="button" className="fs-chip" data-on={on || undefined} onClick={onClick} aria-pressed={on} title={label}>
      <Star size={14} aria-hidden="true" fill={on ? 'currentColor' : 'none'} />
    </button>
  );
}

/** One property's raw form value — kept as a string (or 'true'/'false' for a
 * boolean) until "Try arguments" coerces it, so a half-typed number never
 * fights the input while the user is still typing it. */
type FieldDraft = Record<string, string>;

function coerceArgument(schema: Record<string, unknown>, raw: string): { present: boolean; value: unknown } {
  if (raw === '') return { present: false, value: undefined };
  const types = schema.type;
  const type = Array.isArray(types) ? types[0] : types;
  if (type === 'boolean') return { present: true, value: raw === 'true' };
  if (type === 'integer' || type === 'number') {
    const n = Number(raw);
    return { present: true, value: Number.isNaN(n) ? raw : n };
  }
  if (type === 'array' || type === 'object') {
    try {
      return { present: true, value: JSON.parse(raw) };
    } catch {
      return { present: true, value: raw }; // let the backend's own validator name the type mismatch
    }
  }
  return { present: true, value: raw };
}

function errorsFor(field: string, errors: ArgumentIssue[]): ArgumentIssue[] {
  return errors.filter((e) => e.field === field);
}

function ToolTryPanel({ name, onClose, say }: { name: string; onClose: () => void; say: (t: string) => void }) {
  const [entry, setEntry] = useState<CatalogEntry | null>(null);
  const [draft, setDraft] = useState<FieldDraft>({});
  const [rawJson, setRawJson] = useState('{}');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DryRunResult | null>(null);
  const [loadError, setLoadError] = useState('');

  useEffect(() => {
    setEntry(null);
    setResult(null);
    setDraft({});
    getToolDescriptor(name)
      .then(setEntry)
      .catch(() => setLoadError(t('Could not load this tool.')));
  }, [name]);

  const properties = (entry?.tool.input_schema.properties ?? {}) as Record<string, Record<string, unknown>>;
  const required = new Set((entry?.tool.input_schema.required as string[] | undefined) ?? []);
  const propertyNames = Object.keys(properties);
  const hasSchema = propertyNames.length > 0;

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      let args: Record<string, unknown>;
      if (hasSchema) {
        args = {};
        for (const key of propertyNames) {
          const { present, value } = coerceArgument(properties[key] ?? {}, draft[key] ?? '');
          if (present) args[key] = value;
        }
      } else {
        args = JSON.parse(rawJson || '{}');
      }
      setResult(await dryRunTool(name, args));
    } catch {
      say(t('Could not run the dry-run check.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-set__card" data-testid="tool-try-panel">
      <h3 className="fs-set__card-title fs-tools__cat">
        <span>{t('Try arguments')} — <code className="fs-tools__id">{name}</code></span>
        <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} />
      </h3>
      <p className="fs-set__help">{t('Checks your arguments against the schema and shows what would be rejected or auto-corrected. Nothing runs.')}</p>

      {loadError ? (
        <p className="fs-set__help" data-tone="bad">{loadError}</p>
      ) : !entry ? (
        <Skeleton label={t('Loading')} count={3} height="36px" />
      ) : (
        <>
          {hasSchema ? (
            <div className="fs-tools">
              {propertyNames.map((key) => {
                const prop = properties[key];
                const fieldErrors = errorsFor(key, result?.errors ?? []);
                const repaired = result?.repairs.find((r) => r.field === key);
                return (
                  <div key={key} className="fs-tools__row">
                    <span className="fs-tools__text" style={{ flex: 1 }}>
                      <strong>
                        {key}
                        {required.has(key) ? ' *' : ''} <span className="fs-set__help">{String(prop.type ?? '')}</span>
                      </strong>
                      {prop.type === 'boolean' ? (
                        <select className="fs-field" value={draft[key] ?? ''} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}>
                          <option value="">{t('unset')}</option>
                          <option value="true">true</option>
                          <option value="false">false</option>
                        </select>
                      ) : Array.isArray(prop.enum) ? (
                        <select className="fs-field" value={draft[key] ?? ''} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}>
                          <option value="">{t('unset')}</option>
                          {(prop.enum as unknown[]).map((v) => (
                            <option key={String(v)} value={String(v)}>{String(v)}</option>
                          ))}
                        </select>
                      ) : (
                        <input
                          className="fs-field"
                          type="text"
                          value={draft[key] ?? ''}
                          placeholder={String(prop.description ?? '')}
                          onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                        />
                      )}
                      {fieldErrors.map((err, i) => (
                        <span key={i} className="fs-set__help" data-tone="bad">{err.detail}</span>
                      ))}
                      {repaired && (
                        <span className="fs-set__help" data-tone="ok">
                          {t('Auto-corrected to')} {JSON.stringify(repaired.to)}
                        </span>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <>
              <p className="fs-set__help">{t('This tool publishes no JSON schema — enter raw JSON arguments.')}</p>
              <textarea
                className="fs-field fs-set__pre"
                rows={4}
                value={rawJson}
                onChange={(e) => setRawJson(e.target.value)}
                aria-label={t('Arguments (JSON)')}
              />
            </>
          )}

          <Button variant="primary" label={t('Try arguments')} onClick={() => void run()} loading={busy} disabled={busy} testId="tool-try-run" />

          {result && (
            <p className="fs-set__help" data-tone={result.ok ? 'ok' : 'bad'} data-testid="tool-try-result">
              {result.ok ? t('Valid arguments.') : t('Rejected by the schema.')}
              {!result.schema_available && ` (${t('no schema to check')})`}
            </p>
          )}
        </>
      )}
    </div>
  );
}
