import { Archive, Check, Copy, FileText, Globe, History, Monitor, Save, SkipForward, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router';
import { Button, IconButton, Skeleton } from '../../components';
import { archiveDoc, docPdfUrl, getDoc, listDocVersions, renameDoc, restoreDocVersion, saveDoc, type DocVersion } from '../../adapters/documents';
import { readWorkspaceFile, type WorkspaceFileText } from '../../adapters/workspace';
import { Rich } from '../rich';
import { autoOpenEnabled, setAutoOpen, type DocState, type PanelAction, type PanelState, type PanelTab } from './panel';
import { t, tn } from '../../i18n';

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
}

const TABS: { id: PanelTab; label: string; icon: typeof Globe }[] = [
  { id: 'browser', label: 'Browser', icon: Globe },
  { id: 'doc', label: 'Document', icon: FileText },
  { id: 'file', label: 'File', icon: Monitor },
];

/* ── Browser ── */

function BrowserTab({ state, dispatch }: { state: PanelState; dispatch: SidePanelProps['dispatch'] }) {
  const [auto, setAuto] = useState(autoOpenEnabled);
  const frame = state.active >= 0 ? state.frames[state.active] : null;
  return (
    <div className="fs-panel__body fs-panel__browser">
      <div className="fs-panel__meta">
        <span className="fs-panel__kicker">
          {frame?.source === 'desktop' ? 'Desktop' : 'Browser'}
          {state.live && (
            <span className="fs-panel__live" title={t('The agent is using the browser right now')}>
              <span className="fs-studio__pulse" /> En vivo
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
          Abrir solo
        </label>
      </div>
      {frame ? (
        <>
          <p className="fs-panel__page">
            <strong>{frame.title}</strong>
            {frame.url && <span title={frame.url}>{frame.url}</span>}
          </p>
          <img className="fs-panel__frame" src={frame.src} alt={frame.title || t('Browser screen')} />
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

function DocTab({ doc, dispatch, onNotice }: { doc: DocState | null; dispatch: SidePanelProps['dispatch']; onNotice: SidePanelProps['onNotice'] }) {
  const [text, setText] = useState(doc?.content ?? '');
  const [title, setTitle] = useState(doc?.title ?? '');
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState(false);
  const [versions, setVersions] = useState<DocVersion[] | null>(null);
  const [loading, setLoading] = useState(false);
  const dirty = doc ? text !== doc.content : false;

  // The server's content wins whenever the document changes under us
  // (a new stream, a doc_update, a version restore) — never while typing.
  useEffect(() => {
    setText(doc?.content ?? '');
    setTitle(doc?.title ?? '');
    setVersions(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.id, doc?.version, doc?.streaming ? doc.content : '']);

  // Opened by id only (suggestions or a tool result without doc_update): fetch it.
  useEffect(() => {
    if (!doc?.id || doc.version !== 0 || doc.streaming) return;
    const id = doc.id;
    setLoading(true);
    getDoc(id)
      .then((d) => dispatch({ type: 'doc', doc: { streaming: false, id: d.id, title: d.title, language: d.language, content: d.content, version: d.versionCount, suggestions: doc.suggestions } }))
      .catch(() => onNotice(t('Could not load the document.'), 'danger'))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.id, doc?.version]);

  if (!doc) return <p className="fs-studio__hint fs-panel__body">{t('When the agent creates or edits a document, it appears here to read and edit.')}</p>;
  if (loading) return <div className="fs-panel__body"><Skeleton label={t('Loading the document')} count={6} height="20px" /></div>;

  const save = async (content = text, summary?: string) => {
    if (!doc.id) return;
    setSaving(true);
    try {
      const saved = await saveDoc(doc.id, content, summary);
      dispatch({ type: 'doc', doc: { ...doc, content: saved.content, version: saved.versionCount, title: saved.title, language: saved.language } });
      onNotice(t('Saved (v{n}).', { n: saved.versionCount }));
    } catch (e) {
      onNotice(`${t('Could not save')}: ${(e as Error).message}`, 'danger');
    } finally {
      setSaving(false);
    }
  };

  const rename = async () => {
    const next = title.trim();
    if (!doc.id || !next || next === doc.title) return;
    try {
      const saved = await renameDoc(doc.id, next);
      dispatch({ type: 'doc', doc: { ...doc, title: saved.title } });
    } catch (e) {
      onNotice(`${t('Could not rename')}: ${(e as Error).message}`, 'danger');
    }
  };

  const current = doc.suggestions[0];
  const applySuggestion = async (all: boolean) => {
    let next = text;
    const applied: string[] = [];
    for (const sg of all ? doc.suggestions : doc.suggestions.slice(0, 1)) {
      if (next.includes(sg.find)) {
        next = next.replace(sg.find, sg.replace);
        applied.push(sg.id);
      } else if (!all) {
        onNotice(t('The text it wants to change is no longer in the document; skipping it.'), 'warning');
        applied.push(sg.id);
      }
    }
    setText(next);
    dispatch({ type: 'suggestions', suggestions: doc.suggestions.filter((sg) => !applied.includes(sg.id)) });
    if (next !== text) await save(next, all ? t('Agent\'s suggestions applied') : t('Agent\'s suggestion applied'));
  };

  return (
    <div className="fs-panel__body fs-panel__doc">
      <div className="fs-panel__doc-head">
        <input className="fs-panel__title" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => void rename()} aria-label={t('Document title')} disabled={!doc.id} />
        {doc.language && <code className="fs-sa__model">{doc.language}</code>}
        {doc.streaming && <span className="fs-panel__live"><span className="fs-studio__pulse" /> Escribiendo</span>}
        {!doc.streaming && doc.id && <span className="fs-sa__muted">v{doc.version}</span>}
      </div>

      {current && !doc.streaming && (
        <div className="fs-panel__suggestion" data-testid="doc-suggestion">
          <p className="fs-panel__kicker">Sugerencia {doc.suggestions.length > 1 ? `1 de ${doc.suggestions.length}` : ''}</p>
          {current.reason && <p>{current.reason}</p>}
          <pre className="fs-panel__diff"><span className="fs-diff-del">− {current.find}</span><span className="fs-diff-add">+ {current.replace}</span></pre>
          <div className="fs-panel__row">
            <Button size="sm" variant="primary" icon={Check} label={t('Apply')} onClick={() => void applySuggestion(false)} />
            <Button size="sm" icon={SkipForward} label={t('Skip')} onClick={() => dispatch({ type: 'suggestions', suggestions: doc.suggestions.slice(1) })} />
            {doc.suggestions.length > 1 && <Button size="sm" label={t('Apply all')} onClick={() => void applySuggestion(true)} />}
          </div>
        </div>
      )}

      {preview && !doc.streaming ? (
        <div className="fs-panel__preview"><Rich text={text} /></div>
      ) : (
        <textarea
          className="fs-panel__editor"
          value={text}
          onChange={(e) => setText(e.target.value)}
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
                onClick={() => {
                  archiveDoc(doc.id as string)
                    .then(() => {
                      dispatch({ type: 'doc', doc: null });
                      onNotice(t('Document archived.'));
                    })
                    .catch(() => onNotice(t('Could not archive.'), 'danger'));
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
                  onClick={() => {
                    restoreDocVersion(doc.id as string, v.number)
                      .then((d) => {
                        dispatch({ type: 'doc', doc: { ...doc, content: d.content, version: d.versionCount } });
                        setVersions(null);
                        onNotice(t('Restored v{a} as v{b}.', { a: v.number, b: d.versionCount }));
                      })
                      .catch(() => onNotice(t('Could not restore.'), 'danger'));
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

/* ── File ── */

function FileTab({ file, onNotice }: { file: PanelState['file']; onNotice: SidePanelProps['onNotice'] }) {
  const [data, setData] = useState<WorkspaceFileText | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!file) return;
    const controller = new AbortController();
    setData(null);
    setError(null);
    readWorkspaceFile(file.workspace, file.path, controller.signal)
      .then(setData)
      .catch((e: Error) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [file]);
  const lines = useMemo(() => {
    if (!data?.text) return [];
    const parts = data.text.split('\n');
    return data.text.endsWith('\n') ? parts.slice(0, -1) : parts;
  }, [data]);
  if (!file) return <p className="fs-studio__hint fs-panel__body">{t('Click a file in a turn\'s card to see it here.')}</p>;
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
      {data && !data.binary && (
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

export default function SidePanel({ state, dispatch, onNotice }: SidePanelProps) {
  return (
    <aside className="fs-panel" data-testid="studio-panel" aria-label={t('Side panel')}>
      <header className="fs-panel__head">
        <div className="fs-panel__tabs" role="tablist">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={state.tab === tab.id}
              className="fs-panel__tab"
              onClick={() => dispatch({ type: 'tab', tab: tab.id })}
              data-has={tab.id === 'browser' ? state.frames.length > 0 || undefined : tab.id === 'doc' ? Boolean(state.doc) || undefined : Boolean(state.file) || undefined}
            >
              <tab.icon size={13} aria-hidden="true" />
              <span>{t(tab.label)}</span>
            </button>
          ))}
        </div>
        <IconButton icon={X} label={t('Close the panel')} size="sm" onClick={() => dispatch({ type: 'close' })} />
      </header>
      {state.tab === 'browser' && <BrowserTab state={state} dispatch={dispatch} />}
      {state.tab === 'doc' && <DocTab doc={state.doc} dispatch={dispatch} onNotice={onNotice} />}
      {state.tab === 'file' && <FileTab file={state.file} onNotice={onNotice} />}
    </aside>
  );
}
