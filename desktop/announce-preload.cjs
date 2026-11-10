const {ipcRenderer}=require('electron');
// The hidden window that says the "turn finished" sentence (turn-tray.cjs).
// It plays the audio Faustus synthesised; without one (that Faustus has no
// voice of its own) or when it will not play, it speaks the text with the
// voices installed on this PC. It reports back and exposes nothing to a page.
let playing=null;
const report=result=>ipcRenderer.send('faustus:announce-result',result);
function quiet(){
  try{if(playing){playing.pause();playing.removeAttribute('src');}}catch{/* already gone */}
  playing=null;
  try{window.speechSynthesis.cancel();}catch{/* no speech engine */}
}
function voicesReady(){
  const synth=window.speechSynthesis;
  if(synth.getVoices().length)return Promise.resolve(synth.getVoices());
  return new Promise(resolve=>{
    const done=()=>resolve(synth.getVoices());
    synth.addEventListener('voiceschanged',done,{once:true});
    setTimeout(done,1500);
  });
}
async function speakHere(job,reason){
  try{
    if(!window.speechSynthesis||!job.text){report({error:reason||'no voice on this PC',via:'pc'});return;}
    const wanted=job.lang==='en'?'en':'es';
    const voices=await voicesReady();
    const voice=voices.find(v=>String(v.lang||'').toLowerCase().startsWith(wanted));
    const line=new SpeechSynthesisUtterance(job.text);
    line.lang=voice?voice.lang:(wanted==='en'?'en-US':'es-ES');
    if(voice)line.voice=voice;
    line.onstart=()=>report({started:true,via:'pc',voice:voice?voice.name:''});
    line.onend=()=>report({ended:true,via:'pc'});
    line.onerror=event=>report({error:String(event.error||'speech failed'),via:'pc'});
    window.speechSynthesis.speak(line);
  }catch(error){report({error:String(error&&error.message||error),via:'pc'});}
}
ipcRenderer.on('faustus:announce',(_event,job)=>{
  quiet();
  if(!job||!job.audio){void speakHere(job||{},'');return;}
  const audio=new Audio(`data:${job.mime||'audio/wav'};base64,${job.audio}`);
  playing=audio;
  let fell=false;
  const fall=reason=>{if(fell||playing!==audio)return;fell=true;playing=null;void speakHere(job,reason);};
  audio.addEventListener('ended',()=>{if(playing===audio)playing=null;report({ended:true,via:'faustus',seconds:Math.round(audio.duration*10)/10});});
  audio.addEventListener('error',()=>fall('audio would not play'));
  audio.play().then(()=>report({started:true,via:'faustus',seconds:Math.round((audio.duration||0)*10)/10})).catch(error=>fall(String(error&&error.message||error)));
});
