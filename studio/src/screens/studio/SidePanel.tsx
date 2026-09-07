import { Archive, Check, Copy, FileText, Globe, History, Monitor, Save, SkipForward, X, Users, Paperclip, Files } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router';
import { Button, IconButton, Skeleton } from '../../components';
import { archiveDoc, docPdfUrl, getDoc, listDocVersions, renameDoc, restoreDocVersion, saveDoc, type DocVersion } from '../../adapters/documents';
import { readWorkspaceFile, saveWorkspaceFile, type WorkspaceFileText } from '../../adapters/workspace';
import { Rich } from '../rich';
import { autoOpenEnabled, setAutoOpen, fileKey,docKey,type PanelDraft,type DocState, type PanelAction, type PanelState, type PanelTab } from './panel';
import {WorkbenchResources} from './WorkbenchResources';
import SubagentBoard from './SubagentBoard';
import type {Turn} from './model';
import type {Project} from '../../adapters/projects';
import type {DelegationTask} from '../../adapters/chat';
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
  turns:Turn[]; workspace:string; project:Project|null; busy:boolean;
  onRerun:(task:DelegationTask)=>void;
}

const TABS: { id: PanelTab; label: string; icon: typeof Globe }[] = [
  {id:'outputs',label:'Results',icon:Files},
  {id:'sources',label:'Sources',icon:Paperclip},
  {id:'agents',label:'Agents',icon:Users},
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

function DocTab({ doc, draft, dispatch, onNotice }: { doc: DocState | null; draft?:PanelDraft; dispatch: SidePanelProps['dispatch']; onNotice: SidePanelProps['onNotice'] }) {
  const text=draft?.text ?? doc?.content ?? '';
  const setText=(text:string)=>{if(doc)dispatch({type:'draft',key:docKey(doc),draft:{text,base:draft?.base??doc.content}});};
  const [title, setTitle] = useState(doc?.title ?? '');
  const [saving, setSaving] = useState(false);
  const mutation = useRef(false);
  const [preview, setPreview] = useState(true);
  const [versions, setVersions] = useState<DocVersion[] | null>(null);
  const [loading, setLoading] = useState(false);
  const dirty = doc ? text !== doc.content : false;

  // The server's content wins whenever the document changes under us
  // (a new stream, a doc_update, a version restore) — never while typing.
  useEffect(() => {
    setTitle(doc?.title ?? '');
    setVersions(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.id, doc?.version, doc?.streaming ? doc.content : '']);

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
    if (!doc.id || mutation.current) return false;
    mutation.current = true;
    setSaving(true);
    try {
      const saved = await saveDoc(doc.id, content, summary, false, draft?.base??doc.content);
      dispatch({type:'draft-saved',key:docKey(doc),submitted:{text:content,base:draft?.base??doc.content},base:saved.content});
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

  const current = doc.suggestions[0];
  const applySuggestion = async (all: boolean) => {
    if (mutation.current) return;
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
    if (next !== text && !await save(next, all ? t('Agent\'s suggestions applied') : t('Agent\'s suggestion applied'))) return;
    dispatch({ type: 'suggestions', docId: doc.id, suggestions: doc.suggestions.filter((sg) => !applied.includes(sg.id)) });
  };

  return (
    <div className="fs-panel__body fs-panel__doc">
      <div className="fs-panel__doc-head">
        <input className="fs-panel__title" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => void rename()} aria-label={t('Document title')} disabled={!doc.id || saving || doc.streaming} />
        {doc.language && <code className="fs-sa__model">{doc.language}</code>}
        {doc.streaming && <span className="fs-panel__live"><span className="fs-studio__pulse" /> {t('Writing')}</span>}
        {!doc.streaming && doc.id && <span className="fs-sa__muted">v{doc.version}</span>}
      </div>

      {current && !doc.streaming && (
        <div className="fs-panel__suggestion" data-testid="doc-suggestion">
          <p>{t('Suggestion')}{doc.suggestions.length > 1 ? ` · 1 / ${doc.suggestions.length}` : ''}</p>
          {current.reason && <p>{current.reason}</p>}
          <pre className="fs-panel__diff"><span className="fs-diff-del">− {current.find}</span><span className="fs-diff-add">+ {current.replace}</span></pre>
          <div className="fs-panel__row">
            <Button size="sm" variant="primary" icon={Check} label={t('Apply')} disabled={saving} onClick={() => void applySuggestion(false)} />
            <Button size="sm" icon={SkipForward} label={t('Skip')} disabled={saving} onClick={() => dispatch({ type: 'suggestions', docId: doc.id, suggestions: doc.suggestions.slice(1) })} />
            {doc.suggestions.length > 1 && <Button size="sm" label={t('Apply all')} disabled={saving} onClick={() => void applySuggestion(true)} />}
          </div>
        </div>
      )}

      {preview && !doc.streaming ? (
        <div className="fs-panel__preview"><Rich text={text} /></div>
      ) : (
        <textarea
          className="fs-panel__editor"
          value={text}
          disabled={saving}
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
          {draft&&<Button size="sm" label={t('Discard draft')} disabled={saving} onClick={()=>dispatch({type:'draft',key:docKey(doc),draft:null})}/>}
          {draft&&draft.base!==doc.content&&<p role="status">{t('The agent updated this document. Your draft is preserved; review before saving.')}</p>}
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

/* ── File ── */

function FileTab({ file,draft,dispatch, onNotice }: { file: PanelState['file']; draft?:PanelDraft; dispatch:SidePanelProps['dispatch']; onNotice: SidePanelProps['onNotice'] }) {
  const [data, setData] = useState<WorkspaceFileText | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview,setPreview]=useState(true),[saving,setSaving]=useState(false),[reload,setReload]=useState(0);
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
  }catch(e){setError((e as Error).message);}finally{setSaving(false);}};
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
      {draft&&data&&draft.base!==data.text&&<p role="status">{t('The file changed outside this editor. Your draft is preserved.')}</p>}
      {editable&&(preview?<div className="fs-panel__preview"><Rich text={text}/></div>:<textarea className="fs-panel__editor" aria-label={t('File content')} value={text} disabled={saving} onChange={e=>dispatch({type:'draft',key:fileKey(file),draft:{text:e.target.value,base:draft?.base??data!.text,revision:draft?.revision??data!.revision}})}/>)}
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

export default function SidePanel({ state, dispatch, onNotice,turns,workspace,project,busy,onRerun }: SidePanelProps) {
  const tabStrip=useRef<HTMLDivElement>(null);
  useEffect(()=>{tabStrip.current?.querySelector('[aria-selected=true]')?.scrollIntoView({block:'nearest',inline:'nearest'});},[state.tab]);
  const tabs=TABS.filter(tab=>tab.id!=='doc'&&tab.id!=='file'||tab.id==='doc'&&state.doc||tab.id==='file'&&state.file);
  const workers=[...new Map(turns.flatMap(turn=>turn.workers).map(worker=>[worker.id,worker])).values()];
  return (
    <aside className="fs-panel" data-testid="studio-panel" aria-label={t('Side panel')}>
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
      <label className="fs-panel__resize">{t('Panel width')}<input type="range" min={320} max={900} step={20} value={state.width} onChange={e=>dispatch({type:'width',width:Number(e.target.value)})}/></label>
      {(state.documents.length>0||state.files.length>0)&&<div className="fs-workbench-open" aria-label={t('Open results')}>
        {state.documents.map(doc=><span key={docKey(doc)}><button type="button" aria-pressed={state.tab==='doc'&&state.doc?.id===doc.id} onClick={()=>dispatch({type:'doc',doc})}>{doc.title||t('Document')}{state.drafts[docKey(doc)]?' •':''}</button><button type="button" aria-label={t('Close {name}',{name:doc.title})} disabled={Boolean(state.drafts[docKey(doc)])} onClick={()=>dispatch({type:'forget',key:docKey(doc)})}><X size={13}/></button></span>)}
        {state.files.map(file=><span key={fileKey(file)}><button type="button" aria-pressed={state.tab==='file'&&state.file?.path===file.path} onClick={()=>dispatch({type:'file',...file})}>{file.path.split(/[\\/]/).pop()}{state.drafts[fileKey(file)]?' •':''}</button><button type="button" aria-label={t('Close {name}',{name:file.path})} disabled={Boolean(state.drafts[fileKey(file)])} onClick={()=>dispatch({type:'forget',key:fileKey(file)})}><X size={13}/></button></span>)}
      </div>}
      <div className="fs-workbench-content" role="tabpanel" id="workbench-content" aria-labelledby={'workbench-tab-'+state.tab}>
      {(state.tab==='outputs'||state.tab==='sources')&&<WorkbenchResources kind={state.tab} state={state} turns={turns} workspace={workspace} project={project} dispatch={dispatch}/>}
      {state.tab==='agents'&&<div className="fs-panel__body"><h3>{t('Agents in this conversation')}</h3>{workers.length?<SubagentBoard workers={workers} live={busy} onRerun={onRerun} onNotice={onNotice}/>:<p>{t('No agents have worked in this conversation yet. Configure a team beside the model picker.')}</p>}</div>}
      {state.tab === 'browser' && <BrowserTab state={state} dispatch={dispatch} />}
      {state.tab === 'doc' && <DocTab key={state.doc?.id||'streaming'} doc={state.doc} draft={state.doc?state.drafts[docKey(state.doc)]:undefined} dispatch={dispatch} onNotice={onNotice} />}
      {state.tab === 'file' && <FileTab key={state.file?fileKey(state.file):'none'} file={state.file} draft={state.file?state.drafts[fileKey(state.file)]:undefined} dispatch={dispatch} onNotice={onNotice} />}
      </div>
    </aside>
  );
}
