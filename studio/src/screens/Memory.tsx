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
  { value: 'newest', label: 'Más recientes' },
  { value: 'oldest', label: 'Más antiguas' },
  { value: 'alpha', label: 'Alfabético' },
  { value: 'uses', label: 'Más usadas' },
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
              <select className="fs-field" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Categoría">
                {MEMORY_CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {CATEGORY_LABEL[c]}
                  </option>
                ))}
              </select>
              <span className="fs-mem__spacer" />
              <Button variant="ghost" size="sm" label="Cancelar" onClick={() => setEditing(false)} />
              <Button variant="primary" size="sm" label="Guardar" loading={saving} onClick={() => void commit()} />
            </div>
          </div>
        ) : (
          <button type="button" className="fs-mem__text" onClick={() => !selecting && setEditing(true)} title="Editar">
            {memory.text}
          </button>
        )}
        <div className="fs-mem__meta">
          <span className="fs-mem__cat" data-cat={memory.category}>
            {CATEGORY_LABEL[memory.category] ?? memory.category}
          </span>
          <span>{memory.source === 'user' ? 'tuya' : memory.source === 'auto' ? 'extraída' : memory.source}</span>
          {memory.timestamp > 0 && <span>{relativeTime(new Date(memory.timestamp * 1000).toISOString())}</span>}
          {memory.uses > 0 && <span>{memory.uses} uso{memory.uses === 1 ? '' : 's'}</span>}
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
          <IconButton icon={memory.pinned ? PinOff : Pin} label={memory.pinned ? 'Desfijar (deja de ir siempre en el contexto)' : 'Fijar (va siempre en el contexto)'} size="sm" onClick={onPin} />
          <IconButton icon={Trash2} label="Borrar" size="sm" onClick={onDelete} />
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
      description={items.length ? `${items.length} sugerencia${items.length === 1 ? '' : 's'}. Desmarca lo que no quieras guardar.` : 'No ha salido nada que valga la pena guardar.'}
      testId="memory-suggestions"
      footer={
        <>
          <Button variant="ghost" size="sm" label="Cerrar" onClick={onClose} />
          {items.length > 0 && (
            <Button
              variant="primary"
              size="sm"
              label={`Guardar ${chosen.size}`}
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
              {CATEGORY_LABEL[s.category] ?? s.category}
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
        <span className="fs-rule__score" title="Puntuación efectiva: confianza × frescura + útiles − 4 × dañinas">
          <span className="fs-rule__bar" aria-hidden="true">
            <span style={{ inlineSize: `${pct}%` }} />
          </span>
          {rule.effectiveScore.toFixed(2)}
        </span>
        {harm > 0 && <span className="fs-rule__harm">{harm} % dañina</span>}
        <span className="fs-rule__actions">
          <IconButton icon={ThumbsUp} label="Esta regla ayudó" size="sm" onClick={() => onFeedback('helpful')} />
          <IconButton icon={ThumbsDown} label="Esta regla hizo daño" size="sm" onClick={() => onFeedback('harmful')} />
          <IconButton icon={X} label="Borrar la regla" size="sm" onClick={onDelete} />
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
        setFailed((err as { status?: number })?.status === 403 ? 'Solo el administrador ve las reglas aprendidas.' : 'No he podido leer las reglas aprendidas.');
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
    const t = text.trim();
    if (!t) return;
    setBusy('add');
    try {
      const r = await addRule(t, level);
      setRules((cur) => [r, ...(cur ?? [])]);
      setText('');
      load();
    } catch (err) {
      say((err as Error).message || 'No he podido añadir la regla.');
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
            Reglas aprendidas <span className="fs-rules__count">{all.length}</span>
          </h2>
          <p className="fs-prose">
            Reglas puntuadas por resultado que el agente aprende y olvida solo: si una hace daño varias veces, se invierte en un antipatrón.
            {stats?.semanticLane ? ' Con carril semántico.' : ''}
          </p>
        </div>
        <div className="fs-rules__tools">
          <Button
            variant="secondary"
            size="sm"
            icon={Wand2}
            label="Ejecutar el curador"
            loading={busy === 'curate'}
            onClick={async () => {
              setBusy('curate');
              try {
                setReport(await curateRules());
                load();
              } catch {
                say('El curador ha fallado.');
              } finally {
                setBusy(null);
              }
            }}
          />
          <Button
            variant="ghost"
            size="sm"
            label="Ver el paquete"
            loading={busy === 'pack'}
            onClick={async () => {
              setBusy('pack');
              try {
                setPack(await previewPack());
              } catch {
                say('No he podido montar el paquete.');
              } finally {
                setBusy(null);
              }
            }}
          />
        </div>
      </header>

      {report && (
        <p className="fs-rules__report" role="status">
          Curador: <b>{report.deduped}</b> duplicadas · <b>{report.inverted}</b> invertidas · <b>{report.promoted}</b> promovidas · <b>{report.demoted}</b> degradadas · <b>{report.pruned}</b> podadas · <b>{report.totalActive}</b> activas
        </p>
      )}

      <div className="fs-rules__filters" role="group" aria-label="Filtrar reglas">
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
      {!rules && !failed && <Skeleton label="Cargando reglas" count={3} height="48px" />}
      {rules && !failed && sorted.length === 0 && <p className="fs-rules__empty">{all.length ? 'Nada con ese filtro.' : 'Todavía no hay reglas: añade una abajo o deja que el agente aprenda de los resultados.'}</p>}
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
                    say(kind === 'helpful' ? 'Marcada como útil.' : 'Marcada como dañina.');
                  })
                  .catch(() => say('No he podido guardar la valoración.'))
              }
              onDelete={() =>
                void deleteRule(r.id)
                  .then(() => setRules((cur) => (cur ? cur.filter((x) => x.id !== r.id) : cur)))
                  .catch(() => say('No he podido borrar la regla.'))
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
        <input type="text" className="fs-field" placeholder="Una regla nueva, escrita por ti (confianza máxima)…" value={text} onChange={(e) => setText(e.target.value)} />
        <select className="fs-field" value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Nivel">
          {RULE_LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
        <Button type="submit" variant="secondary" size="sm" icon={Plus} label="Añadir" disabled={!text.trim()} loading={busy === 'add'} />
      </form>

      {pack && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setPack(null);
          }}
          title="Lo que ve el modelo"
          description={`${pack.chars} de ${pack.budget} caracteres${pack.degraded ? ' · degradado' : ''}`}
          footer={<Button variant="ghost" size="sm" label="Cerrar" onClick={() => setPack(null)} />}
        >
          <pre className="fs-rules__pack">{pack.text || '(vacío)'}</pre>
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
    const t = newText.trim();
    if (!t) return;
    setBusy('add');
    try {
      const fresh = await addMemory(t, newCategory);
      say(fresh ? 'Guardada.' : 'Ya la tenía.');
      setNewText('');
      await load();
    } catch (err) {
      say((err as Error).message || 'No he podido guardar.');
    } finally {
      setBusy(null);
    }
  };

  const savePref = async (key: 'memory_enabled' | 'auto_memory', value: boolean) => {
    if (key === 'memory_enabled') setEnabled(value);
    else setAutoExtract(value);
    try {
      await setPref(key, value);
      say(key === 'memory_enabled' ? (value ? 'Memoria activada.' : 'Memoria desactivada.') : value ? 'Extracción automática activada.' : 'Extracción automática desactivada.');
    } catch {
      say('No he podido guardar la preferencia.');
      if (key === 'memory_enabled') setEnabled(!value);
      else setAutoExtract(!value);
    }
  };

  const tidy = async () => {
    setBusy('tidy');
    try {
      const r = await auditMemories();
      say(r.removed > 0 ? `Ordenada: ${r.before} → ${r.after} (${r.removed} fuera).` : 'Ya estaba limpia.');
      await load();
    } catch (err) {
      say((err as Error).message || 'No he podido ordenar la memoria.');
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
      setSuggestions({ title: 'Sugerencias de la conversación', items: items.map((text) => ({ text, category: 'fact' })) });
    } catch (err) {
      say((err as Error).message || 'No he podido extraer nada.');
    } finally {
      setBusy(null);
    }
  };

  const importFile = async (file: File) => {
    setBusy('import');
    try {
      const r = await importFromFile(file);
      if (r.message && r.suggestions.length === 0) say(r.message);
      setSuggestions({ title: `Sugerencias de ${file.name}`, items: r.suggestions });
    } catch (err) {
      say((err as Error).message || 'No he podido leer el fichero.');
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
        title="No he podido leer la memoria"
        body="El endpoint de memoria no responde. La interfaz anterior no depende de esta pantalla."
        primaryAction={{
          label: 'Abrir la interfaz anterior',
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
          <h1 className="fs-screen__title">Memoria</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {memories ? `${memories.length} recuerdo${memories.length === 1 ? '' : 's'}${pinnedCount ? ` · ${pinnedCount} fijado${pinnedCount === 1 ? '' : 's'}` : ''}. ` : ''}
            Lo que el asistente sabe de ti y usa cuando viene al caso; lo fijado va siempre.
          </p>
        </div>
        <div className="fs-memory__switches">
          <label className="fs-switch">
            <input type="checkbox" checked={enabled === true} disabled={enabled === null} onChange={(e) => void savePref('memory_enabled', e.target.checked)} />
            <span>Memoria activa</span>
          </label>
          <label className="fs-switch">
            <input type="checkbox" checked={autoExtract === true} disabled={autoExtract === null} onChange={(e) => void savePref('auto_memory', e.target.checked)} />
            <span>Extraer sola de las conversaciones</span>
          </label>
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
          placeholder="Algo que quieras que recuerde… (Intro guarda)"
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
        <select className="fs-field" value={newCategory} onChange={(e) => setNewCategory(e.target.value)} aria-label="Categoría">
          {MEMORY_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {CATEGORY_LABEL[c]}
            </option>
          ))}
        </select>
        <Button type="submit" variant="primary" size="sm" icon={Plus} label="Guardar" disabled={!newText.trim()} loading={busy === 'add'} testId="memory-add" />
      </form>

      <div className="fs-memory__toolbar">
        <label className="fs-memory__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder="Buscar en la memoria…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Buscar" />
        </label>
        <select className="fs-field" value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label="Ordenar">
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <span className="fs-memory__spacer" />
        <Button variant="ghost" size="sm" icon={Sparkles} label="Ordenar con el modelo" loading={busy === 'tidy'} onClick={() => void tidy()} />
        <Button variant="ghost" size="sm" icon={Wand2} label="De una conversación" onClick={() => void openExtract()} />
        <Button variant="ghost" size="sm" icon={FileUp} label="De un fichero" loading={busy === 'import'} onClick={() => fileRef.current?.click()} />
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
        <Button variant="ghost" size="sm" icon={Download} label="Exportar" disabled={!memories?.length} onClick={() => memories && download(exportMemories(memories), `memoria-${new Date().toISOString().slice(0, 10)}.json`)} />
        <Button
          variant="ghost"
          size="sm"
          icon={selecting ? X : CheckSquare}
          label={selecting ? 'Salir de la selección' : 'Seleccionar varias'}
          onClick={() => {
            setSelecting((v) => !v);
            setSelected(new Set());
          }}
        />
      </div>

      {categories.length > 1 && (
        <div className="fs-memory__cats" role="group" aria-label="Categoría">
          <button type="button" className="fs-chip" data-on={category === 'all' || undefined} onClick={() => setCategory('all')}>
            Todas · {memories?.length ?? 0}
          </button>
          {categories.map(([c, n]) => (
            <button key={c} type="button" className="fs-chip" data-on={category === c || undefined} onClick={() => setCategory(category === c ? 'all' : c)}>
              {CATEGORY_LABEL[c] ?? c} · {n}
            </button>
          ))}
        </div>
      )}

      {selecting && (
        <div className="fs-memory__bulk" role="toolbar" aria-label="Selección">
          <span>
            {selected.size} seleccionada{selected.size === 1 ? '' : 's'}
          </span>
          <button type="button" className="fs-memory__link" onClick={() => setSelected(selected.size === visible.length ? new Set() : new Set(visible.map((m) => m.id)))}>
            {selected.size === visible.length ? 'ninguna' : 'todas'}
          </button>
          <span className="fs-memory__spacer" />
          <Button variant="danger" size="sm" icon={Trash2} label="Borrar" disabled={selected.size === 0} onClick={() => setConfirmBulk(true)} />
        </div>
      )}

      {!memories && <Skeleton label="Cargando la memoria" count={5} height="56px" />}

      {memories && visible.length === 0 && (
        <EmptyState icon={Brain} title={memories.length ? 'Nada coincide' : 'La memoria está vacía'} body={memories.length ? 'Prueba otra búsqueda u otra categoría.' : 'Escribe arriba lo que quieras que recuerde, o sácalo de una conversación o de un fichero.'} />
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
                  .catch(() => say('No he podido fijar.'))
              }
              onDelete={() =>
                void deleteMemory(m.id)
                  .then(() => {
                    setMemories((cur) => (cur ? cur.filter((x) => x.id !== m.id) : cur));
                    say('Borrada.');
                  })
                  .catch(() => say('No he podido borrar.'))
              }
              onSave={async (text, cat) => {
                try {
                  await updateMemory(m.id, text, cat);
                  setMemories((cur) => (cur ? cur.map((x) => (x.id === m.id ? { ...x, text, category: cat, timestamp: Math.floor(Date.now() / 1000) } : x)) : cur));
                } catch {
                  say('No he podido guardar el cambio.');
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
          title="Sacar recuerdos de una conversación"
          description="El modelo lee la conversación y propone lo que valdría la pena guardar."
          footer={
            <>
              <Button variant="ghost" size="sm" label="Cancelar" onClick={() => setExtractOpen(false)} />
              <Button variant="primary" size="sm" label="Extraer" disabled={!extractSession} loading={busy === 'extract'} onClick={() => void extract()} />
            </>
          }
        >
          {!sessions && <Skeleton label="Cargando conversaciones" count={3} height="32px" />}
          {sessions && (
            <select className="fs-field fs-memory__session-select" value={extractSession} onChange={(e) => setExtractSession(e.target.value)} aria-label="Conversación" size={8}>
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
          title={`¿Borrar ${selected.size} recuerdo${selected.size === 1 ? '' : 's'}?`}
          footer={
            <>
              <Button variant="ghost" size="sm" label="Cancelar" onClick={() => setConfirmBulk(false)} />
              <Button variant="danger-solid" size="sm" label="Borrar" onClick={() => void bulkDelete()} />
            </>
          }
        >
          <p className="fs-prose">No se puede deshacer.</p>
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
