const $ = id => document.getElementById(id);
const ask = async message => { const response = await chrome.runtime.sendMessage(message); if (!response?.ok) throw Error(response?.error ?? 'No se ha recibido respuesta.'); return response.result; };
export const realClick = event => event.isTrusted === true && navigator.userActivation.isActive;
const personClick = (button, fn) => button.addEventListener('click', async event => {
  if (!realClick(event)) return;
  button.disabled = true;
  try { await fn(); $('status').textContent = 'Pestaña cedida. Faustus puede continuar con una lectura nueva.'; await refresh(); }
  catch (error) { $('status').textContent = error.message; }
  finally { button.disabled = false; }
});
let active;
async function refresh() {
  const state = await ask({action: 'panel'});
  const current = await chrome.tabs.query({active: true, currentWindow: true}); active = state.tabs.find(tab => tab.id === current[0]?.id);
  $('active-tab').textContent = active ? active.title + '\n' + active.url : 'Selecciona una pestaña web para cederla.';
  $('adopt').disabled = !active;
  $('turns').replaceChildren();
  if (!state.turns.length) $('turns').textContent = 'No hay ninguna intervención pendiente.';
  for (const turn of state.turns) {
    const card = document.createElement('article'), title = document.createElement('strong'), reason = document.createElement('p');
    title.textContent = turn.title || turn.url; reason.className = 'reason'; reason.textContent = turn.reason || 'Completa el paso en esta pestaña y pulsa Continuar.';
    const show = document.createElement('button'); show.textContent = 'Ver pestaña'; show.type = 'button';
    show.addEventListener('click', event => { if (realClick(event)) void chrome.tabs.update(turn.tab_id, {active: true}); });
    const next = document.createElement('button'); next.textContent = turn.kind === 'adoption' ? 'Ceder pestaña' : 'Continuar'; next.type = 'button'; next.className = 'primary';
    personClick(next, () => ask({action: 'person', tab_id: turn.tab_id, turn_id: turn.id}));
    card.append(title, reason, show, next); $('turns').append(card);
  }
  $('sessions').replaceChildren();
  for (const session of state.sessions) { const row = document.createElement('li'); row.textContent = session.name + ' · ' + session.title; $('sessions').append(row); }
  if (!state.sessions.length) $('sessions').textContent = 'Todavía no hay sesiones guardadas.';
}
personClick($('adopt'), () => ask({action: 'person', tab_id: active.id, name: $('name').value}));
$('reconnect').addEventListener('click', async event => { if (!realClick(event)) return; await ask({action: 'reconnect'}); $('status').textContent = 'Reconectando con el relé…'; });
void refresh().catch(error => { $('status').textContent = error.message; });
