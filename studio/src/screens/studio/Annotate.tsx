import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Crosshair, FileText, GitCompare, Globe, Maximize2, Minimize2, RotateCw, Send, Sparkles, Trash2, X, ListTree, MessageSquareQuote } from 'lucide-react';
import { Button, IconButton } from '../../components';
import {
  addAnnotation, composeAnnotations, deleteAnnotation, framePage, getAnnotations, getReview, guideReview, openReview, openTarget,
  planTitle, targetKey, TYPE_LABEL,
  type AnnotateTarget, type Annotation, type AnnotationAnchor, type AnnotationType, type DiffFile, type DiffHunk, type DiffLine,
  type Framable, type Review,
} from '../../adapters/annotations';
import { readWorkspaceFile } from '../../adapters/workspace';
import { onSurfaceEvent } from '../../adapters/surfaces';
import { Rich } from '../rich';
import { t } from '../../i18n';
import type { Turn } from './model';
import type { DocState } from './panel';
import './annotate.css';

/**
 * The Annotate tab: the agent's plan, a Markdown/HTML file of the workspace, a Studio document, a local web
 * page (live through the server's proxy, hot reload included) or a review diff. The person selects text, picks
 * an element or a diff line, leaves a comment, a suggested change, a deletion or a question, and sends every
 * pending note to the agent as the next message. Notes are kept per chat on the server (src/annotations.py).
 */

interface Pending { quote: string; anchor: AnnotationAnchor }
const TYPES: AnnotationType[] = ['comment', 'suggestion', 'delete', 'question'];
const KIND_ICON = { plan: MessageSquareQuote, file: FileText, doc: FileText, web: Globe, diff: GitCompare, text: FileText };
const CHAPTER_LABEL: Record<string, string> = { core: 'Core', effects: 'Effects', glue: 'Glue' };

function lastAssistant(turns: Turn[]): Turn | null {
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i].role === 'assistant' && turns[i].text.trim()) return turns[i];
  return null;
}

/** Paint every quote of `notes` found inside `root` with the CSS Custom Highlight API (no DOM changes). */
function paintQuotes(root: HTMLElement | null, notes: Annotation[]): void {
  const reg = (globalThis as { CSS?: { highlights?: Map<string, unknown> } }).CSS?.highlights;
  const HighlightCtor = (globalThis as { Highlight?: new (...r: Range[]) => unknown }).Highlight;
  if (!reg || !HighlightCtor) return;
  if (!root) { reg.delete('faustus-annotation'); return; }
  const nodes: Text[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n as Text);
  const starts: number[] = [];
  let all = '';
  for (const n of nodes) { starts.push(all.length); all += n.data; }
  const at = (offset: number): [Text, number] | null => {
    for (let i = nodes.length - 1; i >= 0; i--) if (starts[i] <= offset) return [nodes[i], offset - starts[i]];
    return null;
  };
  const ranges: Range[] = [];
  for (const note of notes) {
    const quote = note.quote.trim();
    if (!quote) continue;
    const i = all.indexOf(quote);
    if (i < 0) continue;
    const a = at(i), b = at(i + quote.length);
    if (!a || !b) continue;
    const r = document.createRange();
    try { r.setStart(a[0], a[1]); r.setEnd(b[0], Math.min(b[1], b[0].length)); ranges.push(r); } catch { /* moved on */ }
  }
  reg.set('faustus-annotation', new HighlightCtor(...ranges));
}

function NoteForm({ pending, onSave, onCancel }: { pending: Pending; onSave: (type: AnnotationType, text: string) => Promise<void>; onCancel: () => void }) {
  const [type, setType] = useState<AnnotationType>('comment');
  const [text, setText] = useState('');
  const [saving, setSaving] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { area.current?.focus(); }, [type]);
  const needsText = type !== 'delete';
  const save = async () => {
    if (needsText && !text.trim()) return;
    setSaving(true);
    try { await onSave(type, text); } finally { setSaving(false); }
  };
  return (
    <div className="fs-annot__form" role="group" aria-label={t('New annotation')} data-testid="annotate-form">
      <blockquote className="fs-annot__quote">{pending.quote || t('(no text selected)')}</blockquote>
      {pending.anchor.file && <small className="fs-annot__where">{pending.anchor.file}:{pending.anchor.line}</small>}
      {pending.anchor.selector && <small className="fs-annot__where">{pending.anchor.selector}</small>}
      <div className="fs-annot__types" role="radiogroup" aria-label={t('Kind of annotation')}>
        {TYPES.map(ty => (
          <button key={ty} type="button" role="radio" aria-checked={type === ty} data-type={ty} onClick={() => setType(ty)}>{t(TYPE_LABEL[ty])}</button>
        ))}
      </div>
      {type !== 'delete' && (
        <textarea ref={area} rows={3} value={text} onChange={e => setText(e.target.value)} data-testid="annotate-text"
          placeholder={type === 'suggestion' ? t('Write it as it should read') : type === 'question' ? t('What do you want to ask?') : t('Your comment')}
          onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void save(); } if (e.key === 'Escape') onCancel(); }} />
      )}
      {type === 'delete' && (
        <textarea ref={area} rows={2} value={text} onChange={e => setText(e.target.value)} placeholder={t('Why (optional)')}
          onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void save(); } if (e.key === 'Escape') onCancel(); }} />
      )}
      <div className="fs-annot__formbar">
        <Button size="sm" variant="ghost" onClick={onCancel} label={t('Cancel')} />
        <Button size="sm" variant="primary" loading={saving} disabled={needsText && !text.trim()} onClick={() => void save()} testId="annotate-save" label={t('Save annotation')} />
      </div>
    </div>
  );
}

function NoteItem({ note, n, onDelete }: { note: Annotation; n: number; onDelete: () => void }) {
  return (
    <li className="fs-annot__note" data-type={note.type} data-sent={note.sent_at ? '' : undefined} data-testid="annotate-note">
      <span className="fs-annot__num">{n}</span>
      <div className="fs-annot__body">
        <span className="fs-annot__badge" data-type={note.type}>{t(TYPE_LABEL[note.type])}</span>
        {note.anchor.file && <code className="fs-annot__loc">{note.anchor.file}:{note.anchor.line}</code>}
        {note.quote && <q className="fs-annot__q">{note.quote}</q>}
        {note.text && <p>{note.type === 'suggestion' ? '→ ' : ''}{note.text}</p>}
        {note.sent_at && <small className="fs-annot__sent">{t('Sent to the agent')}</small>}
      </div>
      <IconButton icon={Trash2} label={t('Delete the annotation')} size="sm" onClick={onDelete} />
    </li>
  );
}

/* ── the diff ── */

function lineAnchor(file: DiffFile, hunk: DiffHunk, ln: DiffLine): AnnotationAnchor {
  const side = ln.k === 'del' ? 'old' : 'new';
  return { file: file.path, line: (side === 'old' ? ln.old : ln.new) ?? 0, side, hunk: hunk.id };
}

function HunkView({ file, hunk, notes, onLine, showPath }: {
  file: DiffFile; hunk: DiffHunk; notes: Annotation[]; onLine: (p: Pending) => void; showPath?: boolean;
}) {
  return (
    <div className="fs-annot__hunk" data-hunk={hunk.id}>
      <div className="fs-annot__hunkhead">{showPath && <strong>{file.path} </strong>}<code>{hunk.header}</code></div>
      <table className="fs-annot__diff"><tbody>
        {hunk.lines.map((ln, i) => {
          const anchor = lineAnchor(file, hunk, ln);
          const here = notes.filter(n => n.anchor.file === file.path && n.anchor.line === anchor.line && (n.anchor.side ?? 'new') === anchor.side);
          return [
            <tr key={i} data-k={ln.k} data-file={file.path} data-line={anchor.line} data-side={anchor.side} data-hunk={hunk.id}>
              <td className="fs-annot__ln">{ln.old ?? ''}</td>
              <td className="fs-annot__ln">{ln.new ?? ''}</td>
              <td className="fs-annot__add">
                <button type="button" aria-label={t('Comment on line {n}', { n: String(anchor.line) })} data-testid="annotate-line"
                  onClick={() => onLine({ quote: ln.text, anchor })}>+</button>
              </td>
              <td className="fs-annot__code"><span className="fs-annot__sign">{ln.k === 'add' ? '+' : ln.k === 'del' ? '-' : ' '}</span>{ln.text}</td>
            </tr>,
            here.length > 0 && (
              <tr key={i + 'n'} className="fs-annot__inline"><td colSpan={4}>
                {here.map(n => <div key={n.id} data-type={n.type}><span className="fs-annot__badge" data-type={n.type}>{t(TYPE_LABEL[n.type])}</span> {n.text}</div>)}
              </td></tr>
            ),
          ];
        })}
      </tbody></table>
    </div>
  );
}

function ReviewView({ review, notes, onLine, busyGuide, onGuide }: {
  review: Review; notes: Annotation[]; onLine: (p: Pending) => void; busyGuide: boolean; onGuide: () => void;
}) {
  const [mode, setMode] = useState<'files' | 'chapters'>(review.guide ? 'chapters' : 'files');
  useEffect(() => { if (review.guide) setMode('chapters'); }, [review.guide]);
  const byId = useMemo(() => {
    const m = new Map<string, { file: DiffFile; hunk: DiffHunk }>();
    for (const f of review.files) for (const h of f.hunks) m.set(h.id, { file: f, hunk: h });
    return m;
  }, [review.files]);
  return (
    <div className="fs-annot__review" data-testid="annotate-review">
      <div className="fs-annot__reviewbar">
        <span className="fs-annot__chip">{t('{files} files', { files: String(review.stats.files) })} · <ins>+{review.stats.added}</ins> <del>-{review.stats.removed}</del> · {t('against {base}', { base: review.base })}</span>
        <div className="fs-annot__seg" role="tablist">
          <button type="button" role="tab" aria-selected={mode === 'files'} onClick={() => setMode('files')}>{t('By file')}</button>
          <button type="button" role="tab" aria-selected={mode === 'chapters'} disabled={!review.guide} onClick={() => setMode('chapters')}>{t('By chapter')}</button>
        </div>
        <Button size="sm" icon={Sparkles} loading={busyGuide} onClick={onGuide} testId="annotate-guide" label={review.guide ? t('Guide again') : t('Guide')} />
      </div>
      {review.truncated && <p className="fs-annot__note-line">{t('The diff is long and was cut.')}</p>}
      {!review.files.length && <p className="fs-annot__empty">{t('No changes against {base}.', { base: review.base })}</p>}
      {mode === 'chapters' && review.guide ? (
        <div className="fs-annot__chapters">
          {review.guide.overview && <p className="fs-annot__overview">{review.guide.overview}</p>}
          {review.guide.chapters.map((ch, i) => (
            <section key={i} className="fs-annot__chapter" data-kind={ch.kind} data-testid="annotate-chapter">
              <header><span className="fs-annot__kind" data-kind={ch.kind}>{t(CHAPTER_LABEL[ch.kind] || ch.kind)}</span><h4>{ch.title}</h4></header>
              <p>{ch.summary}</p>
              {ch.hunks.map(id => { const hit = byId.get(id); return hit ? <HunkView key={id} file={hit.file} hunk={hit.hunk} notes={notes} onLine={onLine} showPath /> : null; })}
            </section>
          ))}
        </div>
      ) : review.files.map(f => (
        <details key={f.path} className="fs-annot__file" open={review.files.length <= 12}>
          <summary><code>{f.path}</code> <small data-status={f.status}>{t(f.status === 'added' ? 'New file' : f.status === 'deleted' ? 'Deleted file' : f.status === 'renamed' ? 'Renamed file' : 'Modified file')}</small> <ins>+{f.added}</ins> <del>-{f.removed}</del></summary>
          {f.hunks.map(h => <HunkView key={h.id} file={f} hunk={h} notes={notes} onLine={onLine} />)}
        </details>
      ))}
    </div>
  );
}

/* ── the tab ── */

export default function Annotate({ sessionId, turns, workspace, documents, hidden, maximized, busy, onToggleMaximize, onSend, onNotice }: {
  sessionId: string | null;
  turns: Turn[];
  workspace: string;
  documents: DocState[];
  hidden?: boolean;
  maximized?: boolean;
  busy?: boolean;
  onToggleMaximize?: () => void;
  /** Hands the composed message to the chat: sent now, or put in the composer while a turn runs. */
  onSend: (text: string, sendNow: boolean) => void;
  onNotice: (message: string, tone?: 'info' | 'warning' | 'danger') => void;
}) {
  const [target, setTarget] = useState<AnnotateTarget | null>(null);
  const [notes, setNotes] = useState<Annotation[]>([]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [frame, setFrame] = useState<Framable | null>(null);
  const [fileText, setFileText] = useState<string | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [guiding, setGuiding] = useState(false);
  const [picking, setPicking] = useState(false);
  const [sourceInput, setSourceInput] = useState<{ kind: 'file' | 'web' | 'diff'; value: string } | null>(null);
  const [error, setError] = useState('');
  const viewer = useRef<HTMLDivElement>(null);
  const iframe = useRef<HTMLIFrameElement>(null);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    try {
      const st = await getAnnotations(sessionId);
      setNotes(st.annotations);
      setTarget(cur => (st.target && (!cur || targetKey(cur) !== targetKey(st.target)) ? st.target : cur));
    } catch (e) { setError((e as Error).message); }
  }, [sessionId]);

  useEffect(() => { setTarget(null); setNotes([]); setReview(null); void refresh(); }, [sessionId, refresh]);
  useEffect(() => { if (!hidden) void refresh(); }, [hidden, refresh]);
  useEffect(() => onSurfaceEvent(e => {
    const kind = (e.data as { kind?: string })?.kind;
    if (kind !== 'anotar') return;
    if (e.event === 'open') {
      const tg = (e.data as { target?: AnnotateTarget }).target;
      if (tg) setTarget(tg);
      void refresh();
      if (tg?.kind === 'diff' && sessionId) void getReview(sessionId).then(setReview).catch(() => undefined);
    }
    if (e.event === 'meta' && sessionId) void getReview(sessionId).then(setReview).catch(() => undefined);
  }), [refresh, sessionId]);

  // What the target shows: the plan's text, a file's text, a framed page or the review.
  const planTurn = useMemo(() => {
    if (target?.kind !== 'plan') return null;
    if (!target.ref || target.ref === 'last') return lastAssistant(turns);
    return turns.find(tn => tn.dbId === target.ref || tn.id === target.ref) ?? null;
  }, [target, turns]);
  const doc = target?.kind === 'doc' ? documents.find(d => d.id === target.ref) ?? null : null;
  const htmlDoc = Boolean(doc && /html/i.test(doc.language));

  useEffect(() => {
    setFrame(null); setFileText(null); setError(''); setPicking(false); setPending(null);
    if (!target || !sessionId) return;
    let live = true;
    const fail = (e: unknown) => { if (live) setError((e as Error).message); };
    if (target.kind === 'web') void framePage({ url: target.ref }).then(f => live && setFrame(f)).catch(fail);
    else if (target.kind === 'file' && /\.html?$/i.test(target.ref)) void framePage({ path: target.ref }).then(f => live && setFrame(f)).catch(fail);
    else if (target.kind === 'file') void readWorkspaceFile(target.workspace || workspace, target.ref).then(f => live && setFileText(f.text)).catch(fail);
    else if (target.kind === 'diff') void getReview(sessionId).then(r => live && setReview(r)).catch(fail);
    return () => { live = false; };
  }, [target, sessionId, workspace]);
  useEffect(() => {
    if (!htmlDoc || !doc) return;
    let live = true;
    void framePage({ html: doc.content, name: doc.title }).then(f => live && setFrame(f)).catch(e => live && setError((e as Error).message));
    return () => { live = false; };
  }, [htmlDoc, doc?.content, doc?.title]); // eslint-disable-line react-hooks/exhaustive-deps

  const mine = useMemo(() => target ? notes.filter(n => targetKey(n.target) === targetKey(target)) : [], [notes, target]);
  const pendingAll = notes.filter(n => !n.sent_at).length;
  const pendingElsewhere = notes.filter(n => !n.sent_at && (!target || targetKey(n.target) !== targetKey(target))).length;

  // Highlights in the text views; numbered marks in a framed page.
  useEffect(() => {
    if (hidden) return;
    paintQuotes(frame || target?.kind === 'diff' ? null : viewer.current, mine);
  }, [mine, frame, target, hidden, planTurn?.text, fileText, doc?.content]);
  useEffect(() => () => paintQuotes(null, []), []);
  const sendMarks = useCallback(() => {
    if (!frame || !iframe.current?.contentWindow) return;
    const marks = mine.map((n, i) => ({ selector: n.anchor.selector, n: i + 1 })).filter(m => m.selector);
    iframe.current.contentWindow.postMessage({ source: 'faustus-studio', type: 'marks', marks }, frame.origin);
  }, [frame, mine]);
  useEffect(() => { sendMarks(); }, [sendMarks]);

  // Messages from the framed page: only from our frame and from the proxy's own origin.
  useEffect(() => {
    if (!frame) return;
    const onMessage = (e: MessageEvent) => {
      if (e.source !== iframe.current?.contentWindow || e.origin !== frame.origin) return;
      const d = e.data as { source?: string; type?: string; quote?: string; selector?: string; url?: string };
      if (d?.source !== 'faustus-annotate') return;
      if (d.type === 'ready') sendMarks();
      if (d.type === 'select' || d.type === 'pick') {
        setPicking(false);
        setPending({ quote: String(d.quote || ''), anchor: { selector: d.selector || undefined, url: d.url || undefined } });
      }
      if (d.type === 'pick-cancel') setPicking(false);
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [frame, sendMarks]);
  const togglePick = () => {
    const next = !picking;
    setPicking(next);
    iframe.current?.contentWindow?.postMessage({ source: 'faustus-studio', type: 'pick', on: next }, frame?.origin || '*');
  };

  // Text selection in the plan, a file or a document.
  const onMouseUp = () => {
    const sel = window.getSelection();
    const text = sel ? String(sel).trim() : '';
    if (!text || !viewer.current || !sel?.anchorNode || !viewer.current.contains(sel.anchorNode)) return;
    const row = (sel.anchorNode.nodeType === 1 ? sel.anchorNode as Element : sel.anchorNode.parentElement)?.closest('tr[data-line]') as HTMLElement | null;
    const anchor: AnnotationAnchor = row ? { file: row.dataset.file, line: Number(row.dataset.line), side: row.dataset.side as 'new' | 'old', hunk: row.dataset.hunk } : {};
    setPending({ quote: text.slice(0, 4000), anchor });
  };

  const save = async (type: AnnotationType, text: string) => {
    if (!sessionId || !target || !pending) return;
    try {
      const note = await addAnnotation(sessionId, { target, type, quote: pending.quote, text, anchor: pending.anchor });
      setNotes(cur => [...cur, note]);
      setPending(null);
      window.getSelection()?.removeAllRanges();
    } catch (e) { onNotice(`${t('Could not save the annotation.')} ${(e as Error).message}`, 'danger'); }
  };
  const remove = async (id: string) => {
    if (!sessionId) return;
    try { await deleteAnnotation(sessionId, id); setNotes(cur => cur.filter(n => n.id !== id)); }
    catch (e) { onNotice((e as Error).message, 'danger'); }
  };
  const send = async () => {
    if (!sessionId) return;
    try {
      const msg = await composeAnnotations(sessionId);
      if (!msg.count) { onNotice(t('There are no annotations waiting to be sent.'), 'info'); return; }
      onSend(msg.text, !busy);
      if (busy) onNotice(t('The agent is working: the annotations are in the composer, ready to send.'), 'info');
      await refresh();
    } catch (e) { onNotice(`${t('Could not send the annotations.')} ${(e as Error).message}`, 'danger'); }
  };
  const guide = async () => {
    if (!sessionId) return;
    setGuiding(true);
    try {
      await guideReview(sessionId);
      setReview(await getReview(sessionId));
    } catch (e) { onNotice(`${t('Could not order the changes.')} ${(e as Error).message}`, 'danger'); }
    finally { setGuiding(false); }
  };

  const choose = async (tg: AnnotateTarget) => {
    if (!sessionId) return;
    try { const res = await openTarget(sessionId, tg); setTarget(res.target); }
    catch (e) { onNotice((e as Error).message, 'danger'); }
  };
  const submitSource = async () => {
    if (!sourceInput || !sessionId) return;
    const value = sourceInput.value.trim();
    if (sourceInput.kind === 'diff') {
      if (!workspace) { onNotice(t('Pick a working folder first: the review looks at its changes.'), 'warning'); return; }
      try {
        const rv = await openReview(sessionId, workspace, value || 'HEAD');
        setReview(rv);
        setTarget({ kind: 'diff', ref: `${rv.workspace}@${rv.base}`, title: t('Changes against {base}', { base: rv.base }), workspace });
        setSourceInput(null);
      } catch (e) { onNotice((e as Error).message, 'danger'); }
      return;
    }
    if (!value) return;
    if (sourceInput.kind === 'web') await choose({ kind: 'web', ref: value, title: '' });
    else await choose({ kind: 'file', ref: /^([a-z]:[\\/]|\/)/i.test(value) || !workspace ? value : `${workspace.replace(/[\\/]+$/, '')}/${value}`, title: value.split(/[\\/]/).pop() || value, workspace });
    setSourceInput(null);
  };
  const pickPlan = () => {
    const turn = lastAssistant(turns);
    if (!turn) { onNotice(t('The agent has not answered in this chat yet.'), 'info'); return; }
    void choose({ kind: 'plan', ref: turn.dbId || turn.id, title: planTitle(turn.text) || t('Plan') });
  };
  const pickDoc = (d: DocState) => { if (d.id) void choose({ kind: 'doc', ref: d.id, title: d.title || t('Document') }); };

  if (!sessionId) return <div className="fs-panel__body" hidden={hidden}><p>{t('Send a first message to annotate in this chat.')}</p></div>;
  const Icon = target ? KIND_ICON[target.kind] : MessageSquareQuote;
  const planText = planTurn?.text ?? '';
  return (
    <div className="fs-annot" hidden={hidden} data-testid="annotate-tab">
      <div className="fs-annot__bar">
        <Icon size={14} aria-hidden="true" />
        <strong className="fs-annot__title" title={target?.ref}>{target ? (target.title || target.ref) : t('Annotate')}</strong>
        <div className="fs-annot__sources">
          <Button size="sm" variant="ghost" onClick={pickPlan} testId="annotate-src-plan" label={t('Plan')} />
          <Button size="sm" variant="ghost" onClick={() => setSourceInput({ kind: 'file', value: '' })} testId="annotate-src-file" label={t('File')} />
          <Button size="sm" variant="ghost" onClick={() => setSourceInput({ kind: 'web', value: 'http://localhost:5173/' })} testId="annotate-src-web" label={t('Local web')} />
          <Button size="sm" variant="ghost" onClick={() => setSourceInput({ kind: 'diff', value: 'HEAD' })} testId="annotate-src-diff" label={t('Changes')} />
          {documents.filter(d => d.id).length > 0 && (
            <select aria-label={t('Annotate a document')} value="" onChange={e => { const d = documents.find(x => x.id === e.target.value); if (d) pickDoc(d); }}>
              <option value="">{t('Document…')}</option>
              {documents.filter(d => d.id).map(d => <option key={d.id!} value={d.id!}>{d.title || t('Document')}</option>)}
            </select>
          )}
        </div>
        {frame && <IconButton icon={Crosshair} label={picking ? t('Stop picking (Esc)') : t('Pick an element of the page')} size="sm" onClick={togglePick} testId="annotate-pick" />}
        {frame && <IconButton icon={RotateCw} label={t('Reload the page')} size="sm" onClick={() => { if (iframe.current) iframe.current.src = frame.url; }} />}
        {onToggleMaximize && <IconButton icon={maximized ? Minimize2 : Maximize2} label={maximized ? t('Bring the chat back (Esc)') : t('Fill the window')} size="sm" onClick={onToggleMaximize} />}
      </div>
      {sourceInput && (
        <form className="fs-annot__source" onSubmit={e => { e.preventDefault(); void submitSource(); }}>
          <label>
            <span>{sourceInput.kind === 'file' ? t('File (path in the workspace)') : sourceInput.kind === 'web' ? t('Local address') : t('Compare against (HEAD = uncommitted changes, or a branch)')}</span>
            <input autoFocus value={sourceInput.value} onChange={e => setSourceInput({ ...sourceInput, value: e.target.value })} data-testid="annotate-src-input"
              placeholder={sourceInput.kind === 'file' ? 'docs/PLAN.md' : sourceInput.kind === 'web' ? 'http://localhost:5173/' : 'HEAD'} />
          </label>
          <Button size="sm" variant="primary" type="submit" testId="annotate-src-open" label={t('Open')} />
          <IconButton icon={X} label={t('Cancel')} size="sm" onClick={() => setSourceInput(null)} />
        </form>
      )}
      {error && <p className="fs-notice" data-tone="warning" role="alert">{error}</p>}
      <div className="fs-annot__main">
        <div className="fs-annot__viewer" ref={viewer} onMouseUp={onMouseUp} data-kind={target?.kind}>
          {!target && <div className="fs-annot__empty"><ListTree size={18} aria-hidden="true" /><p>{t('Choose what to annotate: the agent\'s plan, a file, a document, a local web page or the uncommitted changes. Select text or a line, write a note, and send them all to the agent.')}</p></div>}
          {target?.kind === 'plan' && (planText ? <div className="fs-annot__doc"><Rich text={planText} /></div> : <p className="fs-annot__empty">{t('The agent has not answered in this chat yet.')}</p>)}
          {target?.kind === 'file' && fileText !== null && (/\.(md|markdown|txt)$/i.test(target.ref) ? <div className="fs-annot__doc"><Rich text={fileText} /></div> : <pre className="fs-annot__pre">{fileText}</pre>)}
          {target?.kind === 'doc' && doc && !htmlDoc && <div className="fs-annot__doc"><Rich text={doc.content} /></div>}
          {target?.kind === 'doc' && !doc && <p className="fs-annot__empty">{t('That document is not open in this chat.')}</p>}
          {frame && <iframe ref={iframe} className="fs-annot__frame" src={frame.url} title={target?.title || t('Page')} data-testid="annotate-frame"
            onLoad={() => iframe.current?.contentWindow?.postMessage({ source: 'faustus-studio', type: 'hello' }, frame.origin)} />}
          {target?.kind === 'diff' && review && <ReviewView review={review} notes={mine} onLine={setPending} busyGuide={guiding} onGuide={() => void guide()} />}
        </div>
        <aside className="fs-annot__side" aria-label={t('Annotations')}>
          {pending && <NoteForm key={pending.quote + JSON.stringify(pending.anchor)} pending={pending} onSave={save} onCancel={() => setPending(null)} />}
          {!pending && target && <p className="fs-annot__hint">{target.kind === 'diff' ? t('Press + on a line, or select code, to comment on it.') : frame ? t('Select text in the page, or pick an element, to annotate it.') : t('Select text to annotate it.')}</p>}
          <ol className="fs-annot__list">
            {mine.map((n, i) => <NoteItem key={n.id} note={n} n={i + 1} onDelete={() => void remove(n.id)} />)}
          </ol>
          {pendingElsewhere > 0 && <p className="fs-annot__hint">{t('{n} more waiting on other targets.', { n: String(pendingElsewhere) })}</p>}
          <Button variant="primary" icon={Send} disabled={!pendingAll} onClick={() => void send()} testId="annotate-send"
            label={pendingAll ? t('Send to the agent ({n})', { n: String(pendingAll) }) : t('Send to the agent')} />
        </aside>
      </div>
    </div>
  );
}
