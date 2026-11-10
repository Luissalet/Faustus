const {test}=require('node:test');
const assert=require('node:assert/strict');
const turns=require('./turn-status.cjs');

const T0=1_800_000_000;
const activity=(running,{approvals=[],ts=T0,workers={},queued={},unverified=0}={})=>({
  ts,unverified,workers,queued,awaiting_approval:approvals,
  running:running.map(r=>r.id),
  runs:Object.fromEntries(running.map(r=>[r.id,r.run||'run-'+r.id])),
  details:Object.fromEntries(running.map(r=>[r.id,{label:r.label||'',started_at:r.started||ts,phase_canonical:'generating'}])),
});
const snap=(running,options={},questions=null)=>turns.snapshotFrom(activity(running,options),questions,options.ts||T0);
const away={focused:false,visibleId:'',settings:{notify:true,voice:false,minSeconds:60},lang:'es'};
const types=effects=>effects.map(e=>e.type+(e.kind?':'+e.kind:''));

test('settings fall back to: notify on, voice off, one minute',()=>{
  assert.deepEqual(turns.settingsFrom(null),{notify:true,voice:false,minSeconds:60});
  assert.deepEqual(turns.settingsFrom({desktop_turn_notify_enabled:false,desktop_turn_voice_enabled:'true',desktop_turn_notify_min_seconds:'15'}),{notify:false,voice:true,minSeconds:15});
  assert.equal(turns.settingsFrom({desktop_turn_notify_min_seconds:-4}).minSeconds,0);
  assert.equal(turns.settingsFrom({desktop_turn_notify_min_seconds:999999}).minSeconds,3600);
  assert.equal(turns.settingsFrom({desktop_turn_notify_min_seconds:'abc'}).minSeconds,60);
});

test('a snapshot leaves delegate workers out and keeps only recent questions with a chat behind',()=>{
  const s=turns.snapshotFrom({ts:T0,running:['a','w'],runs:{a:'r1'},details:{a:{label:'Informe',started_at:T0-30}},workers:{w:{parent:'a'}},awaiting_approval:['a'],queued:{a:2}},
    {questions:[{session:'q1',question:'¿Sigo?',opened_at:new Date((T0-60)*1000).toISOString()},{session:'old',question:'x',opened_at:'2020-01-01T00:00:00Z'},{session:'mcp:srv',question:'y'},{session:'code_mode:a',question:'¿Ejecuto?'}]});
  assert.deepEqual(s.running.map(r=>r.id),['a']);
  assert.equal(s.running[0].label,'Informe');
  assert.equal(s.running[0].queued,2);
  assert.deepEqual(s.questions.map(q=>q.id),['q1','a']);
  assert.equal(s.reliable,true);
  assert.equal(turns.snapshotFrom({ts:T0,unverified:1},null).reliable,false);
});

test('running turns are counted; the icon word is running',()=>{
  const state=turns.createState();
  assert.deepEqual(turns.observe(state,snap([{id:'a',label:'Uno'},{id:'b',label:'Dos'}]),away),[]);
  const s=turns.summary(state);
  assert.equal(s.kind,'running');assert.equal(s.count,2);
  assert.equal(turns.tooltip(state,'es'),'Faustus — 2 turnos en marcha');
  assert.equal(turns.tooltip(state,'en'),'Faustus — 2 turns running');
});

test('a run that leaves asks for its outcome; a long one out of sight is announced with its first sentence',()=>{
  const state=turns.createState();
  turns.observe(state,snap([{id:'a',label:'Informe de ventas',started:T0}]),away);
  const gone=turns.observe(state,snap([], {ts:T0+90}),away);
  assert.deepEqual(types(gone),['outcome']);
  assert.equal(turns.summary(state).running,1,'still counted while its outcome is read');
  const alerts=turns.settle(state,'a',{status:'done',finishedAt:T0+90,text:'## Resultado\n\nHe terminado el informe. Tiene **tres** secciones y un anexo.'},away);
  assert.deepEqual(types(alerts),['notify:done']);
  assert.equal(alerts[0].title,'Terminado: Informe de ventas');
  assert.equal(alerts[0].body,'He terminado el informe.');
  const s=turns.summary(state);
  assert.equal(s.kind,'done');assert.equal(s.unseen,1);
  assert.equal(turns.tooltip(state,'es'),'Faustus — 1 terminado sin ver');
});

test('a short turn, a focused window or a switched-off alert stay silent',()=>{
  const run=(context,seconds)=>{
    const state=turns.createState();
    turns.observe(state,snap([{id:'a',label:'X',started:T0}]),context);
    turns.observe(state,snap([], {ts:T0+seconds}),context);
    return {state,alerts:turns.settle(state,'a',{status:'done',finishedAt:T0+seconds,text:'Listo.'},context)};
  };
  assert.deepEqual(run(away,20).alerts,[],'under the threshold');
  assert.equal(run(away,20).state.turns.get('a').seen,false,'short but still unseen in the menu');
  assert.deepEqual(run({...away,focused:true,visibleId:'other'},300).alerts,[],'the window has the focus');
  assert.equal(run({...away,focused:true,visibleId:'other'},300).state.turns.get('a').seen,false,'another chat was on screen');
  assert.equal(run({...away,focused:true,visibleId:'a'},300).state.turns.get('a').seen,true,'it ended in front of the owner');
  assert.deepEqual(run({...away,settings:{notify:false,voice:false,minSeconds:60}},300).alerts,[]);
  assert.deepEqual(types(run({...away,settings:{notify:false,voice:true,minSeconds:60}},300).alerts),['speak:done'],'voice alone');
  assert.deepEqual(types(run({...away,settings:{notify:true,voice:true,minSeconds:0}},1).alerts),['notify:done','speak:done']);
});

test('the spoken line names the chat and says the first sentence',()=>{
  const state=turns.createState(),context={...away,settings:{notify:false,voice:true,minSeconds:0}};
  turns.observe(state,snap([{id:'a',label:'Plan de viaje',started:T0}]),context);
  turns.observe(state,snap([], {ts:T0+5}),context);
  const [speak]=turns.settle(state,'a',{status:'done',finishedAt:T0+5,text:'El plan está listo: tres días en Lisboa. ¿Quieres que reserve?'},context);
  assert.equal(speak.text,'Faustus ha terminado Plan de viaje. El plan está listo: tres días en Lisboa.');
});

test('a failed turn turns the icon red and says so; a stopped one is the owner\'s own doing',()=>{
  const state=turns.createState();
  turns.observe(state,snap([{id:'a',label:'Deploy',started:T0},{id:'b',label:'Borrador',started:T0}]),away);
  turns.observe(state,snap([], {ts:T0+120}),away);
  const failed=turns.settle(state,'a',{status:'error',finishedAt:T0+120,error:'The model endpoint did not answer.'},away);
  assert.deepEqual(types(failed),['notify:failed']);
  assert.equal(failed[0].title,'Ha fallado: Deploy');
  assert.equal(failed[0].body,'The model endpoint did not answer.');
  assert.deepEqual(turns.settle(state,'b',{status:'stopped',finishedAt:T0+120},away),[]);
  assert.equal(state.turns.get('b').seen,true);
  const s=turns.summary(state);
  assert.equal(s.kind,'failed');assert.equal(s.failed,1);assert.equal(s.unseen,0);
});

test('waiting for the owner outranks everything, is announced once and clears when answered',()=>{
  const state=turns.createState();
  turns.observe(state,snap([{id:'a',label:'Migración',started:T0}]),away);
  const first=turns.observe(state,snap([{id:'a',label:'Migración',started:T0}],{approvals:['a'],ts:T0+10}),away);
  assert.deepEqual(types(first),['notify:waiting']);
  assert.equal(first[0].title,'Te espera: Migración');
  assert.equal(first[0].body,'Hay una aprobación pendiente.');
  assert.deepEqual(turns.observe(state,snap([{id:'a',label:'Migración',started:T0}],{approvals:['a'],ts:T0+14}),away),[],'not again while it waits');
  assert.equal(turns.summary(state).kind,'waiting');
  assert.equal(turns.tooltip(state,'es'),'Faustus — 1 te espera');
  turns.observe(state,snap([{id:'a',label:'Migración',started:T0}],{ts:T0+20}),away);
  assert.equal(turns.summary(state).kind,'running');
});

test('a turn that ends by asking waits with its question instead of counting as finished',()=>{
  const state=turns.createState();
  turns.observe(state,snap([{id:'a',label:'Vídeo',started:T0}],{},{questions:[]}),away);
  assert.equal(turns.needsQuestions(state,activity([])),true,'the questions are read before deciding it ended');
  const effects=turns.observe(state,snap([], {ts:T0+40},{questions:[{session:'a',question:'¿En qué proyecto lo guardo?',opened_at:new Date((T0+39)*1000).toISOString()}]}),away);
  assert.deepEqual(types(effects),['notify:waiting']);
  assert.equal(effects[0].body,'¿En qué proyecto lo guardo?');
  assert.equal(turns.summary(state).kind,'waiting');
  // Answered: the question goes and nothing runs, so the entry goes too.
  turns.observe(state,snap([], {ts:T0+60},{questions:[]}),away);
  assert.equal(turns.summary(state).kind,'idle');
});

test('what was already waiting when the app starts is shown but not announced',()=>{
  const state=turns.createState();
  const effects=turns.observe(state,snap([{id:'a',label:'Uno',started:T0}],{approvals:['a','b']},{questions:[]}),away);
  assert.deepEqual(types(effects),['label'],'only the name of the chat it knows nothing about');
  assert.equal(turns.summary(state).waiting,2);
  assert.equal(turns.setLabel(state,'b','Otro chat'),true);
  assert.match(turns.menu(state,'es',T0)[0].items.map(i=>i.text).join('|'),/Otro chat — aprobación pendiente/);
});

test('an unreliable snapshot never ends a turn',()=>{
  const state=turns.createState();
  turns.observe(state,snap([{id:'a',label:'Uno',started:T0}]),away);
  assert.deepEqual(turns.observe(state,snap([], {ts:T0+5,unverified:1}),away),[]);
  assert.equal(turns.summary(state).running,1);
});

test('a new run of the same chat replaces the finished one; a late outcome is ignored',()=>{
  const state=turns.createState();
  turns.observe(state,snap([{id:'a',label:'Uno',started:T0,run:'r1'}]),away);
  turns.observe(state,snap([], {ts:T0+70}),away);
  turns.observe(state,snap([{id:'a',label:'Uno',started:T0+71,run:'r2'}],{ts:T0+72}),away);
  assert.deepEqual(turns.settle(state,'a',{status:'done',finishedAt:T0+70,text:'Viejo.'},away),[]);
  assert.equal(state.turns.get('a').state,'running');
  assert.equal(state.turns.get('a').runId,'r2');
});

test('opening the chat clears its mark; the corner dot remembers what the main mark hides',()=>{
  const state=turns.createState();
  turns.observe(state,snap([{id:'a',label:'Uno',started:T0},{id:'b',label:'Dos',started:T0}]),away);
  turns.observe(state,snap([{id:'b',label:'Dos',started:T0}],{ts:T0+10}),away);
  turns.settle(state,'a',{status:'done',finishedAt:T0+10,text:'Ok.'},away);
  let s=turns.summary(state);
  assert.equal(s.kind,'running');assert.equal(s.corner,'done');
  assert.equal(turns.markSeen(state,'a'),true);
  assert.equal(turns.markSeen(state,'a'),false);
  s=turns.summary(state);
  assert.equal(s.corner,'');
});

test('the menu lists waiting, running and finished chats by name',()=>{
  const state=turns.createState();
  turns.observe(state,snap([{id:'a',label:'Largo',started:T0-600},{id:'b',label:'Cola',started:T0},{id:'c',label:'Pide permiso',started:T0-30},{id:'d',label:'Acabado',started:T0-200}],{queued:{b:2}}),away);
  turns.observe(state,snap([{id:'a',label:'Largo',started:T0-600},{id:'b',label:'Cola',started:T0},{id:'c',label:'Pide permiso',started:T0-30}],{queued:{b:2},approvals:['c'],ts:T0+1}),away);
  turns.settle(state,'d',{status:'done',finishedAt:T0,text:'Hecho.'},away);
  const sections=turns.menu(state,'es',T0+300);
  assert.deepEqual(sections.map(s=>s.title),['Te esperan','En marcha','Terminados']);
  assert.deepEqual(sections[0].items.map(i=>i.text),['Pide permiso — aprobación pendiente']);
  assert.deepEqual(sections[1].items.map(i=>i.text),['Largo — 15 min','Cola — en cola (2)']);
  assert.deepEqual(sections[2].items.map(i=>i.text),['● Acabado — terminado hace 5 min']);
  assert.equal(sections[2].items[0].id,'d');
});

test('finished turns survive a restart with their unseen mark; only the last ones are kept',()=>{
  const state=turns.createState();
  const many=Array.from({length:turns.RECENT_MAX+3},(_,i)=>({id:'s'+i,label:'Chat '+i,started:T0}));
  turns.observe(state,snap(many),away);
  turns.observe(state,snap([], {ts:T0+100}),away);
  many.forEach((m,i)=>turns.settle(state,m.id,{status:'done',finishedAt:T0+100+i,text:'Ok.'},away));
  assert.equal([...state.turns.values()].filter(t=>t.state==='done').length,turns.RECENT_MAX);
  const again=turns.createState(JSON.parse(JSON.stringify(turns.serialize(state))));
  assert.equal(turns.summary(again).unseen,turns.RECENT_MAX);
  assert.equal(again.turns.has('s0'),false,'the oldest fell off');
  // Half a day later they are history.
  turns.observe(again,snap([], {ts:T0+13*3600}),away);
  assert.equal(turns.summary(again).kind,'idle');
});

test('first sentence: plain, short and never a heading, a code block or the reasoning',()=>{
  assert.equal(turns.firstSentence('<think>mmm</think>\n\n### Resumen\n\n- **Listo**: el fichero `a.py` pesa 3.5 MB. Lo demás sigue igual.'),'Listo: el fichero a.py pesa 3.5 MB.');
  assert.equal(turns.firstSentence('```python\nprint(1)\n```\nHecho. He añadido la prueba [aquí](http://x/y).'),'Hecho. He añadido la prueba aquí.');
  assert.equal(turns.firstSentence('Vale.'),'Vale.');
  assert.equal(turns.firstSentence(''),'');
  assert.equal(turns.firstSentence([{type:'text',text:'Una parte.'},{type:'image'}]),'Una parte.');
  const long=turns.firstSentence('palabra '.repeat(80),60);
  assert.ok(long.length<=60&&long.endsWith('…'),long);
});

test('offline and signed-out say so in the tooltip',()=>{
  const state=turns.createState();
  assert.equal(turns.tooltip(state,'es','offline'),'Faustus — sin conexión');
  assert.equal(turns.tooltip(state,'es','signed-out'),'Faustus — inicia sesión en la ventana');
  assert.equal(turns.tooltip(state,'es',''),'Faustus — nada en marcha');
});
