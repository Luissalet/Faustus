import {
  Archive,
  ArchiveRestore,
  Bell,
  Bot,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  GripVertical,
  LayoutGrid,
  List,
  ListChecks,
  MoreHorizontal,
  Pin,
  PinOff,
  Plus,
  StickyNote,
  Trash2,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, QuickMenu, Skeleton } from '../components';
import {
  advance,
  createNote,
  deleteNote,
  fireReminder,
  formatDue,
  hasTime,
  isChecklist,
  isOverdue,
  isToday,
  labelsOf,
  listNotes,
  NOTE_COLORS,
  parseDue,
  progress,
  reorderNotes,
  repeatLabel,
  REPEATS,
  toggleArchive,
  toggleItem,
  togglePin,
  toLocalInput,
  updateNote,
  type Note,
  type NoteDraft,
  type NoteItem,
  type NoteType,
} from '../adapters/notes';
import './projects.css';
import './notes.css';

/**
 * Notas.
 *
 * The previous interface's floating notes window as a screen: quick add,
 * cards (note / checklist / goal) with colour, labels, pin, due date with
 * repeat, archive with undo, manual order, "today", and "solve with the
 * agent", which hands the note to Studio. Reminders fire from here only
 * when the previous notes.js is not loaded underneath (it keeps its own
 * loop), so nothing rings twice.
 */

const VIEW_KEY = 'odysseus-notes-view';
const FIRED_KEY = 'faustus_studio_notes_fired';

type View = 'list' | 'grid';
type Filter = 'all' | 'today' | 'goals';

function readView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === 'grid' ? 'grid' : 'list';
  } catch {
    return 'list';
  }
}

function uid() {
  return Math.random().toString(36).slice(2, 10);
}

function linkify(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /https?:\/\/[^\s<>"')\]]+/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(
      <a key={k++} href={m[0]} target="_blank" rel="noopener noreferrer" className="fs-note__link" onClick={(e) => e.stopPropagation()}>
        {m[0]}
      </a>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function agentPrompt(note: Note): string {
  const parts: string[] = [];
  if (note.title.trim()) parts.push(note.title.trim());
  if (note.content.trim()) parts.push(note.content.trim());
  (note.items ?? []).filter((it) => !it.done && it.text.trim()).forEach((it) => parts.push(`- ${it.text.trim()}`));
  const body = parts.join('\n');
  return body ? `Ayúdame a terminar esto:\n\n${body}\n\nLa nota de origen es de solo lectura: no la edites ni la sustituyas.` : '';
}

function serialize(note: Note): string {
  const lines: string[] = [];
  if (note.title) lines.push(note.title);
  if (note.content) lines.push(note.content);
  (note.items ?? []).forEach((it) => lines.push(`${it.done ? '[x]' : '[ ]'} ${it.text}`));
  return lines.join('\n');
}

const TYPE_LABEL: Record<string, string> = { note: 'Nota', todo: 'Lista', checklist: 'Lista', goal: 'Objetivo', draw: 'Dibujo' };

/* ── Editor ─────────────────────────────────────────────────────────────── */

interface EditorState {
  title: string;
  content: string;
  items: NoteItem[];
  noteType: NoteType;
  color: string;
  label: string;
  dueDate: string;
  repeat: string;
}

function editorFrom(note: Note | null, type: NoteType = 'note', text = ''): EditorState {
  return {
    title: note?.title ?? '',
    content: note?.content ?? (type === 'note' ? text : ''),
    items: note?.items?.map((it) => ({ ...it, id: it.id ?? uid() })) ?? (type !== 'note' && text ? [{ id: uid(), text, done: false }] : [{ id: uid(), text: '', done: false }]),
    noteType: note?.noteType ?? type,
    color: note?.color ?? '',
    label: note?.label ?? '',
    dueDate: note?.dueDate ?? '',
    repeat: note?.repeat ?? 'none',
  };
}

function quickDate(kind: 'later' | 'tomorrow' | 'week'): string {
  const d = new Date();
  if (kind === 'later') {
    d.setHours(d.getHours() + 3, 0, 0, 0);
  } else if (kind === 'tomorrow') {
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
  } else {
    const until = (8 - d.getDay()) % 7 || 7;
    d.setDate(d.getDate() + until);
    d.setHours(9, 0, 0, 0);
  }
  return toLocalInput(d);
}

function NoteEditor({ note, initial, onClose, onSaved, onArchive, onDelete }: { note: Note | null; initial: EditorState; onClose: () => void; onSaved: (note: Note) => void; onArchive?: () => void; onDelete?: () => void }) {
  const [state, setState] = useState<EditorState>(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listMode = state.noteType !== 'note' && state.noteType !== 'draw';
  const set = (patch: Partial<EditorState>) => setState((s) => ({ ...s, ...patch }));

  const setItem = (i: number, patch: Partial<NoteItem>) => set({ items: state.items.map((it, j) => (j === i ? { ...it, ...patch } : it)) });
  const removeItem = (i: number) => set({ items: state.items.filter((_, j) => j !== i) });
  const addItem = (after?: number) => {
    const next = state.items.slice();
    next.splice(after === undefined ? next.length : after + 1, 0, { id: uid(), text: '', done: false });
    set({ items: next });
    requestAnimationFrame(() => {
      const inputs = document.querySelectorAll<HTMLInputElement>('.fs-note-editor__item input[type="text"]');
      inputs[after === undefined ? inputs.length - 1 : after + 1]?.focus();
    });
  };
  const moveItem = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= state.items.length) return;
    const next = state.items.slice();
    [next[i], next[j]] = [next[j], next[i]];
    set({ items: next });
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    const draft: NoteDraft = {
      title: state.title.trim(),
      content: listMode ? '' : state.content,
      items: listMode ? state.items.filter((it) => it.text.trim()).map((it) => ({ id: it.id, text: it.text.trim(), done: it.done })) : null,
      noteType: state.noteType,
      color: state.color,
      label: state.label.trim(),
      dueDate: state.dueDate || null,
      repeat: state.dueDate ? state.repeat : 'none',
    };
    try {
      const saved = note ? await updateNote(note.id, draft) : await createNote(draft);
      onSaved(saved);
    } catch {
      setError('No he podido guardar la nota.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={note ? 'Editar la nota' : 'Nueva nota'}
      testId="note-editor"
      footer={
        <div className="fs-note-editor__foot">
          {note && onArchive && <Button variant="ghost" size="sm" icon={Archive} label={note.archived ? 'Recuperar' : 'Archivar'} onClick={onArchive} />}
          {note && onDelete && <Button variant="danger" size="sm" icon={Trash2} label="Borrar" onClick={onDelete} />}
          <span className="fs-note-editor__spacer" />
          <Button variant="ghost" size="sm" label="Cancelar" onClick={onClose} />
          <Button variant="primary" size="sm" label={note ? 'Guardar' : 'Crear'} loading={saving} onClick={() => void save()} testId="note-save" />
        </div>
      }
    >
      <div className={`fs-note-editor${state.color && !state.color.startsWith('bg:') ? ` fs-note--${state.color}` : ''}`}>
        <div className="fs-note-editor__type" role="group" aria-label="Tipo de nota">
          {(['note', 'todo', 'goal'] as NoteType[]).map((t) => (
            <button key={t} type="button" className="fs-chip" data-on={state.noteType === t || undefined} onClick={() => set({ noteType: t })}>
              {t === 'note' ? <StickyNote size={13} aria-hidden="true" /> : <ListChecks size={13} aria-hidden="true" />}
              {TYPE_LABEL[t]}
            </button>
          ))}
        </div>
        <input
          type="text"
          className="fs-note-editor__title"
          placeholder="Título"
          value={state.title}
          onChange={(e) => set({ title: e.target.value })}
          autoFocus={!note}
        />
        {!listMode && (
          <textarea className="fs-note-editor__content" placeholder="Escribe la nota…" rows={5} value={state.content} onChange={(e) => set({ content: e.target.value })} />
        )}
        {listMode && (
          <div className="fs-note-editor__items">
            {state.noteType === 'goal' && (
              <textarea className="fs-note-editor__content" placeholder="Qué quieres conseguir (los pasos van abajo)" rows={2} value={state.content} onChange={(e) => set({ content: e.target.value })} />
            )}
            {state.items.map((it, i) => (
              <div key={it.id ?? i} className="fs-note-editor__item" data-done={it.done || undefined}>
                <input type="checkbox" checked={it.done} onChange={(e) => setItem(i, { done: e.target.checked })} aria-label="Hecho" />
                <input
                  type="text"
                  value={it.text}
                  placeholder={state.noteType === 'goal' ? 'Paso' : 'Elemento'}
                  onChange={(e) => setItem(i, { text: e.target.value })}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      addItem(i);
                    } else if (e.key === 'Backspace' && !it.text && state.items.length > 1) {
                      e.preventDefault();
                      removeItem(i);
                    }
                  }}
                />
                <IconButton icon={ChevronUp} label="Subir" size="sm" disabled={i === 0} onClick={() => moveItem(i, -1)} />
                <IconButton icon={ChevronDown} label="Bajar" size="sm" disabled={i === state.items.length - 1} onClick={() => moveItem(i, 1)} />
                <IconButton icon={X} label="Quitar" size="sm" onClick={() => removeItem(i)} />
              </div>
            ))}
            <button type="button" className="fs-note-editor__add" onClick={() => addItem()}>
              <Plus size={13} aria-hidden="true" /> {state.noteType === 'goal' ? 'Añadir paso' : 'Añadir elemento'}
            </button>
          </div>
        )}

        <div className="fs-note-editor__row">
          <span className="fs-note-editor__label">Color</span>
          <div className="fs-note-editor__colors" role="radiogroup" aria-label="Color">
            {NOTE_COLORS.map((c) => (
              <button
                key={c || 'none'}
                type="button"
                role="radio"
                aria-checked={state.color === c}
                aria-label={c || 'sin color'}
                className={`fs-note-editor__dot${c ? ` fs-note--${c}` : ''}`}
                data-on={state.color === c || undefined}
                onClick={() => set({ color: c })}
              />
            ))}
            {state.color.startsWith('bg:') && <span className="fs-note-editor__hint">Fondo propio (elegido en la interfaz anterior)</span>}
          </div>
        </div>

        <div className="fs-note-editor__row">
          <span className="fs-note-editor__label">Etiquetas</span>
          <input type="text" className="fs-field" placeholder="#casa #trabajo" value={state.label} onChange={(e) => set({ label: e.target.value })} />
        </div>

        <div className="fs-note-editor__row">
          <span className="fs-note-editor__label">
            <Bell size={12} aria-hidden="true" /> Recordar
          </span>
          <div className="fs-note-editor__when">
            <input type="datetime-local" className="fs-field" value={state.dueDate} onChange={(e) => set({ dueDate: e.target.value })} aria-label="Fecha y hora" />
            <div className="fs-note-editor__quick">
              <button type="button" className="fs-chip" onClick={() => set({ dueDate: quickDate('later') })}>Más tarde</button>
              <button type="button" className="fs-chip" onClick={() => set({ dueDate: quickDate('tomorrow') })}>Mañana</button>
              <button type="button" className="fs-chip" onClick={() => set({ dueDate: quickDate('week') })}>Semana que viene</button>
              {state.dueDate && (
                <button type="button" className="fs-chip" onClick={() => set({ dueDate: '', repeat: 'none' })}>
                  <X size={12} aria-hidden="true" /> Quitar
                </button>
              )}
            </div>
            {state.dueDate && (
              <select className="fs-field" value={state.repeat.split(':')[0]} onChange={(e) => set({ repeat: e.target.value })} aria-label="Repetir">
                {REPEATS.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>
        {error && <p className="fs-note-editor__error" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}

/* ── Card ───────────────────────────────────────────────────────────────── */

interface CardProps {
  note: Note;
  index: number;
  count: number;
  archivedView: boolean;
  onEdit: () => void;
  onToggleItem: (index: number) => void;
  onPin: () => void;
  onArchive: () => void;
  onDelete: () => void;
  onAgent: () => void;
  onCopy: () => void;
  onMove: (dir: -1 | 1) => void;
  onOpenAgentChat: () => void;
  dragProps: {
    draggable: boolean;
    onDragStart: () => void;
    onDragOver: (e: React.DragEvent) => void;
    onDrop: () => void;
    onDragEnd: () => void;
  };
}

function NoteCard({ note, index, count, archivedView, onEdit, onToggleItem, onPin, onArchive, onDelete, onAgent, onCopy, onMove, onOpenAgentChat, dragProps }: CardProps) {
  const list = isChecklist(note);
  const { done, total } = progress(note);
  const overdue = isOverdue(note.dueDate) && !archivedView;
  const bg = note.color.startsWith('bg:') ? note.color.slice(3) : null;
  const colorClass = note.color && !bg ? ` fs-note--${note.color}` : '';
  const labels = labelsOf(note);
  const nextStep = note.noteType === 'goal' ? (note.items ?? []).find((it) => !it.done) : null;

  return (
    <article
      className={`fs-note fs-enter${colorClass}`}
      style={{ ['--i' as string]: Math.min(index, 8), ...(bg ? { backgroundImage: `url("${bg}")` } : {}) }}
      data-type={note.noteType}
      data-pinned={note.pinned || undefined}
      data-overdue={overdue || undefined}
      data-done={list && total > 0 && done === total ? true : undefined}
      data-testid="note-card"
      {...dragProps}
    >
      <header className="fs-note__head">
        <span className="fs-note__grip" aria-hidden="true">
          <GripVertical size={13} />
        </span>
        <button type="button" className="fs-note__title" onClick={onEdit} title="Editar">
          {note.pinned && <Pin size={11} aria-label="Fijada" className="fs-note__pin" />}
          {note.title || <span className="fs-note__untitled">{list ? 'Lista' : 'Nota'}</span>}
        </button>
        <QuickMenu
          label="Acciones de la nota"
          icon={MoreHorizontal}
          items={[
            { label: 'Editar', onSelect: onEdit },
            { label: note.pinned ? 'Desfijar' : 'Fijar', icon: note.pinned ? PinOff : Pin, onSelect: onPin },
            { label: 'Copiar el texto', icon: Copy, onSelect: onCopy },
            { label: 'Resolver con el agente', icon: Bot, onSelect: onAgent },
            ...(note.agentSessionId ? [{ label: 'Ver el chat del agente', icon: Bot, onSelect: onOpenAgentChat }] : []),
            { label: 'Subir', icon: ChevronUp, onSelect: () => onMove(-1), disabled: index === 0 },
            { label: 'Bajar', icon: ChevronDown, onSelect: () => onMove(1), disabled: index >= count - 1 },
            { label: note.archived ? 'Recuperar' : 'Archivar', icon: note.archived ? ArchiveRestore : Archive, onSelect: onArchive },
            { label: 'Borrar', icon: Trash2, onSelect: onDelete, variant: 'danger' },
          ]}
        />
      </header>

      {note.imageUrl && <img className="fs-note__image" src={note.imageUrl} alt="" loading="lazy" />}

      {!list && note.content && (
        <button type="button" className="fs-note__body" onClick={onEdit}>
          {linkify(note.content)}
        </button>
      )}

      {list && (
        <div className="fs-note__items">
          {note.noteType === 'goal' && note.content && <p className="fs-note__goal">{note.content}</p>}
          {(note.items ?? []).map((it, i) => (
            <label key={it.id ?? i} className="fs-note__item" data-done={it.done || undefined} data-next={nextStep === it || undefined}>
              <input type="checkbox" checked={it.done} onChange={() => onToggleItem(i)} />
              <span>{linkify(it.text)}</span>
              {it.agent_status && <span className="fs-note__agent-status">{it.agent_status}</span>}
            </label>
          ))}
          {total > 0 && (
            <div className="fs-note__progress" aria-label={`${done} de ${total}`}>
              <span style={{ inlineSize: `${Math.round((done / total) * 100)}%` }} />
            </div>
          )}
        </div>
      )}

      {(note.dueDate || labels.length > 0 || note.agentSessionId) && (
        <footer className="fs-note__foot">
          {note.dueDate && (
            <span className="fs-note__tag fs-note__tag--due" data-overdue={overdue || undefined} data-today={isToday(note.dueDate) || undefined}>
              <Bell size={11} aria-hidden="true" />
              {formatDue(note.dueDate)}
              {note.repeat !== 'none' && ` · ${repeatLabel(note.repeat)}`}
            </span>
          )}
          {labels.map((l) => (
            <span key={l} className="fs-note__tag">
              #{l}
            </span>
          ))}
          {note.agentSessionId && (
            <button type="button" className="fs-note__tag fs-note__tag--agent" onClick={onOpenAgentChat}>
              <Bot size={11} aria-hidden="true" /> agente
            </button>
          )}
        </footer>
      )}
    </article>
  );
}

/* ── Screen ─────────────────────────────────────────────────────────────── */

interface Undo {
  note: Note;
  index: number;
  label: string;
}

export function NotesScreen() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [archivedView, setArchivedView] = useState(false);
  const [view, setView] = useState<View>(readView);
  const [filter, setFilter] = useState<Filter>('all');
  const [label, setLabel] = useState<string | null>(null);
  const [quick, setQuick] = useState('');
  const [quickType, setQuickType] = useState<NoteType>('note');
  const [editing, setEditing] = useState<{ note: Note | null; initial: EditorState } | null>(null);
  const [undo, setUndo] = useState<Undo | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Note | null>(null);
  const dragFrom = useRef<number | null>(null);
  const undoTimer = useRef(0);

  const load = useCallback(
    (archived: boolean, signal?: AbortSignal) =>
      listNotes(archived, signal)
        .then((list) => {
          setNotes(list);
          setFailed(false);
        })
        .catch((err: unknown) => {
          if ((err as { name?: string })?.name !== 'AbortError') setFailed(true);
        }),
    [],
  );

  useEffect(() => {
    const controller = new AbortController();
    setNotes(null);
    void load(archivedView, controller.signal);
    return () => controller.abort();
  }, [archivedView, load]);

  /* Deep link: /notes?n=<id> opens that note (the previous interface's openNote). */
  useEffect(() => {
    const id = params.get('n');
    if (!id || !notes) return;
    const target = notes.find((n) => n.id === id);
    const next = new URLSearchParams(params);
    next.delete('n');
    setParams(next, { replace: true });
    if (target) setEditing({ note: target, initial: editorFrom(target) });
  }, [params, notes, setParams]);

  useEffect(() => {
    try {
      localStorage.setItem(VIEW_KEY, view);
    } catch {
      /* ignore */
    }
  }, [view]);

  const say = (text: string) => {
    setNotice(text);
    window.setTimeout(() => setNotice((cur) => (cur === text ? null : cur)), 4000);
  };

  const replace = (saved: Note) => setNotes((cur) => (cur ? cur.map((n) => (n.id === saved.id ? saved : n)) : cur));

  const allLabels = useMemo(() => {
    const set = new Set<string>();
    (notes ?? []).forEach((n) => labelsOf(n).forEach((l) => set.add(l)));
    return [...set].sort((a, b) => a.localeCompare(b, 'es'));
  }, [notes]);

  const visible = useMemo(() => {
    let list = notes ?? [];
    if (filter === 'today') list = list.filter((n) => n.dueDate && (isToday(n.dueDate) || isOverdue(n.dueDate)));
    if (filter === 'goals') list = list.filter((n) => n.noteType === 'goal');
    if (label) list = list.filter((n) => labelsOf(n).includes(label));
    return list;
  }, [notes, filter, label]);

  const dueCount = useMemo(() => (notes ?? []).filter((n) => !n.archived && n.dueDate && (isToday(n.dueDate) || isOverdue(n.dueDate))).length, [notes]);

  /* ── Actions ── */
  const quickAdd = async () => {
    const text = quick.trim();
    if (!text) return;
    setQuick('');
    try {
      const draft: NoteDraft =
        quickType === 'note'
          ? { noteType: 'note', title: '', content: text }
          : { noteType: quickType, title: '', items: text.split('\n').map((t) => ({ id: uid(), text: t.trim(), done: false })).filter((it) => it.text) };
      const created = await createNote(draft);
      setNotes((cur) => (cur ? [created, ...cur] : [created]));
    } catch {
      say('No he podido crear la nota.');
      setQuick(text);
    }
  };

  const onToggleItem = async (note: Note, index: number) => {
    // Optimistic: the checkbox answers at once, the server confirms.
    replace({ ...note, items: (note.items ?? []).map((it, i) => (i === index ? { ...it, done: !it.done } : it)) });
    try {
      const items = await toggleItem(note.id, index);
      replace({ ...note, items });
    } catch {
      replace(note);
      say('No he podido marcar el elemento.');
    }
  };

  const onPin = async (note: Note) => {
    try {
      const pinned = await togglePin(note.id);
      setNotes((cur) => {
        if (!cur) return cur;
        const next = cur.map((n) => (n.id === note.id ? { ...n, pinned } : n));
        return next.sort((a, b) => Number(b.pinned) - Number(a.pinned));
      });
    } catch {
      say('No he podido fijar la nota.');
    }
  };

  const onArchive = async (note: Note) => {
    const index = (notes ?? []).findIndex((n) => n.id === note.id);
    try {
      const archived = await toggleArchive(note.id);
      setNotes((cur) => (cur ? cur.filter((n) => n.id !== note.id) : cur));
      window.clearTimeout(undoTimer.current);
      setUndo({ note: { ...note, archived }, index, label: archived ? 'Nota archivada.' : 'Nota recuperada.' });
      undoTimer.current = window.setTimeout(() => setUndo(null), 7000);
      setEditing(null);
    } catch {
      say('No he podido archivar la nota.');
    }
  };

  const runUndo = async () => {
    if (!undo) return;
    const { note, index } = undo;
    setUndo(null);
    try {
      await toggleArchive(note.id);
      setNotes((cur) => {
        if (!cur) return cur;
        const next = cur.slice();
        next.splice(Math.min(index, next.length), 0, { ...note, archived: !note.archived });
        return next;
      });
    } catch {
      say('No he podido deshacer.');
    }
  };

  const onDelete = async (note: Note) => {
    try {
      await deleteNote(note.id);
      setNotes((cur) => (cur ? cur.filter((n) => n.id !== note.id) : cur));
      setEditing(null);
      setConfirmDelete(null);
      say('Nota borrada.');
    } catch {
      say('No he podido borrar la nota.');
    }
  };

  const onCopy = async (note: Note) => {
    try {
      await navigator.clipboard.writeText(serialize(note));
      say('Copiada.');
    } catch {
      say('El navegador no deja copiar.');
    }
  };

  const onAgent = (note: Note) => {
    const prompt = agentPrompt(note);
    if (!prompt) {
      say('La nota está vacía: no hay nada que resolver.');
      return;
    }
    const q = new URLSearchParams({ draft: prompt, mode: 'agent', send: '1', note: note.id });
    navigate(`/studio?${q.toString()}`);
  };

  const commitOrder = (list: Note[]) => {
    setNotes(list);
    void reorderNotes(list.map((n) => n.id)).catch(() => say('No he podido guardar el orden.'));
  };

  const onMove = (note: Note, dir: -1 | 1) => {
    const list = (notes ?? []).slice();
    const i = list.findIndex((n) => n.id === note.id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    commitOrder(list);
  };

  const dropOn = (targetIndex: number) => {
    const from = dragFrom.current;
    dragFrom.current = null;
    if (from === null || from === targetIndex || !notes) return;
    const list = notes.slice();
    const [moved] = list.splice(from, 1);
    list.splice(targetIndex, 0, moved);
    commitOrder(list);
  };

  /* ── Reminders: only when the previous notes.js is not running underneath. ── */
  useEffect(() => {
    if (archivedView || !notes) return;
    if (document.getElementById('notes-pane') || document.getElementById('notes-rail-btn')) return;
    const fired = (): Set<string> => {
      try {
        return new Set(JSON.parse(localStorage.getItem(FIRED_KEY) || '[]') as string[]);
      } catch {
        return new Set();
      }
    };
    const check = () => {
      const seen = fired();
      const now = Date.now();
      for (const note of notes) {
        if (!note.dueDate || !hasTime(note.dueDate)) continue;
        const at = parseDue(note.dueDate);
        if (!at || at.getTime() > now || now - at.getTime() > 12 * 3600 * 1000) continue;
        const key = `${note.id}@${note.dueDate}`;
        if (seen.has(key)) continue;
        seen.add(key);
        try {
          localStorage.setItem(FIRED_KEY, JSON.stringify([...seen].slice(-200)));
        } catch {
          /* ignore */
        }
        const body = note.content || (note.items ?? []).filter((it) => !it.done).map((it) => it.text).join(', ');
        void fireReminder(note.id)
          .then((r) => {
            const text = r.synthesis || body;
            if ('Notification' in window && Notification.permission === 'granted') {
              new Notification(note.title || 'Recordatorio', { body: text, tag: note.id });
            } else {
              say(`Recordatorio: ${note.title || text}`);
            }
          })
          .catch(() => say(`Recordatorio: ${note.title || body}`));
        if (note.repeat !== 'none') {
          const next = advance(note.dueDate, note.repeat);
          if (next) void updateNote(note.id, { dueDate: next }).then(replace).catch(() => undefined);
        }
      }
    };
    check();
    const timer = window.setInterval(check, 30000);
    return () => window.clearInterval(timer);
  }, [notes, archivedView]);

  const askNotifications = () => {
    if (!('Notification' in window)) return;
    void Notification.requestPermission();
  };

  if (failed) {
    return (
      <EmptyState
        icon={StickyNote}
        title="No he podido leer tus notas"
        body="El endpoint de notas no responde. La interfaz anterior no depende de esta pantalla."
        primaryAction={{
          label: 'Abrir la interfaz anterior',
          onClick: () => {
            window.location.href = '/notes?shell=legacy';
          },
        }}
      />
    );
  }

  return (
    <div className="fs-screen fs-notes" data-testid="notes" data-view={view}>
      <header className="fs-screen__head fs-notes__head">
        <div>
          <h1 className="fs-screen__title">{archivedView ? 'Notas archivadas' : 'Notas'}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {notes
              ? archivedView
                ? `${notes.length} archivada${notes.length === 1 ? '' : 's'}.`
                : `${notes.length} nota${notes.length === 1 ? '' : 's'}${dueCount ? ` · ${dueCount} para hoy` : ''}.`
              : 'Apuntes, listas y recordatorios.'}
          </p>
        </div>
        <div className="fs-notes__tools">
          <IconButton icon={view === 'list' ? LayoutGrid : List} label={view === 'list' ? 'Ver en cuadrícula' : 'Ver en lista'} size="sm" onClick={() => setView(view === 'list' ? 'grid' : 'list')} />
          <IconButton icon={archivedView ? StickyNote : Archive} label={archivedView ? 'Volver a las notas' : 'Ver las archivadas'} size="sm" onClick={() => setArchivedView((v) => !v)} />
          {'Notification' in window && Notification.permission === 'default' && (
            <IconButton icon={Bell} label="Permitir avisos del navegador" size="sm" onClick={askNotifications} />
          )}
          {!archivedView && <Button variant="primary" size="sm" icon={Plus} label="Nueva" onClick={() => setEditing({ note: null, initial: editorFrom(null) })} testId="note-new" />}
        </div>
      </header>

      {!archivedView && (
        <form
          className="fs-notes__quick"
          onSubmit={(e) => {
            e.preventDefault();
            void quickAdd();
          }}
        >
          <div className="fs-notes__quick-type" role="group" aria-label="Tipo">
            <button type="button" className="fs-chip" data-on={quickType === 'note' || undefined} onClick={() => setQuickType('note')}>
              <StickyNote size={13} aria-hidden="true" /> Nota
            </button>
            <button type="button" className="fs-chip" data-on={quickType === 'todo' || undefined} onClick={() => setQuickType('todo')}>
              <ListChecks size={13} aria-hidden="true" /> Lista
            </button>
          </div>
          <textarea
            className="fs-notes__quick-input"
            rows={1}
            placeholder={quickType === 'note' ? 'Apunta algo y pulsa Intro…' : 'Un elemento por línea; Intro para crear la lista…'}
            value={quick}
            onChange={(e) => setQuick(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void quickAdd();
              }
            }}
            data-testid="note-quick"
          />
          <button type="submit" className="fs-btn" data-size="sm" data-variant="secondary" disabled={!quick.trim()}>
            <Plus size={13} aria-hidden="true" /> Añadir
          </button>
        </form>
      )}

      {notes && !archivedView && (allLabels.length > 0 || notes.some((n) => n.noteType === 'goal') || dueCount > 0) && (
        <div className="fs-notes__filters" role="group" aria-label="Filtrar">
          <button type="button" className="fs-chip" data-on={filter === 'all' && !label ? true : undefined} onClick={() => { setFilter('all'); setLabel(null); }}>
            Todas
          </button>
          {dueCount > 0 && (
            <button type="button" className="fs-chip" data-on={filter === 'today' || undefined} onClick={() => setFilter(filter === 'today' ? 'all' : 'today')}>
              <Bell size={12} aria-hidden="true" /> Hoy · {dueCount}
            </button>
          )}
          {notes.some((n) => n.noteType === 'goal') && (
            <button type="button" className="fs-chip" data-on={filter === 'goals' || undefined} onClick={() => setFilter(filter === 'goals' ? 'all' : 'goals')}>
              Objetivos
            </button>
          )}
          {allLabels.map((l) => (
            <button key={l} type="button" className="fs-chip" data-on={label === l || undefined} onClick={() => setLabel(label === l ? null : l)}>
              #{l}
            </button>
          ))}
        </div>
      )}

      {!notes && <Skeleton label="Cargando notas" count={4} height="88px" />}

      {notes && visible.length === 0 && (
        <EmptyState
          icon={StickyNote}
          title={archivedView ? 'Nada archivado' : notes.length ? 'Nada con ese filtro' : 'Todavía no hay notas'}
          body={archivedView ? 'Lo que archives desde una nota aparece aquí y se puede recuperar.' : 'Escribe arriba y pulsa Intro. Una lista se crea con un elemento por línea.'}
        />
      )}

      {notes && visible.length > 0 && (
        <div className="fs-notes__grid" data-view={view}>
          {visible.map((note, i) => (
            <NoteCard
              key={note.id}
              note={note}
              index={i}
              count={visible.length}
              archivedView={archivedView}
              onEdit={() => setEditing({ note, initial: editorFrom(note) })}
              onToggleItem={(idx) => void onToggleItem(note, idx)}
              onPin={() => void onPin(note)}
              onArchive={() => void onArchive(note)}
              onDelete={() => setConfirmDelete(note)}
              onAgent={() => onAgent(note)}
              onCopy={() => void onCopy(note)}
              onMove={(dir) => onMove(note, dir)}
              onOpenAgentChat={() => note.agentSessionId && navigate(`/studio?s=${encodeURIComponent(note.agentSessionId)}`)}
              dragProps={{
                draggable: filter === 'all' && !label && !archivedView,
                onDragStart: () => {
                  dragFrom.current = i;
                },
                onDragOver: (e) => {
                  if (dragFrom.current !== null) e.preventDefault();
                },
                onDrop: () => dropOn(i),
                onDragEnd: () => {
                  dragFrom.current = null;
                },
              }}
            />
          ))}
        </div>
      )}

      {editing && (
        <NoteEditor
          note={editing.note}
          initial={editing.initial}
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            if (editing.note) replace(saved);
            else setNotes((cur) => (cur ? [saved, ...cur] : [saved]));
            setEditing(null);
          }}
          onArchive={editing.note ? () => void onArchive(editing.note as Note) : undefined}
          onDelete={editing.note ? () => setConfirmDelete(editing.note) : undefined}
        />
      )}

      {confirmDelete && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setConfirmDelete(null);
          }}
          title="¿Borrar la nota?"
          description={confirmDelete.title || 'No tiene título.'}
          footer={
            <>
              <Button variant="ghost" size="sm" label="Cancelar" onClick={() => setConfirmDelete(null)} />
              <Button variant="danger-solid" size="sm" label="Borrar" onClick={() => void onDelete(confirmDelete)} testId="note-delete-confirm" />
            </>
          }
        >
          <p className="fs-prose">Se borra del todo. Si solo quieres quitarla de en medio, archívala.</p>
        </Dialog>
      )}

      {(undo || notice) && (
        <div className="fs-notes__toast" role="status">
          <span>{undo ? undo.label : notice}</span>
          {undo && (
            <button type="button" className="fs-btn" data-size="sm" data-variant="secondary" onClick={() => void runUndo()}>
              Deshacer
            </button>
          )}
          <IconButton icon={X} label="Cerrar" size="sm" onClick={() => { setUndo(null); setNotice(null); }} />
        </div>
      )}
      {!archivedView && (
        <span className="fs-notes__hint">
          <Check size={11} aria-hidden="true" /> Arrastra una tarjeta para cambiar el orden.
        </span>
      )}
    </div>
  );
}
