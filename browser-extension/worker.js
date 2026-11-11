import {BrowserController} from './controller.js';
import {BrowserRelay} from './relay.js';

const controller = new BrowserController(chrome);
const relay = new BrowserRelay({controller, extensionId: chrome.runtime.id, WebSocket, onState: ({state, message}) => {
  void chrome.action.setBadgeText({text: state === 'ready' ? 'ON' : state === 'connecting' ? '…' : 'OFF'});
  void chrome.action.setTitle({title: 'Faustus Browser: ' + state + (message ? '. ' + message : '') + '. Open the panel to reconnect or continue.'});
}});
chrome.debugger.onDetach.addListener(source => controller.onDetach(source));
chrome.debugger.onEvent.addListener((source, method, params) => controller.onEvent(source, method, params));
chrome.tabs.onUpdated.addListener((tabId, change) => controller.onTabUpdated(tabId, change));
chrome.tabs.onRemoved.addListener(tabId => controller.onTabRemoved(tabId));
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  // Content pages and tool commands cannot finish a human turn. The popup sends
  // Continue only from a trusted, active user gesture.
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('panel.html')) return;
  let work;
  if (message?.action === 'panel') work = controller.journeys.panel();
  else if (message?.action === 'person') work = controller.journeys.person(message);
  else if (message?.action === 'reconnect') { relay.connect(); work = Promise.resolve({connecting: true}); }
  else return;
  work.then(result => reply({ok: true, result}), error => reply({ok: false, error: String(error.message ?? error)}));
  return true;
});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'faustus-browser-reconnect') relay.connect(); });
chrome.runtime.onStartup.addListener(() => relay.connect());
chrome.runtime.onInstalled.addListener(() => relay.connect());
void chrome.alarms.create('faustus-browser-reconnect', {delayInMinutes: 1, periodInMinutes: 1});
relay.connect();
