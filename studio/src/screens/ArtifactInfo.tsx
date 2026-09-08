import {useEffect, useState} from 'react';
import {Link} from 'react-router';
import {loadArtifactInfo, type ArtifactInfo as Info} from '../adapters/artifact-info';
import {locale, t} from '../i18n';

/** Read metadata only on demand; a file download never requires this fetch. */
export function ArtifactInfo({id}:{id:string}) {
  const [open,setOpen] = useState(false);
  const [info,setInfo] = useState<Info|null>(null);
  const [error,setError] = useState('');
  const [attempt,setAttempt] = useState(0);
  useEffect(()=>{
    if (!open) return;
    const abort = new AbortController();
    setInfo(null); setError('');
    loadArtifactInfo(id,abort.signal).then(value=>{if(!abort.signal.aborted)setInfo(value);})
      .catch(()=>{if(!abort.signal.aborted)setError(t('Could not read file provenance. Retry or download the file.'));});
    return ()=>abort.abort();
  },[id,open,attempt]);
  return <details className="fs-act__artifact-info" open={open} onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary>{t('File provenance')}</summary>
    {open && <>
      {error ? <p role="alert">{error} <button type="button" onClick={()=>setAttempt(n=>n+1)}>{t('Retry')}</button></p> : !info ? <p role="status">{t('Loading file provenance…')}</p> : <>
        <dl className="fs-act__facts">
          {[[t('Name'),info.label],[t('Type'),info.mediaType||info.kind],[t('Size'),`${info.byteSize.toLocaleString(locale())} B`],
            [t('Status'),info.partial?t('Partial output'):t('Complete output')],[t('Created'),info.createdAt],
            [t('Project'),info.projectId],[t('Originating run'),info.runId],['SHA-256',info.sha256]].filter(([,value])=>value).map(([label,value])=><div className="fs-act__fact" key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
        </dl>
        {info.sessionId && <Link className="fs-act__link" to={`/studio?s=${encodeURIComponent(info.sessionId)}`}>{t('Open the source conversation')}</Link>}
      </>}
    </>}
  </details>;
}
