import {useEffect,useRef,useState} from 'react';
import {Captions} from 'lucide-react';
import {Button,Popover} from '../../components';
import {t} from '../../i18n';

export interface Subtitle {start:number;end:number;text:string}
export function subtitleText(rows:Subtitle[],format:'srt'|'vtt'):string {
  if(!rows.length||rows.length>80)throw new Error(t('Add between 1 and 80 subtitle segments.'));
  let previous=0;
  const timestamp=(time:number)=>{const ms=Math.round(time*1000);return `${String(Math.floor(ms/3600000)).padStart(2,'0')}:${String(Math.floor(ms/60000)%60).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}${format==='srt'?',':'.'}${String(ms%1000).padStart(3,'0')}`;};
  const blocks=rows.map((row,index)=>{
    if(!Number.isFinite(row.start)||!Number.isFinite(row.end)||row.start<previous||row.end-row.start<.1||row.end>180||!row.text.trim()||row.text.length>1000)throw new Error(t('Check segment times and text: ordered, non-overlapping, within 3 minutes.'));
    previous=row.end;
    // Subtitles are text, not HTML, VTT settings or additional cue blocks.
    const text=row.text.replace(/\r/g,'').replace(/[<>\u0000]/g,'').split('\n').filter(line=>line.trim()).join('\n');
    return `${index+1}\n${timestamp(row.start)} --> ${timestamp(row.end)}\n${text}`;
  });
  return (format==='vtt'?'WEBVTT\n\n':'')+blocks.join('\n\n')+'\n';
}
function download(blob:Blob,name:string){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}

export default function LocalVideo(){return <VideoForm/>;}
function VideoForm(){
  const [file,setFile]=useState<File|null>(null),[preview,setPreview]=useState(''),[rows,setRows]=useState<Subtitle[]>([]),[language,setLanguage]=useState('auto'),[voice,setVoice]=useState('es');
  const [busy,setBusy]=useState(''),[error,setError]=useState(''),[seconds,setSeconds]=useState(0),[caps,setCaps]=useState<{ffmpeg:boolean;whisper:boolean;system_voice:boolean}|null>(null);
  const active=useRef<AbortController|null>(null),mounted=useRef(true);
  useEffect(()=>{mounted.current=true;const controller=new AbortController();fetch('/api/media/local-video/capabilities',{signal:controller.signal,credentials:'same-origin'}).then(async r=>{if(!r.ok)throw new Error(t('Could not check local video tools.'));return r.json();}).then(value=>{if(!controller.signal.aborted)setCaps(value);}).catch(e=>{if(!controller.signal.aborted)setError(e.message);});return()=>{mounted.current=false;controller.abort();active.current?.abort();};},[]);
  useEffect(()=>{if(!file){setPreview('');return;}const url=URL.createObjectURL(file);setPreview(url);return()=>URL.revokeObjectURL(url);},[file]);
  useEffect(()=>{if(!busy)return;setSeconds(0);const start=Date.now(),timer=setInterval(()=>setSeconds(Math.floor((Date.now()-start)/1000)),1000);return()=>clearInterval(timer);},[busy]);
  const run=async(mode:'transcribe'|'dub')=>{
    if(!file||busy)return;setError('');const controller=new AbortController();active.current=controller;
    try{if(mode==='dub')subtitleText(rows,'srt');setBusy(mode);const body=new FormData();body.append('file',file);body.append('language',mode==='dub'?voice:language);body.append('segments',JSON.stringify(rows));
      const response=await fetch('/api/media/local-video/'+mode,{method:'POST',body,credentials:'same-origin',signal:controller.signal});
      if(!response.ok){const result=await response.json();throw new Error(typeof result.detail==='string'?result.detail:t('Local video processing failed.'));}
      if(mode==='transcribe'){const result=await response.json();if(!Array.isArray(result.segments))throw new Error(t('The transcript response is invalid.'));subtitleText(result.segments,'srt');if(mounted.current)setRows(result.segments);}
      else{const blob=await response.blob();if(mounted.current)download(blob,'localized.mp4');}
    }catch(e){if(!controller.signal.aborted)setError((e as Error).message);}finally{if(mounted.current)setBusy('');}
  };
  return <Popover side="top" className="fs-media-recipes" trigger={<button type="button" className="fs-studio__chip"><Captions size={13}/>{t('Local video')}{busy?'…':''}</button>}><section aria-label={t('Local video')}><h3>{t('Local video')}</h3>
    <p>{t('Offline subtitles and narration for clips up to 3 minutes, 1080p and 64 MB. No cloud calls or automatic model downloads.')}</p>
    <p>{t('Transcribe with existing Whisper weights, then edit or translate the segments yourself. Dubbing uses an installed Windows voice and replaces the original audio; it does not clone voices or synchronize lips.')}</p>
    {caps&&<p>{t('Local tools:')} FFmpeg {caps.ffmpeg?t('Available'):t('Unavailable')} · Whisper {caps.whisper?t('Installed'):t('Not installed')} · {t('System voice')} {caps.system_voice?t('Available'):t('Unavailable')}</p>}
    <label>{t('Video file')}<input type="file" accept=".mp4,.mov,.webm,.mkv" disabled={!!busy} onChange={e=>{const chosen=e.target.files?.[0];if(!chosen)return;if(!/\.(mp4|mov|webm|mkv)$/i.test(chosen.name)||chosen.size>64*1024*1024||!chosen.size){setError(t('Choose a non-empty MP4, MOV, WebM or MKV up to 64 MB.'));return;}setFile(chosen);setRows([]);setError('');}}/></label>
    {preview&&<video src={preview} controls preload="metadata" style={{width:'100%',maxHeight:220}}/>}
    <label>{t('Spoken language')}<select value={language} disabled={!!busy} onChange={e=>setLanguage(e.target.value)}><option value="auto">{t('Detect automatically')}</option><option value="en">English</option><option value="es">Español</option></select></label>
    <Button label={t('Transcribe locally')} loading={busy==='transcribe'} disabled={!!busy||!file||!caps?.ffmpeg||!caps.whisper} onClick={()=>void run('transcribe')}/>
    {rows.map((row,index)=><fieldset key={index} disabled={!!busy}><legend>{t('Segment')} {index+1}</legend><div className="fs-panel__row">{(['start','end'] as const).map(key=><label key={key}>{t(key==='start'?'Start (seconds)':'End (seconds)')}<input type="number" min={0} max={180} step={.01} value={row[key]} onChange={e=>setRows(old=>old.map((value,i)=>i===index?{...value,[key]:Number(e.target.value)}:value))}/></label>)}</div><label>{t('Transcript or translation')}<textarea maxLength={1000} value={row.text} onChange={e=>setRows(old=>old.map((value,i)=>i===index?{...value,text:e.target.value}:value))}/></label><Button label={t('Remove segment')} onClick={()=>setRows(old=>old.filter((_,i)=>i!==index))}/></fieldset>)}
    <Button label={t('Add segment')} disabled={!!busy||rows.length>=80} onClick={()=>setRows(old=>[...old,{start:old.at(-1)?.end||0,end:Math.min(180,(old.at(-1)?.end||0)+5),text:''}])}/>
    <div className="fs-panel__row">{(['srt','vtt'] as const).map(format=><Button key={format} label={'Export '+format.toUpperCase()} disabled={!!busy||!rows.length} onClick={()=>{try{download(new Blob([subtitleText(rows,format)],{type:'text/plain;charset=utf-8'}),'subtitles.'+format);}catch(e){setError((e as Error).message);}}}/>)}</div>
    <label>{t('Narration language')}<select value={voice} disabled={!!busy} onChange={e=>setVoice(e.target.value)}><option value="es">Español</option><option value="en">English</option></select></label>
    <Button label={t('Create dubbed MP4 locally')} loading={busy==='dub'} disabled={!!busy||!file||!rows.length||!caps?.ffmpeg||!caps.system_voice} onClick={()=>void run('dub')}/>
    {busy&&<p role="status">{t(busy==='dub'?'Creating local narration and video…':'Transcribing locally…')} {seconds}s · {t('Keep this chat open to receive the result. Maximum processing time: 10 minutes.')}</p>}
    {error&&<p role="alert">{error}</p>}
  </section></Popover>;
}
