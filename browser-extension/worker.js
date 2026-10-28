import {BrowserController} from './controller.js';
import {BrowserRelay} from './relay.js';

const controller = new BrowserController(chrome);
const relay = new BrowserRelay({controller, extensionId: chrome.runtime.id, WebSocket, onState: ({state, message}) => {
  void chrome.action.setBadgeText({text: state === 'ready' ? 'ON' : state === 'connecting' ? '…' : 'OFF'});
  void chrome.action.setTitle({title: 'Faustus Browser: ' + state + (message ? '. ' + message : '') + '. Click to reconnect.'});
}});
chrome.debugger.onDetach.addListener(source => controller.onDetach(source));
chrome.debugger.onEvent.addListener((source, method, params) => controller.onEvent(source, method, params));
chrome.tabs.onUpdated.addListener((tabId, change) => controller.onTabUpdated(tabId, change));
chrome.tabs.onRemoved.addListener(tabId => controller.onTabRemoved(tabId));
chrome.action.onClicked.addListener(() => relay.connect());
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'faustus-browser-reconnect') relay.connect(); });
void chrome.alarms.create('faustus-browser-reconnect', {delayInMinutes: 1, periodInMinutes: 1});
relay.connect();
