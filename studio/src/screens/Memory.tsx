import {
  Brain,
  Check,
  CheckSquare,
  Download,
  FileUp,
  Pin,
  PinOff,
  Plus,
  Search,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  Wand2,
  X,
  Zap,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Skeleton, Toast } from '../components';
import { listSessions, type ChatSession } from '../adapters/chat';
import { relativeTime } from '../adapters/home';
import {
  addMemory,
  addRule,
  auditMemories,
  CATEGORY_LABEL,
  curateRules,
  deleteMemory,
  deleteRule,
  exportMemories,
  extractFromSession,
  getPref,
  importFromFile,
  listMemories,
  listRules,
  MEMORY_CATEGORIES,
  pinMemory,
  previewPack,
  RULE_LEVELS,
  ruleFeedback,
  setPref,
  updateMemory,
  type CuratorReport,
  type ImportSuggestion,
  type LearnedRule,
  type Memory,
  type RuleStats,
} from '../adapters/memory';
import './projects.css';
import './memory.css';
import { t, tn } from '../i18n';

/**
 * Memoria (the previous interface's Brain modal, `/memory`).
 *
 * Two stores, same page: the facts the assistant keeps about you
 * (`/api/memory`: add, edit in place, pin, delete, search, category, sort,
 * select several, tidy with the model, suggestions from a conversation or a
 * file, export) and the learned rules the agent scores by outcome
 * (`/api/memory-engine`: add, helpful/harmful, delete, curator, the pack
 * the model actually sees). The two switches are the same preferences.
 */

type Sort = 'newest' | 'oldest' | 'alpha' | 'uses';
const SORTS: { value: Sort; label: string }[] = [
  { value: 'newest', label: 'Newest first' },
  { value: 'oldest', label: 'Oldest first' },
  { value: 'alpha', label: 'Alphabetical' },
  { value: 'uses', label: 'Most used' },
];

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ── One memory row: click the text to edit it in place ── */

function MemoryRow({ memory, selecting, selected, onToggle, onPin, onDelete, onSave }: { memory: Memory; selecting: boolean; selected: boolean; onToggle: () => void; onPin: () => void; onDelete: () => void; onSave: (text: string, category: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(memory.text);
  const [category, setCategory] = useState(memory.category);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!editing) {
      setText(memory.text);
      setCategory(memory.category);
    }
  }, [memory, editing]);

  const commit = async () => {
    const t = text.trim();
    if (!t) return;
    if (t === memory.text && category === memory.category) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onSave(t, category);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <article className="fs-mem" data-pinned={memory.pinned || undefined} data-selected={selected || undefined} data-testid="memory-row">
      {selecting && <input type="checkbox" className="fs-mem__check" checked={selected} onChange={onToggle} aria-label={`Seleccionar: ${memory.text.slice(0, 40)}`} />}
      <div className="fs-mem__main">
        {editing ? (
          <div className="fs-mem__edit">
            <textarea
              className="fs-mem__textarea"
              value={text}
              rows={2}
              autoFocus
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void commit();
                } else if (e.key === 'Escape') setEditing(false);
              }}
            />
            <div className="fs-mem__edit-row">
              <select className="fs-field" value={category} onChange={(e) => setCategory(e.target.value)} aria-label={t('Category')}>
                {MEMORY_CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {t(CATEGORY_LABEL[c])}
                  </option>
                ))}
              </select>
              <span className="fs-mem__spacer" />
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setEditing(false)} />
              <Button variant="primary" size="sm" label={t('Save')} loading={saving} onClick={() => void commit()} />
            </div>
          </div>
        ) : (
          <button type="button" className="fs-mem__text" onClick={() => !selecting && setEditing(true)} title={t('Edit')}>
            {memory.text}
          </button>
        )}
        <div className="fs-mem__meta">
          <span className="fs-mem__cat" data-cat={memory.category}>
            {CATEGORY_LABEL[memory.category] ? t(CATEGORY_LABEL[memory.category]) : memory.category}
          </span>
          <span>{memory.source === 'user' ? t('yours') : memory.source === 'auto' ? t('extracted') : memory.source}</span>
          {memory.timestamp > 0 && <span>{relativeTime(new Date(memory.timestamp * 1000).toISOString())}</span>}
          {memory.uses > 0 && <span>{tn(memory.uses, '{n} use', '{n} uses')}</span>}
          {memory.sessionId &&
            (/^[0-9a-f-]{32,36}$/i.test(memory.sessionId) ? (
              <Link to={`/studio?s=${encodeURIComponent(memory.sessionId)}`} className="fs-mem__session">
                conversación
              </Link>
            ) : (
              /* Older extractions stored the conversation's name here, not its id. */
              <span className="fs-mem__origin" title={memory.sessionId}>
                de «{memory.sessionId.length > 48 ? `${memory.sessionId.slice(0, 48)}…` : memory.sessionId}»
              </span>
            ))}
        </div>
      </div>
      {!selecting && (
        <div className="fs-mem__actions">
          <IconButton icon={memory.pinned ? PinOff : Pin} label={memory.pinned ? t('Unpin (stops going in the context every time)') : t('Pin (goes in the context every time)')} size="sm" onClick={onPin} />
          <IconButton icon={Trash2} label={t('Delete')} size="sm" onClick={onDelete} />
        </div>
      )}
    </article>
  );
}

/* ── Suggestions (from a conversation or a file): pick which to keep ── */

function SuggestionsDialog({ title, items, onClose, onSave }: { title: string; items: ImportSuggestion[]; onClose: () => void; onSave: (chosen: ImportSuggestion[]) => Promise<void> }) {
  const [chosen, setChosen] = useState<Set<number>>(() => new Set(items.map((_, i) => i)));
  const [saving, setSaving] = useState(false);
  const toggle = (i: number) =>
    setChosen((cur) => {
      const next = new Set(cur);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={title}
      description={items.length ? `${items.length} sugerencia${items.length === 1 ? '' : 's'}. Desmarca lo que no quieras guardar.` : t('Nothing came out worth saving.')}
      testId="memory-suggestions"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} />
          {items.length > 0 && (
            <Button
              variant="primary"
              size="sm"
              label={t('Save {n}', { n: chosen.size })}
              disabled={chosen.size === 0}
              loading={saving}
              onClick={async () => {
                setSaving(true);
                try {
                  await onSave(items.filter((_, i) => chosen.has(i)));
                } finally {
                  setSaving(false);
                }
              }}
            />
          )}
        </>
      }
    >
      <div className="fs-mem__suggestions">
        {items.map((s, i) => (
          <label key={i} className="fs-mem__suggestion" data-on={chosen.has(i) || undefined}>
            <input type="checkbox" checked={chosen.has(i)} onChange={() => toggle(i)} />
            <span>{s.text}</span>
            <span className="fs-mem__cat" data-cat={s.category}>
              {CATEGORY_LABEL[s.category] ? t(CATEGORY_LABEL[s.category]) : s.category}
            </span>
          </label>
        ))}
      </div>
    </Dialog>
  );
}

/* ── Learned rules ── */

function RuleRow({ rule, onFeedback, onDelete }: { rule: LearnedRule; onFeedback: (kind: 'helpful' | 'harmful') => void; onDelete: () => void }) {
  const pct = Math.max(0, Math.min(100, Math.round(rule.effectiveScore * 100)));
  const harm = Math.round(rule.harmfulRatio * 100);
  return (
    <article className="fs-rule" data-status={rule.status} data-testid="rule-row">
      <div className="fs-rule__main">
        {rule.status === 'anti_pattern' && <span className="fs-rule__avoid">EVITAR</span>}
        <span className="fs-rule__text">{rule.text}</span>
      </div>
      <div className="fs-rule__meta">
        <span className="fs-rule__chip" data-level={rule.level}>
          {rule.level}
        </span>
        <span className="fs-rule__chip">{rule.maturity}</span>
        {rule.trustClass && <span className="fs-rule__trust">{rule.trustClass}</span>}
        <span className="fs-rule__score" title={t('Effective score: confidence × freshness + helpful − 4 × harmful')}>
          <span className="fs-rule__bar" aria-hidden="true">
            <span style={{ inlineSize: `${pct}%` }} />
          </span>
          {rule.effectiveScore.toFixed(2)}
        </span>
        {harm > 0 && <span className="fs-rule__harm">{harm} % dañina</span>}
        <span className="fs-rule__actions">
          <IconButton icon={ThumbsUp} label={t('This rule helped')} size="sm" onClick={() => onFeedback('helpful')} />
          <IconButton icon={ThumbsDown} label={t('This rule did harm')} size="sm" onClick={() => onFeedback('harmful')} />
          <IconButton icon={X} label={t('Delete the rule')} size="sm" onClick={onDelete} />
        </span>
      </div>
    </article>
  );
}

function LearnedRules({ say }: { say: (text: string) => void }) {
  const [rules, setRules] = useState<LearnedRule[] | null>(null);
  const [stats, setStats] = useState<RuleStats | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | 'active' | 'anti'>('all');
  const [text, setText] = useState('');
  const [level, setLevel] = useState<string>('procedural');
  const [report, setReport] = useState<CuratorReport | null>(null);
  const [pack, setPack] = useState<{ text: string; chars: number; budget: number; degraded: boolean } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback((signal?: AbortSignal) => {
    listRules(signal)
      .then((r) => {
        setRules(r.rules);
        setStats(r.stats);
        setFailed(null);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name === 'AbortError') return;
        setRules([]);
        setFailed((err as { status?: number })?.status === 403 ? t('Only the administrator sees the learned rules.') : t('Could not read the learned rules.'));
      });
  }, []);

  useEffect(() => {
    const c = new AbortController();
    load(c.signal);
    return () => c.abort();
  }, [load]);

  const sorted = useMemo(() => {
    const rank: Record<string, number> = { active: 0, anti_pattern: 1, deprecated: 2 };
    const all = (rules ?? []).slice().sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3) || b.effectiveScore - a.effectiveScore || a.id.localeCompare(b.id));
    if (filter === 'active') return all.filter((r) => r.status === 'active');
    if (filter === 'anti') return all.filter((r) => r.status === 'anti_pattern');
    return all;
  }, [rules, filter]);

  const replace = (r: LearnedRule) => setRules((cur) => (cur ? cur.map((x) => (x.id === r.id ? r : x)) : cur));

  const add = async () => {
    const value = text.trim();
    if (!value) return;
    setBusy('add');
    try {
      const r = await addRule(value, level);
      setRules((cur) => [r, ...(cur ?? [])]);
      setText('');
      load();
    } catch (err) {
      say((err as Error).message || t('Could not add the rule.'));
    } finally {
      setBusy(null);
    }
  };

  const all = rules ?? [];
  const activeCount = all.filter((r) => r.status === 'active').length;
  const antiCount = all.filter((r) => r.status === 'anti_pattern').length;

  return (
    <section className="fs-rules" aria-labelledby="fs-rules-title">
      <header className="fs-rules__head">
        <div>
          <h2 id="fs-rules-title" className="fs-rules__title">
            {t('Learned rules')} <span className="fs-rules__count">{all.length}</span>
          </h2>
          <p className="fs-prose">
            {t('Outcome-scored rules the agent learns and forgets on its own: if one does harm several times, it is inverted into an antipattern.')}
            {stats?.semanticLane ? t(' With a semantic lane.') : ''}
          </p>
        </div>
        <div className="fs-rules__tools">
          <Button
            variant="secondary"
            size="sm"
            icon={Wand2}
            label={t('Run the curator')}
            loading={busy === 'curate'}
            onClick={async () => {
              setBusy('curate');
              try {
                setReport(await curateRules());
                load();
              } catch {
                say(t('The curator failed.'));
              } finally {
                setBusy(null);
              }
            }}
          />
          <Button
            variant="ghost"
            size="sm"
            label={t('See the pack')}
            loading={busy === 'pack'}
            onClick={async () => {
              setBusy('pack');
              try {
                setPack(await previewPack());
              } catch {
                say(t('Could not build the pack.'));
              } finally {
                setBusy(null);
              }
            }}
          />
        </div>
      </header>

      {report && (
        <p className="fs-rules__report" role="status">
          {t('Curator')}: <b>{report.deduped}</b> {t('duplicates')} · <b>{report.inverted}</b> {t('inverted')} · <b>{report.promoted}</b> {t('promoted')} · <b>{report.demoted}</b> {t('demoted')} · <b>{report.pruned}</b> {t('pruned')} · <b>{report.totalActive}</b> {t('active')}
        </p>
      )}

      <div className="fs-rules__filters" role="group" aria-label={t('Filter rules')}>
        <button type="button" className="fs-chip" data-on={filter === 'all' || undefined} onClick={() => setFilter('all')}>
          Todas · {all.length}
        </button>
        <button type="button" className="fs-chip" data-on={filter === 'active' || undefined} onClick={() => setFilter('active')}>
          Activas · {activeCount}
        </button>
        <button type="button" className="fs-chip" data-on={filter === 'anti' || undefined} onClick={() => setFilter('anti')}>
          Antipatrones · {antiCount}
        </button>
      </div>

      {failed && <p className="fs-rules__error">{failed}</p>}
      {!rules && !failed && <Skeleton label={t('Loading rules')} count={3} height="48px" />}
      {rules && !failed && sorted.length === 0 && <p className="fs-rules__empty">{all.length ? t('Nothing with that filter.') : t('No rules yet: add one below or let the agent learn from outcomes.')}</p>}
      {sorted.length > 0 && (
        <div className="fs-rules__list">
          {sorted.map((r) => (
            <RuleRow
              key={r.id}
              rule={r}
              onFeedback={(kind) =>
                void ruleFeedback(r.id, kind)
                  .then((u) => {
                    replace(u);
                    say(kind === 'helpful' ? t('Marked as helpful.') : t('Marked as harmful.'));
                  })
                  .catch(() => say(t('Could not save the rating.')))
              }
              onDelete={() =>
                void deleteRule(r.id)
                  .then(() => setRules((cur) => (cur ? cur.filter((x) => x.id !== r.id) : cur)))
                  .catch(() => say(t('Could not delete the rule.')))
              }
            />
          ))}
        </div>
      )}

      <form
        className="fs-rules__add"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <input type="text" className="fs-field" placeholder={t('A new rule, written by you (maximum confidence)…')} value={text} onChange={(e) => setText(e.target.value)} />
        <select className="fs-field" value={level} onChange={(e) => setLevel(e.target.value)} aria-label={t('Level')}>
          {RULE_LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
        <Button type="submit" variant="secondary" size="sm" icon={Plus} label={t('Add')} disabled={!text.trim()} loading={busy === 'add'} />
      </form>

      {pack && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setPack(null);
          }}
          title={t('What the model sees')}
          description={`${t('{a} of {b} characters', { a: pack.chars, b: pack.budget })}${pack.degraded ? t(' · degraded') : ''}`}
          footer={<Button variant="ghost" size="sm" label={t('Close')} onClick={() => setPack(null)} />}
        >
          <pre className="fs-rules__pack">{pack.text || t('(empty)')}</pre>
        </Dialog>
      )}
    </section>
  );
}

/* ── Screen ── */

export function MemoryScreen() {
  const [memories, setMemories] = useState<Memory[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState<string>('all');
  const [sort, setSort] = useState<Sort>('newest');
  const [newText, setNewText] = useState('');
  const [newCategory, setNewCategory] = useState<string>('fact');
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [autoExtract, setAutoExtract] = useState<boolean | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<{ title: string; items: ImportSuggestion[] } | null>(null);
  const [sessions, setSessions] = useState<ChatSession[] | null>(null);
  const [extractOpen, setExtractOpen] = useState(false);
  const [extractSession, setExtractSession] = useState('');
  const [confirmBulk, setConfirmBulk] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const say = useCallback((text: string) => {
    setNotice(text);
    window.setTimeout(() => setNotice((cur) => (cur === text ? null : cur)), 4000);
  }, []);

  const load = useCallback((signal?: AbortSignal) => {
    return listMemories(signal)
      .then((list) => {
        setMemories(list);
        setFailed(false);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== 'AbortError') setFailed(true);
      });
  }, []);

  useEffect(() => {
    const c = new AbortController();
    void load(c.signal);
    void getPref<boolean>('memory_enabled', true).then((v) => setEnabled(v !== false));
    void getPref<boolean>('auto_memory', false).then((v) => setAutoExtract(v === true));
    return () => c.abort();
  }, [load]);

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    (memories ?? []).forEach((m) => counts.set(m.category, (counts.get(m.category) ?? 0) + 1));
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [memories]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = (memories ?? []).filter((m) => (!q || m.text.toLowerCase().includes(q)) && (category === 'all' || m.category === category));
    list = list.slice();
    if (sort === 'newest') list.sort((a, b) => b.timestamp - a.timestamp);
    else if (sort === 'oldest') list.sort((a, b) => a.timestamp - b.timestamp);
    else if (sort === 'alpha') list.sort((a, b) => a.text.localeCompare(b.text, 'es'));
    else list.sort((a, b) => b.uses - a.uses || b.timestamp - a.timestamp);
    return list.sort((a, b) => Number(b.pinned) - Number(a.pinned));
  }, [memories, query, category, sort]);

  const pinnedCount = (memories ?? []).filter((m) => m.pinned).length;

  /* ── Actions ── */
  const add = async () => {
    const value = newText.trim();
    if (!value) return;
    setBusy('add');
    try {
      const fresh = await addMemory(value, newCategory);
      say(fresh ? t('Saved.') : t('Already had it.'));
      setNewText('');
      await load();
    } catch (err) {
      say((err as Error).message || t('Could not save.'));
    } finally {
      setBusy(null);
    }
  };

  const savePref = async (key: 'memory_enabled' | 'auto_memory', value: boolean) => {
    if (key === 'memory_enabled') setEnabled(value);
    else setAutoExtract(value);
    try {
      await setPref(key, value);
      say(key === 'memory_enabled' ? (value ? t('Memory on.') : t('Memory off.')) : value ? t('Automatic extraction on.') : t('Automatic extraction off.'));
    } catch {
      say(t('Could not save the preference.'));
      if (key === 'memory_enabled') setEnabled(!value);
      else setAutoExtract(!value);
    }
  };

  const tidy = async () => {
    setBusy('tidy');
    try {
      const r = await auditMemories();
      say(r.removed > 0 ? `Ordenada: ${r.before} → ${r.after} (${r.removed} fuera).` : t('It was already tidy.'));
      await load();
    } catch (err) {
      say((err as Error).message || t('Could not tidy the memory.'));
    } finally {
      setBusy(null);
    }
  };

  const openExtract = async () => {
    setExtractOpen(true);
    if (!sessions) {
      try {
        setSessions(await listSessions());
      } catch {
        setSessions([]);
      }
    }
  };

  const extract = async () => {
    if (!extractSession) return;
    setBusy('extract');
    try {
      const items = await extractFromSession(extractSession);
      setExtractOpen(false);
      setSuggestions({ title: t('Suggestions from the conversation'), items: items.map((text) => ({ text, category: 'fact' })) });
    } catch (err) {
      say((err as Error).message || t('Could not extract anything.'));
    } finally {
      setBusy(null);
    }
  };

  const importFile = async (file: File) => {
    setBusy('import');
    try {
      const r = await importFromFile(file);
      if (r.message && r.suggestions.length === 0) say(r.message);
      setSuggestions({ title: t('Suggestions from {name}', { name: file.name }), items: r.suggestions });
    } catch (err) {
      say((err as Error).message || t('Could not read the file.'));
    } finally {
      setBusy(null);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const saveSuggestions = async (chosen: ImportSuggestion[]) => {
    let saved = 0;
    for (const s of chosen) {
      try {
        if (await addMemory(s.text, s.category)) saved += 1;
      } catch {
        /* keep going */
      }
    }
    setSuggestions(null);
    say(`${saved} guardada${saved === 1 ? '' : 's'}.`);
    await load();
  };

  const bulkDelete = async () => {
    const ids = [...selected];
    setConfirmBulk(false);
    let n = 0;
    for (const id of ids) {
      try {
        await deleteMemory(id);
        n += 1;
      } catch {
        /* keep going */
      }
    }
    setSelected(new Set());
    setSelecting(false);
    say(`${n} borrada${n === 1 ? '' : 's'}.`);
    await load();
  };

  if (failed) {
    return (
      <EmptyState
        icon={Brain}
        title={t('Could not read the memory')}
        body={t('The memory endpoint is not responding. The previous interface does not depend on this screen.')}
        primaryAction={{
          label: t('Open the previous interface'),
          onClick: () => {
            window.location.href = '/memory?shell=legacy';
          },
        }}
      />
    );
  }

  return (
    <div className="fs-screen fs-memory" data-testid="memory">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Memory')}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {memories ? `${tn(memories.length, '{n} memory', '{n} memories')}${pinnedCount ? ` · ${tn(pinnedCount, '{n} pinned', '{n} pinned#')}` : ''}. ` : ''}
            {t('What the assistant knows about you and uses when it fits; pinned ones always go.')}
          </p>
        </div>
        <div className="fs-memory__switches">
          <label className="fs-switch">
            <input type="checkbox" checked={enabled === true} disabled={enabled === null} onChange={(e) => void savePref('memory_enabled', e.target.checked)} />
            <span>{t('Memory on')}</span>
          </label>
          <label className="fs-switch">
            <input type="checkbox" checked={autoExtract === true} disabled={autoExtract === null} onChange={(e) => void savePref('auto_memory', e.target.checked)} />
            <span>{t('Extract from conversations on its own')}</span>
          </label>
          {/* The previous Brain kept the skills as a tab here; they have their own screen now. */}
          <Link to="/skills" className="fs-memory__skills-link">
            <Zap size={13} aria-hidden="true" /> {t('Skills live on their own screen')}
          </Link>
        </div>
      </header>

      <form
        className="fs-memory__add"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <textarea
          className="fs-memory__add-input"
          rows={1}
          placeholder={t('Something you want it to remember… (Enter saves)')}
          value={newText}
          onChange={(e) => setNewText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void add();
            }
          }}
          data-testid="memory-new"
        />
        <select className="fs-field" value={newCategory} onChange={(e) => setNewCategory(e.target.value)} aria-label={t('Category')}>
          {MEMORY_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {t(CATEGORY_LABEL[c])}
            </option>
          ))}
        </select>
        <Button type="submit" variant="primary" size="sm" icon={Plus} label={t('Save')} disabled={!newText.trim()} loading={busy === 'add'} testId="memory-add" />
      </form>

      <div className="fs-memory__toolbar">
        <label className="fs-memory__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder={t('Search the memory…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Search')} />
        </label>
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label={t('Sort')}>
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {t(s.label)}
            </option>
          ))}
        </select>
        <span className="fs-memory__spacer" />
        <Button variant="ghost" size="sm" icon={Sparkles} label={t('Tidy with the model')} loading={busy === 'tidy'} onClick={() => void tidy()} />
        <Button variant="ghost" size="sm" icon={Wand2} label={t('From a conversation')} onClick={() => void openExtract()} />
        <Button variant="ghost" size="sm" icon={FileUp} label={t('From a file')} loading={busy === 'import'} onClick={() => fileRef.current?.click()} />
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.txt,.md,.markdown,.docx,.csv,.json,text/plain,application/pdf"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void importFile(f);
          }}
        />
        <Button variant="ghost" size="sm" icon={Download} label={t('Export')} disabled={!memories?.length} onClick={() => memories && download(exportMemories(memories), `memory-${new Date().toISOString().slice(0, 10)}.json`)} />
        <Button
          variant="ghost"
          size="sm"
          icon={selecting ? X : CheckSquare}
          label={selecting ? t('Leave selection') : t('Select several')}
          onClick={() => {
            setSelecting((v) => !v);
            setSelected(new Set());
          }}
        />
      </div>

      {categories.length > 1 && (
        <div className="fs-memory__cats" role="group" aria-label={t('Category')}>
          <button type="button" className="fs-chip" data-on={category === 'all' || undefined} onClick={() => setCategory('all')}>
            Todas · {memories?.length ?? 0}
          </button>
          {categories.map(([c, n]) => (
            <button key={c} type="button" className="fs-chip" data-on={category === c || undefined} onClick={() => setCategory(category === c ? 'all' : c)}>
              {CATEGORY_LABEL[c] ? t(CATEGORY_LABEL[c]) : c} · {n}
            </button>
          ))}
        </div>
      )}

      {selecting && (
        <div className="fs-memory__bulk" role="toolbar" aria-label={t('Selection')}>
          <span>
            {selected.size} seleccionada{selected.size === 1 ? '' : 's'}
          </span>
          <button type="button" className="fs-memory__link" onClick={() => setSelected(selected.size === visible.length ? new Set() : new Set(visible.map((m) => m.id)))}>
            {selected.size === visible.length ? 'ninguna' : 'todas'}
          </button>
          <span className="fs-memory__spacer" />
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} disabled={selected.size === 0} onClick={() => setConfirmBulk(true)} />
        </div>
      )}

      {!memories && <Skeleton label={t('Loading the memory')} count={5} height="56px" />}

      {memories && visible.length === 0 && (
        <EmptyState icon={Brain} title={memories.length ? t('Nothing matches') : t('The memory is empty')} body={memories.length ? t('Try another search or another category.') : t('Write above what you want it to remember, or pull it from a conversation or a file.')} />
      )}

      {visible.length > 0 && (
        <div className="fs-memory__list">
          {visible.map((m) => (
            <MemoryRow
              key={m.id}
              memory={m}
              selecting={selecting}
              selected={selected.has(m.id)}
              onToggle={() =>
                setSelected((cur) => {
                  const next = new Set(cur);
                  if (next.has(m.id)) next.delete(m.id);
                  else next.add(m.id);
                  return next;
                })
              }
              onPin={() =>
                void pinMemory(m.id, !m.pinned)
                  .then(() => setMemories((cur) => (cur ? cur.map((x) => (x.id === m.id ? { ...x, pinned: !m.pinned } : x)) : cur)))
                  .catch(() => say(t('Could not pin.')))
              }
              onDelete={() =>
                void deleteMemory(m.id)
                  .then(() => {
                    setMemories((cur) => (cur ? cur.filter((x) => x.id !== m.id) : cur));
                    say(t('Deleted.'));
                  })
                  .catch(() => say(t('Could not delete.')))
              }
              onSave={async (text, cat) => {
                try {
                  await updateMemory(m.id, text, cat);
                  setMemories((cur) => (cur ? cur.map((x) => (x.id === m.id ? { ...x, text, category: cat, timestamp: Math.floor(Date.now() / 1000) } : x)) : cur));
                } catch {
                  say(t('Could not save the change.'));
                  throw new Error('save');
                }
              }}
            />
          ))}
        </div>
      )}

      <LearnedRules say={say} />

      {suggestions && <SuggestionsDialog title={suggestions.title} items={suggestions.items} onClose={() => setSuggestions(null)} onSave={saveSuggestions} />}

      {extractOpen && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setExtractOpen(false);
          }}
          title={t('Pull memories from a conversation')}
          description={t('The model reads the conversation and proposes what would be worth saving.')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setExtractOpen(false)} />
              <Button variant="primary" size="sm" label={t('Extract')} disabled={!extractSession} loading={busy === 'extract'} onClick={() => void extract()} />
            </>
          }
        >
          {!sessions && <Skeleton label={t('Loading conversations')} count={3} height="32px" />}
          {sessions && (
            <select className="fs-field fs-memory__session-select" value={extractSession} onChange={(e) => setExtractSession(e.target.value)} aria-label={t('Conversation')} size={8}>
              {sessions.slice(0, 60).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} · {s.messageCount} mensajes
                </option>
              ))}
            </select>
          )}
        </Dialog>
      )}

      {confirmBulk && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setConfirmBulk(false);
          }}
          title={tn(selected.size, 'Delete {n} memory?', 'Delete {n} memories?')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirmBulk(false)} />
              <Button variant="danger-solid" size="sm" label={t('Delete')} onClick={() => void bulkDelete()} />
            </>
          }
        >
          <p className="fs-prose">{t('This cannot be undone.')}</p>
        </Dialog>
      )}

      {notice && (
        <Toast>
          <Check size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}
