import { Archive, Check, Copy, FileText, GitBranch, Globe, History, Kanban, Monitor, MessageSquarePlus, Redo2, Save, SkipForward, Undo2, X, Users, Paperclip, Files } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { Link } from 'react-router';
import { Button, IconButton, Popover, Skeleton } from '../../components';
import { archiveDoc, docPdfUrl, getDoc, listDocVersions, renameDoc, restoreDocVersion, saveDoc, type DocVersion } from '../../adapters/documents';
import { readWorkspaceFile, saveWorkspaceFile, type WorkspaceFileText } from '../../adapters/workspace';
import { ApiError } from '../../adapters/api';
import { threeWayLines, threeWaySummary } from '../../adapters/fileConflict';
import { Rich } from '../rich';
import { autoOpenEnabled, setAutoOpen, fileKey,docKey,type PanelDraft,type DocState, type PanelAction, type PanelState, type PanelTab } from './panel';
import {
  currentText, discardDraft, findOccurrences, isDirty, markSaved, mergeSuggestions,
  redo as sessionRedo, replaceAt, sendComposerContext, setDraftText, setSelection, setSuggestions,
  sync as syncSession, undo as sessionUndo, useDocSession,
  type DocRange, type Occurrence, type PendingSuggestion,
} from '../../lib/docSession';
import {WorkbenchResources} from './WorkbenchResources';
import SubagentBoard from './SubagentBoard';
import { SourceControlPanel } from '../source-control/SourceControlPanel';
import { BoardCompact } from '../board/BoardCompact';
import FrameSelection, {type VisualSelection} from './FrameSelection';
import { aggregateFileChanges } from '../../adapters/workbenchChanges';
import { isInsecureRemoteAccess } from '../../adapters/remoteAccess';
import type {Turn} from './model';
import type {Project} from '../../adapters/projects';
import type {DelegationTask} from '../../adapters/chat';
import { t, tn } from '../../i18n';
// CMP-01/02 (W2-A1): the occurrence picker and selection chip's styles
// (`.fs-occurrences`, `.fs-selchip*`) live in `documents.css` alongside
// the rest of this lot's document CSS, not in `studio.css` (a different
// lot's file this wave) — imported here so `DocTab` can use them.
import '../documents.css';

/**
 * The panel beside the transcript. Three things live here, each on its own
 * tab: the frames the agent's browser (or the desktop) produced, the living
 * document the agent is writing — with a real editor: save, rename,
 * versions, PDF, the agent's suggestions — and a file from the workspace.
 */

export interface SidePanelProps {
  state: PanelState;
  dispatch: (action: PanelAction) => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  turns:Turn[]; workspace:string; project:Project|null; busy:boolean;
  onRerun:(task:DelegationTask)=>void;
  onVisualSelection?:(selection:VisualSelection)=>Promise<void>;
}

const TABS: { id: PanelTab; label: string; icon: typeof Globe }[] = [
  {id:'outputs',label:'Results',icon:Files},
  {id:'sources',label:'Sources',icon:Paperclip},
  {id:'agents',label:'Agents',icon:Users},
  { id: 'browser', label: 'Browser', icon: Globe },
  { id: 'doc', label: 'Document', icon: FileText },
  { id: 'file', label: 'File', icon: Monitor },
  // Lote 86 (CONTRATO_GIT_4.md): only meaningful once there is a workspace
  // or project to find a repo in — see the `tabs` filter below.
  { id: 'git', label: 'Source control', icon: GitBranch },
  // Lote 93 (CONTRATO_BOARD.md): the project's own board — needs a project
  // (the board is scoped by project id, not by a bare workspace folder).
  { id: 'board', label: 'Board', icon: Kanban },
];

/* ── Browser ── */

function BrowserTab({ state, dispatch, onVisualSelection }: { state: PanelState; dispatch: SidePanelProps['dispatch']; onVisualSelection?:SidePanelProps['onVisualSelection'] }) {
  const [auto, setAuto] = useState(autoOpenEnabled);
  const frame = state.active >= 0 ? state.frames[state.active] : null;
  return (
    <div className="fs-panel__body fs-panel__browser">
      <div className="fs-panel__meta">
        <span className="fs-panel__kicker">
          {t(frame?.source === 'desktop' ? 'Desktop' : 'Browser')}
          {state.live && (
            <span className="fs-panel__live" title={t('The agent is using the browser right now')}>
              <span className="fs-studio__pulse" /> {t('Live')}
            </span>
          )}
        </span>
        <label className="fs-panel__auto">
          <input
            type="checkbox"
            checked={auto}
            onChange={(e) => {
              setAuto(e.target.checked);
              setAutoOpen(e.target.checked);
            }}
          />
          {t('Open automatically')}
        </label>
      </div>
      {frame ? (
        <>
          <p className="fs-panel__page">
            <strong>{frame.title}</strong>
            {frame.url && <span title={frame.url}>{frame.url}</span>}
          </p>
          {onVisualSelection&&frame.source==='browser'?<FrameSelection frame={frame} onAdd={onVisualSelection}/>:<img className="fs-panel__frame" src={frame.src} alt={frame.title || t('Browser screen')} />}
        </>
      ) : (
        <p className="fs-studio__hint">{t('What the agent sees when it uses the browser or the desktop appears here.')}</p>
      )}
      {state.frames.length > 1 && (
        <div className="fs-panel__strip" role="list">
          {state.frames.map((f, i) => (
            <button
              key={f.at + i}
              type="button"
              role="listitem"
              className="fs-panel__thumb"
              aria-current={i === state.active || undefined}
              title={`${f.source === 'desktop' ? `${t('Desktop')} · ` : ''}${f.title || f.url}`}
              onClick={() => dispatch({ type: 'show', index: i })}
            >
              <img src={f.src} alt="" />
              <span>{f.source === 'desktop' ? '🖥 ' : ''}{i + 1}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Document ── */

/** The current selection inside the document, as index ranges into
 *  `text` — captured from the `<textarea>`'s native `selectionStart`/
 *  `selectionEnd` (the preview is rendered HTML; mapping a DOM selection
 *  there back to source offsets is not attempted here — CMP-01 decision
 *  doc notes it as a follow-up). Written into the shared `docSession` so
 *  it survives switching to the full editor and back, per CMP-01's "el
 *  panel y Editor.tsx la LEEN y ESCRIBEN". */
function useTextSelection(docId: string | undefined | null, textareaRef: RefObject<HTMLTextAreaElement | null>) {
  const [range, setRange] = useState<DocRange | null>(null);
  const onSelect = () => {
    const el = textareaRef.current;
    if (!el || !docId) return;
    if (el.selectionStart === el.selectionEnd) {
      setRange(null);
      return;
    }
    const r = { start: el.selectionStart, end: el.selectionEnd };
    setRange(r);
    setSelection(docId, [r]);
  };
  return { range, onSelect };
}

/** "Sobre esta selección…" (CMP-03, §3.2): turns a selection into a
 *  structured context reference for the NEXT chat message, via
 *  `sendComposerContext` — never a copy of the text into the transcript as
 *  though the human had typed it, and never an edit made on its own. */
function SelectionChip({ doc, text, range, onNotice }: { doc: DocState; text: string; range: DocRange; onNotice: SidePanelProps['onNotice'] }) {
  const quote = text.slice(range.start, range.end);
  const act = (action: 'clarify' | 'keep_term' | 'fix') => {
    if (!doc.id) return;
    sendComposerContext({ doc: { id: doc.id, title: doc.title }, ranges: [range], action, items: [{ quote }] });
    onNotice(t('Added as context for your next message.'));
  };
  return (
    <Popover
      testId="doc-selection-chip"
      trigger={<Button size="sm" icon={MessageSquarePlus} label={t('About this selection…')} />}
    >
      <div className="fs-selchip">
        <p className="fs-selchip__quote">“{quote.length > 140 ? `${quote.slice(0, 140)}…` : quote}”</p>
        <div className="fs-panel__row">
          <Button size="sm" label={t('Clarify')} onClick={() => act('clarify')} />
          <Button size="sm" label={t('Keep this wording')} onClick={() => act('keep_term')} />
          <Button size="sm" label={t('Fix this')} onClick={() => act('fix')} />
        </div>
      </div>
    </Popover>
  );
}

/** CMP-02 (§3.2): `find` occurs more than once — show every occurrence
 *  with its surrounding text and let the person pick, or replace every
 *  occurrence identically ("todas"). Never a silent first-match. */
function OccurrencePicker({ occurrences, onPick, onAll, onCancel }: { occurrences: Occurrence[]; onPick: (o: Occurrence) => void; onAll: () => void; onCancel: () => void }) {
  return (
    <div className="fs-occurrences" role="group" aria-label={t('Which occurrence?')} data-testid="doc-occurrences">
      <p>{tn(occurrences.length, 'This text appears {n} time — choose which one.', 'This text appears {n} times — choose which one.')}</p>
      <ul>
        {occurrences.map((o, i) => (
          <li key={o.start}>
            <button type="button" onClick={() => onPick(o)} data-testid={`doc-occurrence-${i}`}>
              <span className="fs-sa__muted">…{o.before}</span>
              <mark>{'…'}</mark>
              <span className="fs-sa__muted">{o.after}…</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="fs-panel__row">
        <Button size="sm" variant="primary" label={t('Apply to all occurrences')} onClick={onAll} testId="doc-occurrence-all" />
        <Button size="sm" label={t('Cancel')} onClick={onCancel} />
      </div>
    </div>
  );
}

function DocTab({ doc, dispatch, onNotice }: { doc: DocState | null; dispatch: SidePanelProps['dispatch']; onNotice: SidePanelProps['onNotice'] }) {
  // CMP-01: identity/draft/selection/undo/proposals live in the shared
  // `docSession`, not component state — that is what makes them survive
  // switching to the full editor (a different mounted tree) and back.
  const session = useDocSession(doc?.id ?? null, doc ? { content: doc.content, version: doc.version } : undefined);
  const text = session ? currentText(session) : doc?.content ?? '';
  const dirty = session ? isDirty(session) : false;
  const [title, setTitle] = useState(doc?.title ?? '');
  const [saving, setSaving] = useState(false);
  const mutation = useRef(false);
  const [preview, setPreview] = useState(true);
  const [versions, setVersions] = useState<DocVersion[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [occurrences, setOccurrences] = useState<{ sg: PendingSuggestion; occ: Occurrence[] } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const selection = useTextSelection(doc?.id, textareaRef);

  // The server's content wins whenever the document changes under us
  // (a new stream, a doc_update, a version restore) — never while typing;
  // `syncSession` keeps a pending draft instead of discarding it.
  useEffect(() => {
    setTitle(doc?.title ?? '');
    setVersions(null);
    if (doc?.id && !doc.streaming) syncSession(doc.id, { content: doc.content, version: doc.version });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.id, doc?.version, doc?.streaming ? doc.content : '']);

  // The chat stream is the only source of new suggestions; mirror them
  // into the session so the full editor (which never sees SSE events) can
  // still see what is pending after a mode switch.
  useEffect(() => {
    if (doc?.id && doc.suggestions.length) mergeSuggestions(doc.id, doc.suggestions);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.id, doc?.suggestions]);

  // Opened by id only (suggestions or a tool result without doc_update): fetch it.
  useEffect(() => {
    if (!doc?.id || doc.version !== 0 || doc.streaming) return;
    const id = doc.id;
    const abort = new AbortController();
    setLoading(true);
    getDoc(id, abort.signal)
      .then((d) => {if(!abort.signal.aborted)dispatch({ type: 'doc-saved', doc: { streaming: false, id: d.id, title: d.title, language: d.language, content: d.content, version: d.versionCount, suggestions: doc.suggestions } });})
      .catch(() => {if(!abort.signal.aborted)onNotice(t('Could not load the document.'), 'danger');})
      .finally(() => {if(!abort.signal.aborted)setLoading(false);});
    return ()=>abort.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.id, doc?.version]);

  if (!doc) return <p className="fs-studio__hint fs-panel__body">{t('When the agent creates or edits a document, it appears here to read and edit.')}</p>;
  if (loading) return <div className="fs-panel__body"><Skeleton label={t('Loading the document')} count={6} height="20px" /></div>;

  const save = async (content = text, summary?: string) => {
    if (!doc.id || mutation.current || !session) return false;
    mutation.current = true;
    setSaving(true);
    try {
      const saved = await saveDoc(doc.id, content, summary, false, session.baseText);
      markSaved(doc.id, { content: saved.content, version: saved.versionCount });
      dispatch({ type: 'doc-saved', doc: { ...doc, content: saved.content, version: saved.versionCount, title: saved.title, language: saved.language } });
      onNotice(t('Saved (v{n}).', { n: saved.versionCount }));
      return true;
    } catch (e) {
      onNotice(`${t('Could not save')}: ${(e as Error).message}`, 'danger');
      return false;
    } finally {
      mutation.current = false;
      setSaving(false);
    }
  };

  const rename = async () => {
    const next = title.trim();
    if (!doc.id || !next || next === doc.title || mutation.current) return;
    mutation.current = true;
    setSaving(true);
    try {
      const saved = await renameDoc(doc.id, next);
      dispatch({ type: 'doc-renamed', id: doc.id, title: saved.title });
    } catch (e) {
      onNotice(`${t('Could not rename')}: ${(e as Error).message}`, 'danger');
    } finally {
      mutation.current = false;
      setSaving(false);
    }
  };

  const suggestions = session?.suggestions ?? [];
  const current = suggestions[0];

  /** Removes `ids` from BOTH the session (so they stay gone across a mode
   *  switch) and the panel's live `doc.suggestions` (so the merge effect
   *  above does not resurrect them the next time it runs). */
  const removeSuggestions = (ids: string[]) => {
    if (!doc.id) return;
    setSuggestions(doc.id, suggestions.filter((s) => !ids.includes(s.id)));
    dispatch({ type: 'suggestions', docId: doc.id, suggestions: doc.suggestions.filter((s) => !ids.includes(s.id)) });
  };

  /** Applies ONE suggestion at a known, already-disambiguated span (or the
   *  single unambiguous occurrence) — never at "the first occurrence". */
  const applyAt = async (sg: PendingSuggestion, range: DocRange | 'all') => {
    if (!doc.id || mutation.current) return;
    const before = text;
    const next = range === 'all' ? before.split(sg.find).join(sg.replace) : replaceAt(before, range.start, range.end, sg.replace);
    setDraftText(doc.id, next, { record: true });
    setOccurrences(null);
    if (next !== before && !(await save(next, t("Agent's suggestion applied")))) return;
    removeSuggestions([sg.id]);
  };

  /** CMP-02: locate `sg.find`; zero occurrences skips it (as before), one
   *  occurrence applies it, more than one shows the picker — the bug this
   *  fiche exists to fix was applying blindly to the first match. */
  const applySuggestion = (sg: PendingSuggestion) => {
    if (!doc.id) return;
    const occ = findOccurrences(text, sg.find);
    if (occ.length === 0) {
      onNotice(t('The text it wants to change is no longer in the document; skipping it.'), 'warning');
      removeSuggestions([sg.id]);
      return;
    }
    if (occ.length === 1) { void applyAt(sg, occ[0]); return; }
    setOccurrences({ sg, occ });
  };

  /** "Apply all" = walk every pending suggestion once, in order, on the
   *  text as it stands after the previous one. The first suggestion that
   *  turns out ambiguous stops the bulk pass and opens the picker for it —
   *  it never guesses which occurrence a bulk action meant either. */
  const applyAllSuggestions = async () => {
    if (!doc.id || mutation.current || !session) return;
    let working = text;
    const resolved: string[] = [];
    for (const sg of suggestions) {
      const occ = findOccurrences(working, sg.find);
      if (occ.length === 0) { resolved.push(sg.id); continue; }
      if (occ.length > 1) { setOccurrences({ sg, occ }); break; }
      working = replaceAt(working, occ[0].start, occ[0].end, sg.replace);
      resolved.push(sg.id);
    }
    if (working !== text) {
      setDraftText(doc.id, working, { record: true });
      if (!(await save(working, t("Agent's suggestions applied")))) return;
    }
    if (resolved.length) removeSuggestions(resolved);
  };

  return (
    <div className="fs-panel__body fs-panel__doc">
      <div className="fs-panel__doc-head">
        <input className="fs-panel__title" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => void rename()} aria-label={t('Document title')} disabled={!doc.id || saving || doc.streaming} />
        {doc.language && <code className="fs-sa__model">{doc.language}</code>}
        {doc.streaming && <span className="fs-panel__live"><span className="fs-studio__pulse" /> {t('Writing')}</span>}
        {!doc.streaming && doc.id && <span className="fs-sa__muted">v{doc.version}</span>}
      </div>

      {occurrences && (
        <OccurrencePicker
          occurrences={occurrences.occ}
          onPick={(o) => void applyAt(occurrences.sg, o)}
          onAll={() => void applyAt(occurrences.sg, 'all')}
          onCancel={() => setOccurrences(null)}
        />
      )}

      {current && !doc.streaming && !occurrences && (
        <div className="fs-panel__suggestion" data-testid="doc-suggestion">
          <p>{t('Suggestion')}{suggestions.length > 1 ? ` · 1 / ${suggestions.length}` : ''}</p>
          {current.reason && <p>{current.reason}</p>}
          <pre className="fs-panel__diff"><span className="fs-diff-del">− {current.find}</span><span className="fs-diff-add">+ {current.replace}</span></pre>
          <div className="fs-panel__row">
            <Button size="sm" variant="primary" icon={Check} label={t('Apply')} disabled={saving} onClick={() => applySuggestion(current)} />
            <Button size="sm" icon={SkipForward} label={t('Skip')} disabled={saving} onClick={() => removeSuggestions([current.id])} />
            {suggestions.length > 1 && <Button size="sm" label={t('Apply all')} disabled={saving} onClick={() => void applyAllSuggestions()} />}
          </div>
        </div>
      )}

      {selection.range && !doc.streaming && !preview && (
        <div className="fs-panel__row fs-selchip-row">
          <SelectionChip doc={doc} text={text} range={selection.range} onNotice={onNotice} />
        </div>
      )}

      {preview && !doc.streaming ? (
        <div className="fs-panel__preview"><Rich text={text} /></div>
      ) : (
        <textarea
          ref={textareaRef}
          className="fs-panel__editor"
          value={text}
          disabled={saving}
          onChange={(e) => doc.id && setDraftText(doc.id, e.target.value)}
          onSelect={selection.onSelect}
          readOnly={doc.streaming || !doc.id}
          spellCheck={false}
          aria-label={t('Document content')}
          data-testid="doc-editor"
        />
      )}

      {!doc.streaming && (
        <div className="fs-panel__row fs-panel__doc-actions">
          <Button size="sm" variant="primary" icon={Save} label={dirty ? t('Save') : t('Saved')} disabled={!dirty || !doc.id} loading={saving} onClick={() => void save()} testId="doc-save" />
          <Button size="sm" label={preview ? t('Edit') : t('Preview')} onClick={() => setPreview((v) => !v)} />
          {session && session.undoStack.length > 0 && <IconButton icon={Undo2} label={t('Undo')} size="sm" onClick={() => doc.id && sessionUndo(doc.id)} />}
          {session && session.redoStack.length > 0 && <IconButton icon={Redo2} label={t('Redo')} size="sm" onClick={() => doc.id && sessionRedo(doc.id)} />}
          {dirty&&<Button size="sm" label={t('Discard draft')} disabled={saving} onClick={()=>doc.id && discardDraft(doc.id)}/>}
          {session?.rebasedPending&&<p role="status">{t('The agent updated this document. Your draft is preserved; review before saving.')}</p>}
          <IconButton icon={Copy} label={t('Copy the content')} size="sm" onClick={() => void navigator.clipboard?.writeText(text)} />
          {doc.id && (
            <>
              <IconButton
                icon={History}
                label={t('Versions')}
                size="sm"
                onClick={() => {
                  if (versions) setVersions(null);
                  else listDocVersions(doc.id as string).then(setVersions).catch(() => onNotice(t('Could not read the versions.'), 'danger'));
                }}
              />
              <a className="fs-btn" data-size="sm" href={docPdfUrl(doc.id)} target="_blank" rel="noreferrer">
                <span>PDF</span>
              </a>
              <Link className="fs-btn" data-size="sm" to={`/documents/${encodeURIComponent(doc.id)}`} title={t('Toolbar, find, versions with review, export, PDF pages and signatures')}>
                <span>{t('Full editor')}</span>
              </Link>
              <IconButton
                icon={Archive}
                label={t('Archive')}
                size="sm"
                disabled={dirty || saving}
                onClick={() => {
                  if (mutation.current || dirty) return;
                  mutation.current = true;
                  setSaving(true);
                  archiveDoc(doc.id as string)
                    .then(() => {
                      dispatch({ type: 'forget', key: docKey(doc) });
                      onNotice(t('Document archived.'));
                    })
                    .catch(() => onNotice(t('Could not archive.'), 'danger'))
                    .finally(() => {mutation.current = false; setSaving(false);});
                }}
              />
            </>
          )}
        </div>
      )}

      {versions && (
        <ul className="fs-panel__versions">
          {versions.map((v) => (
            <li key={v.id}>
              <span>
                v{v.number} · {v.source === 'user' ? t('you') : t('agent')} · {v.summary || '—'}
              </span>
              {v.number !== doc.version && (
                <Button
                  size="sm"
                  label={t('Restore')}
                  disabled={dirty || saving || doc.streaming}
                  onClick={() => {
                    if (mutation.current || dirty) return;
                    mutation.current = true;
                    setSaving(true);
                    restoreDocVersion(doc.id as string, v.number, doc.content)
                      .then((d) => {
                        markSaved(doc.id as string, { content: d.content, version: d.versionCount });
                        dispatch({ type: 'doc-saved', doc: { ...doc, content: d.content, version: d.versionCount } });
                        setVersions(null);
                        onNotice(t('Restored v{a} as v{b}.', { a: v.number, b: d.versionCount }));
                      })
                      .catch((e:Error) => onNotice(`${t('Could not restore.')} ${e.message}`, 'danger'))
                      .finally(() => {mutation.current = false; setSaving(false);});
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ── File save conflict (BENCH-04) ── */

/** What a save collided with: base (what the draft started from), mine
 * (what was about to be written) and theirs (what is on disk now). Rows
 * where only one side changed are shown but need no decision; a `diverged`
 * row is the only kind that actually competes for the same line. Either
 * resolution keeps a full copy of both sides available until it is picked —
 * nothing is discarded by opening this view. */
function ConflictView({ base, mine, theirs, saving, onKeepMine, onTakeTheirs, onCancel }: {
  base: string; mine: string; theirs: string; saving: boolean;
  onKeepMine: () => void; onTakeTheirs: () => void; onCancel: () => void;
}) {
  const rows = useMemo(() => threeWayLines(base, mine, theirs), [base, mine, theirs]);
  const summary = useMemo(() => threeWaySummary(rows), [rows]);
  return (
    <div className="fs-panel__conflict" role="alert" data-testid="file-conflict">
      <p>
        {t('Someone else saved this file first.')}{' '}
        {summary.diverged > 0
          ? tn(summary.diverged, '{n} line changed on both sides.', '{n} lines changed on both sides.')
          : t('The two edits do not touch the same lines.')}
      </p>
      <div className="fs-panel__conflict-rows" role="table" aria-label={t('Three-way comparison')}>
        {rows.map((row, i) => (
          <div className="fs-panel__conflict-row" role="row" data-status={row.status} key={i}>
            <span role="cell" aria-label={t('Yours')}>{row.mine ?? ''}</span>
            <span role="cell" aria-label={t('On disk now')}>{row.theirs ?? ''}</span>
          </div>
        ))}
      </div>
      <div className="fs-panel__row">
        <Button size="sm" variant="primary" label={t('Keep mine (overwrite)')} disabled={saving} onClick={onKeepMine} />
        <Button size="sm" label={t('Take the newer version')} disabled={saving} onClick={onTakeTheirs} />
        <Button size="sm" label={t('Cancel')} disabled={saving} onClick={onCancel} />
      </div>
    </div>
  );
}

/* ── File ── */

function FileTab({ file,draft,dispatch, onNotice }: { file: PanelState['file']; draft?:PanelDraft; dispatch:SidePanelProps['dispatch']; onNotice: SidePanelProps['onNotice'] }) {
  const [data, setData] = useState<WorkspaceFileText | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview,setPreview]=useState(true),[saving,setSaving]=useState(false),[reload,setReload]=useState(0);
  // BENCH-04: a 409 (routes/workspace_routes.py — the file's revision moved
  // under the draft) parks the conflict here instead of discarding either
  // side; `mine` is exactly what was about to be saved.
  const [conflict, setConflict] = useState<{ mine: string; theirs: WorkspaceFileText } | null>(null);
  useEffect(() => {
    if (!file) return;
    const controller = new AbortController();
    setData(null);
    setError(null);
    readWorkspaceFile(file.workspace, file.path, controller.signal)
      .then(d=>{if(!controller.signal.aborted)setData(d);})
      .catch((e: Error) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [file,reload]);
  const lines = useMemo(() => {
    if (!data?.text) return [];
    const parts = data.text.split('\n');
    return data.text.endsWith('\n') ? parts.slice(0, -1) : parts;
  }, [data]);
  if (!file) return <p className="fs-studio__hint fs-panel__body">{t('Click a file in a turn\'s card to see it here.')}</p>;
  const text=draft?.text??data?.text??'';
  const editable=Boolean(data&&!data.binary&&!data.truncated&&data.revision&&/\.(md|markdown|txt)$/i.test(file.path));
  const save=async()=>{if(!editable||saving||!data)return;setSaving(true);setError(null);try{
    const saved=await saveWorkspaceFile(file.workspace,file.path,text,draft?.revision||data.revision!);
    setData(saved);dispatch({type:'draft-saved',key:fileKey(file),submitted:{text,base:draft?.base??data.text,revision:draft?.revision??data.revision},base:saved.text,revision:saved.revision});onNotice(t('File saved.'));
  }catch(e){
    if(e instanceof ApiError && e.status===409){
      // Never lose the draft: park it in `conflict.mine` and fetch what is
      // on disk now, so the three-way view has all three sides.
      try{const theirs=await readWorkspaceFile(file.workspace,file.path);setConflict({mine:text,theirs});}
      catch(reReadError){setError((reReadError as Error).message);}
    } else setError((e as Error).message);
  }finally{setSaving(false);}};
  const keepMine=async()=>{
    if(!conflict||saving)return;setSaving(true);setError(null);try{
      const saved=await saveWorkspaceFile(file.workspace,file.path,conflict.mine,conflict.theirs.revision!);
      setData(saved);dispatch({type:'draft-saved',key:fileKey(file),submitted:{text:conflict.mine,base:draft?.base??conflict.theirs.text,revision:conflict.theirs.revision},base:saved.text,revision:saved.revision});
      setConflict(null);onNotice(t('Saved your version over the newer one (v{n}).',{n:saved.revision?.slice(0,7)||''}));
    }catch(e){setError((e as Error).message);}finally{setSaving(false);}
  };
  const takeTheirs=()=>{
    if(!conflict)return;
    setData(conflict.theirs);dispatch({type:'draft',key:fileKey(file),draft:null});setConflict(null);
    onNotice(t('Loaded the newer version; your draft was discarded.'));
  };
  return (
    <div className="fs-panel__body fs-panel__file">
      <p className="fs-panel__page">
        <strong title={data?.path ?? file.path}>{data?.rel ?? file.path}</strong>
        {data && <span>{tn(data.lines, '{n} line', '{n} lines')} · {data.size} B{data.truncated ? t(' · truncated') : ''}</span>}
        <IconButton icon={Copy} label={t('Copy the path')} size="sm" onClick={() => void navigator.clipboard?.writeText(data?.path ?? file.path).then(() => onNotice(t('Path copied.')))} />
      </p>
      {error && <p className="fs-notice" data-tone="danger">{error}</p>}
      {!data && !error && <Skeleton label={t('Reading the file')} count={8} height="16px" />}
      {data?.binary && <p className="fs-studio__hint">{t('It is a binary file.')}</p>}
      {editable&&<div className="fs-panel__row"><Button label={t('Save')} disabled={!draft} loading={saving} onClick={()=>void save()}/><Button label={t(preview?'Edit':'Preview')} onClick={()=>setPreview(v=>!v)}/><Button label={t('Reload file')} disabled={saving} onClick={()=>setReload(n=>n+1)}/>{draft&&<Button label={t('Discard draft')} disabled={saving} onClick={()=>dispatch({type:'draft',key:fileKey(file),draft:null})}/>}</div>}
      {draft&&data&&draft.base!==data.text&&!conflict&&<p role="status">{t('The file changed outside this editor. Your draft is preserved.')}</p>}
      {conflict&&data&&<ConflictView base={draft?.base??data.text} mine={conflict.mine} theirs={conflict.theirs.text} saving={saving} onKeepMine={()=>void keepMine()} onTakeTheirs={takeTheirs} onCancel={()=>setConflict(null)}/>}
      {editable&&!conflict&&(preview?<div className="fs-panel__preview"><Rich text={text}/></div>:<textarea className="fs-panel__editor" aria-label={t('File content')} value={text} disabled={saving} onChange={e=>dispatch({type:'draft',key:fileKey(file),draft:{text:e.target.value,base:draft?.base??data!.text,revision:draft?.revision??data!.revision}})}/>)}
      {data && !data.binary && !editable && (
        <pre className="fs-panel__code">
          {lines.map((line, i) => (
            <span key={i} className="fs-panel__line">
              <span className="fs-panel__ln">{i + 1}</span>
              {line || ' '}
            </span>
          ))}
        </pre>
      )}
    </div>
  );
}

/* ── Plan & changes (BENCH-05) ── */

/** The turn's own plan (`plan_state`'s projection, already carried on
 * `Turn.planSteps` — TASK-01/CALL-07) and its files touched so far, grouped
 * by file (`aggregateFileChanges`) instead of one card per tool call. Reads
 * data the transcript already has; no new backend call. */
function PlanAndChanges({ turns }: { turns: Turn[] }) {
  const planTurn = [...turns].reverse().find((turn) => turn.planSteps && turn.planSteps.length > 0);
  const changeTurn = [...turns].reverse().find((turn) => turn.steps.some((s) => s.diff));
  const changes = changeTurn ? aggregateFileChanges(changeTurn.steps.filter((s) => s.diff).map((s) => s.diff!)) : [];
  if (!planTurn && changes.length === 0) return null;
  return (
    <div className="fs-panel__body fs-panel__plan-changes">
      {planTurn?.planSteps && (
        <section aria-label={t('Plan')}>
          <h3>{t('Plan')}</h3>
          <ol className="fs-panel__plan-list">
            {planTurn.planSteps.map((step) => (
              <li key={step.id} data-status={step.status}>
                <span>{step.title}</span>
                <span className="fs-sa__muted">
                  {step.status === 'blocked' ? t('blocked') : step.status === 'done' ? t('done') : t('pending')}
                  {step.verified ? ` · ${t('verified')}` : ''}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}
      {changes.length > 0 && (
        <section aria-label={t('Changes')}>
          <h3>{t('Changes')}</h3>
          <ul className="fs-panel__changes-list">
            {changes.map((row) => (
              <li key={row.file}>
                <span title={row.file}>{row.file.split(/[\\/]/).pop()}</span>
                <span className="fs-diff-stat">
                  {row.newFile && <em>{t('new')}</em>}
                  {row.added > 0 && <ins>+{row.added}</ins>}
                  {row.removed > 0 && <del>−{row.removed}</del>}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

/** One tab in the "open documents" strip. A separate component (not inlined
 *  in the `.map` below) because it needs its own `useDocSession` — reading
 *  the shared session is what shows the same "unsaved" dot the DocTab
 *  itself shows, now sourced from `docSession` instead of the panel's own
 *  (per-key, doc-and-file-shared) draft map. */
function OpenDocChip({ doc, active, dispatch }: { doc: DocState; active: boolean; dispatch: SidePanelProps['dispatch'] }) {
  const session = useDocSession(doc.id);
  const dirty = session ? isDirty(session) : false;
  return (
    <span>
      <button type="button" aria-pressed={active} onClick={() => dispatch({ type: 'doc', doc })}>{doc.title || t('Document')}{dirty ? ' •' : ''}</button>
      <button type="button" aria-label={t('Close {name}', { name: doc.title })} disabled={dirty} onClick={() => dispatch({ type: 'forget', key: docKey(doc) })}><X size={13} /></button>
    </span>
  );
}

export default function SidePanel({ state, dispatch, onNotice,turns,workspace,project,busy,onRerun,onVisualSelection }: SidePanelProps) {
  const tabStrip=useRef<HTMLDivElement>(null);
  useEffect(()=>{tabStrip.current?.querySelector('[aria-selected=true]')?.scrollIntoView({block:'nearest',inline:'nearest'});},[state.tab]);
  const tabs=TABS.filter(tab=>
    (tab.id!=='doc'||state.doc) &&
    (tab.id!=='file'||state.file) &&
    (tab.id!=='git'||Boolean(workspace||project)) &&
    (tab.id!=='board'||Boolean(project)),
  );
  const workers=[...new Map(turns.flatMap(turn=>turn.workers).map(worker=>[worker.id,worker])).values()];
  return (
    <aside className="fs-panel" data-testid="studio-panel" aria-label={t('Side panel')}>
      <div
        className="fs-panel__grip"
        role="separator"
        aria-orientation="vertical"
        aria-label={t('Resize the panel')}
        aria-valuemin={320}
        aria-valuemax={900}
        aria-valuenow={state.width}
        tabIndex={0}
        title={t('Drag to resize')}
        onPointerDown={(e) => {
          // Drag the panel's left edge, like any split view. Pointer capture
          // keeps the drag alive when the cursor leaves the 6px grip.
          e.preventDefault();
          const startX = e.clientX;
          const startW = state.width;
          const target = e.currentTarget;
          target.setPointerCapture(e.pointerId);
          const move = (ev: PointerEvent) => {
            const next = Math.min(900, Math.max(320, Math.round(startW + (startX - ev.clientX))));
            dispatch({ type: 'width', width: next });
          };
          const up = () => {
            target.releasePointerCapture(e.pointerId);
            target.removeEventListener('pointermove', move);
            target.removeEventListener('pointerup', up);
            target.removeEventListener('pointercancel', up);
          };
          target.addEventListener('pointermove', move);
          target.addEventListener('pointerup', up);
          target.addEventListener('pointercancel', up);
        }}
        onKeyDown={(e) => {
          // Keyboard users resize with the arrows (the grip is a separator).
          const step = e.shiftKey ? 80 : 20;
          if (e.key === 'ArrowLeft') { e.preventDefault(); dispatch({ type: 'width', width: Math.min(900, state.width + step) }); }
          if (e.key === 'ArrowRight') { e.preventDefault(); dispatch({ type: 'width', width: Math.max(320, state.width - step) }); }
        }}
      />
      <header className="fs-panel__head">
        <div className="fs-panel__tabs" role="tablist" ref={tabStrip}>
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={state.tab === tab.id}
              tabIndex={state.tab===tab.id?0:-1}
              id={'workbench-tab-'+tab.id}
              aria-controls="workbench-content"
              className="fs-panel__tab"
              onClick={() => dispatch({ type: 'tab', tab: tab.id })}
              onKeyDown={event=>{const delta=event.key==='ArrowRight'?1:event.key==='ArrowLeft'?-1:0;
                if(delta||event.key==='Home'||event.key==='End'){event.preventDefault();const index=event.key==='Home'?0:event.key==='End'?tabs.length-1:(tabs.findIndex(v=>v.id===tab.id)+delta+tabs.length)%tabs.length;dispatch({type:'tab',tab:tabs[index].id});document.getElementById('workbench-tab-'+tabs[index].id)?.focus();}}}
              data-has={tab.id === 'browser' ? state.frames.length > 0 || undefined : tab.id === 'doc' ? Boolean(state.doc) || undefined : tab.id==='file'?Boolean(state.file)||undefined:undefined}
            >
              <tab.icon size={13} aria-hidden="true" />
              <span>{t(tab.label)}</span>
            </button>
          ))}
        </div>
        <IconButton icon={X} label={t('Close the panel')} size="sm" onClick={() => dispatch({ type: 'close' })} />
      </header>
      {typeof location!=='undefined'&&isInsecureRemoteAccess(location.hostname,location.protocol)&&(
        <p className="fs-notice" data-tone="warning" role="alert">{t('This page is reachable over plain HTTP from outside this machine — set up HTTPS or a tunnel before using it remotely.')}</p>
      )}
      {(state.documents.length>0||state.files.length>0)&&<div className="fs-workbench-open" aria-label={t('Open results')}>
        {state.documents.map(doc=><OpenDocChip key={docKey(doc)} doc={doc} active={state.tab==='doc'&&state.doc?.id===doc.id} dispatch={dispatch}/>)}
        {state.files.map(file=><span key={fileKey(file)}><button type="button" aria-pressed={state.tab==='file'&&state.file?.path===file.path} onClick={()=>dispatch({type:'file',...file})}>{file.path.split(/[\\/]/).pop()}{state.drafts[fileKey(file)]?' •':''}</button><button type="button" aria-label={t('Close {name}',{name:file.path})} disabled={Boolean(state.drafts[fileKey(file)])} onClick={()=>dispatch({type:'forget',key:fileKey(file)})}><X size={13}/></button></span>)}
      </div>}
      <div className="fs-workbench-content" role="tabpanel" id="workbench-content" aria-labelledby={'workbench-tab-'+state.tab}>
      {(state.tab==='outputs'||state.tab==='sources')&&<WorkbenchResources kind={state.tab} state={state} turns={turns} workspace={workspace} project={project} dispatch={dispatch}/>}
      {state.tab==='agents'&&<><PlanAndChanges turns={turns}/><div className="fs-panel__body"><h3>{t('Agents in this conversation')}</h3>{workers.length?<SubagentBoard workers={workers} live={busy} onRerun={onRerun} onNotice={onNotice}/>:<p>{t('No agents have worked in this conversation yet. Configure a team beside the model picker.')}</p>}</div></>}
      {state.tab === 'browser' && <BrowserTab state={state} dispatch={dispatch} onVisualSelection={onVisualSelection} />}
      {state.tab === 'doc' && <DocTab key={state.doc?.id||'streaming'} doc={state.doc} dispatch={dispatch} onNotice={onNotice} />}
      {state.tab === 'file' && <FileTab key={state.file?fileKey(state.file):'none'} file={state.file} draft={state.file?state.drafts[fileKey(state.file)]:undefined} dispatch={dispatch} onNotice={onNotice} />}
      {state.tab === 'git' && (workspace || project) && (
        <div className="fs-panel__body">
          <SourceControlPanel compact projectId={project?.id} workspace={workspace} />
        </div>
      )}
      {state.tab === 'board' && project && (
        <div className="fs-panel__body">
          <BoardCompact projectId={project.id} openIssueId={state.boardIssue} />
        </div>
      )}
      </div>
    </aside>
  );
}
