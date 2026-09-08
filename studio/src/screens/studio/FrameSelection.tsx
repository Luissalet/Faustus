import {useState} from 'react';
import type {BrowserFrame} from '../../adapters/chat';
import {Button} from '../../components';
import {t} from '../../i18n';

/** Selection belongs to an immutable frame, never to the page currently open in the agent. */
export interface VisualSelection {frame:BrowserFrame; x:number; y:number; request:string}

export function selectionPrompt(selection:VisualSelection):string {
  return [selection.request.trim(), t('Visual change request:'),
    JSON.stringify({page:selection.frame.url, title:selection.frame.title,
      capturedAt:new Date(selection.frame.at).toISOString(),
      pointPercent:{x:selection.x,y:selection.y}}),
    t('The attached capture marks the target. It is a past observation, not the current page. Inspect the current page and its DOM before editing. Locate the source in the project; do not assume a DOM element identifies a source file.')].join('\n\n');
}

/** Only existing, bounded in-memory captures: no URL fetch or remote image loading. */
export async function selectionImage(selection:VisualSelection):Promise<File> {
  if(!/^data:image\/(png|jpeg|webp);base64,[a-z0-9+/=]+$/i.test(selection.frame.src)||selection.frame.src.length>28_000_000)
    throw new Error(t('This capture cannot be attached. Take a new browser capture.'));
  const img=new Image(); img.src=selection.frame.src;
  await img.decode();
  if(!img.naturalWidth||!img.naturalHeight||img.naturalWidth*img.naturalHeight>16_777_216)
    throw new Error(t('This capture is too large. Take a smaller browser capture.'));
  const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;
  const context=canvas.getContext('2d');if(!context)throw new Error(t('Could not prepare the capture.'));
  context.drawImage(img,0,0);
  const x=canvas.width*selection.x/100,y=canvas.height*selection.y/100;
  const radius=Math.max(10,Math.min(canvas.width,canvas.height)*.02);
  for(const [color,width] of [['#fff',6],['#d00038',3]] as const){ // guard-ok: exported raster annotation, independent of the viewer's theme
    context.strokeStyle=color;context.lineWidth=width;context.beginPath();context.arc(x,y,radius,0,Math.PI*2);context.stroke();
    context.beginPath();context.moveTo(x-radius*1.5,y);context.lineTo(x+radius*1.5,y);context.moveTo(x,y-radius*1.5);context.lineTo(x,y+radius*1.5);context.stroke();
  }
  const blob=await new Promise<Blob>((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error(t('Could not prepare the capture.'))),'image/png'));
  return new File([blob],'visual-selection.png',{type:'image/png'});
}

export default function FrameSelection({frame,onAdd}:{frame:BrowserFrame;onAdd:(selection:VisualSelection)=>Promise<void>}) {
  const [frozen,setFrozen]=useState<BrowserFrame|null>(null),[point,setPoint]=useState({x:50,y:50});
  const selecting=Boolean(frozen),shown=frozen||frame;
  const [request,setRequest]=useState(''),[saving,setSaving]=useState(false),[error,setError]=useState('');
  return <div className="fs-frame-selection">
    <div className="fs-frame-selection__image">
      <img className="fs-panel__frame" src={shown.src} alt={shown.title||t('Browser screen')}/>
      {selecting&&<button type="button" className="fs-frame-selection__target" aria-label={t('Select the area to change')} disabled={saving}
        onClick={e=>{if(e.detail===0)return;const r=e.currentTarget.getBoundingClientRect();setPoint({x:Math.round(Math.max(0,Math.min(100,(e.clientX-r.left)/r.width*100))),y:Math.round(Math.max(0,Math.min(100,(e.clientY-r.top)/r.height*100)))});}}
        onKeyDown={e=>{const directions:Record<string,[number,number]>={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};const delta=directions[e.key];if(delta){e.preventDefault();setPoint(p=>({x:Math.max(0,Math.min(100,p.x+delta[0])),y:Math.max(0,Math.min(100,p.y+delta[1]))}));}}}>
        <span className="fs-frame-selection__cross" style={{left:point.x+'%',top:point.y+'%'}} aria-hidden="true"/>
      </button>}
    </div>
    {!selecting?<Button label={t('Point to a change')} onClick={()=>setFrozen(frame)}/>:<>
      <p className="fs-studio__hint">{t('Selection capture:')} {shown.title||shown.url}</p>
      <p className="fs-studio__hint">{t('Click the capture or use the arrow keys to mark a target. Nothing is edited or sent automatically.')}</p>
      <div className="fs-panel__row">{(['x','y'] as const).map(axis=><label key={axis}>{axis.toUpperCase()} (%) <input type="number" min={0} max={100} step={1} value={point[axis]} disabled={saving} onChange={e=>setPoint(p=>({...p,[axis]:Math.max(0,Math.min(100,Number(e.target.value)||0))}))}/></label>)}</div>
      <label>{t('What should change?')}<textarea value={request} maxLength={4000} disabled={saving} onChange={e=>setRequest(e.target.value)}/></label>
      {error&&<p role="alert" className="fs-notice" data-tone="danger">{error}</p>}
      <div className="fs-panel__row"><Button label={t('Add to message')} loading={saving} disabled={!request.trim()} onClick={()=>{setSaving(true);setError('');void onAdd({frame:shown,...point,request}).then(()=>{setFrozen(null);setRequest('');}).catch(e=>setError((e as Error).message)).finally(()=>setSaving(false));}}/><Button label={t('Cancel')} disabled={saving} onClick={()=>setFrozen(null)}/></div>
    </>}
  </div>;
}
