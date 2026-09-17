// WhatsApp: the adapter talks to /api/whatsapp* the way
// routes/whatsapp_routes.py + src/whatsapp_bridge.py define it, the screen
// renders the setup panel per status, the QR image, the two-pane chat
// layout with inline (never native) confirms for Stop/Unlink, and the
// route is wired into the shell and the nav. Static source assertions
// only, no bundling — the same style process-center.check.mjs uses.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const adapter = readFileSync(new URL('../src/adapters/whatsapp.ts', import.meta.url), 'utf8');
assert.match(adapter, /export type WaStatusKind/, 'WaStatusKind type exists');
assert.match(adapter, /export interface WhatsAppStatus/, 'WhatsAppStatus type exists');
assert.match(adapter, /export interface WaChat/, 'WaChat type exists');
assert.match(adapter, /export interface WaMessage/, 'WaMessage type exists');
assert.match(adapter, /export interface WaContact/, 'WaContact type exists');
assert.match(adapter, /export const waStatus/, 'waStatus() exists');
assert.match(adapter, /export const waStart/, 'waStart() exists');
assert.match(adapter, /export const waStop/, 'waStop() exists');
assert.match(adapter, /export const waLogout/, 'waLogout() exists');
assert.match(adapter, /export async function waChats/, 'waChats() exists');
assert.match(adapter, /export async function waMessages/, 'waMessages() exists');
assert.match(adapter, /export async function waContacts/, 'waContacts() exists');
assert.match(adapter, /export const waSend/, 'waSend() exists');
assert.match(adapter, /export const waMarkRead/, 'waMarkRead() exists');
assert.match(adapter, /'\/api\/whatsapp\/status'/, 'calls GET /api/whatsapp/status');
assert.match(adapter, /'\/api\/whatsapp\/start'/, 'calls POST /api/whatsapp/start');
assert.match(adapter, /'\/api\/whatsapp\/stop'/, 'calls POST /api/whatsapp/stop');
assert.match(adapter, /'\/api\/whatsapp\/logout'/, 'calls POST /api/whatsapp/logout');
assert.match(adapter, /\/api\/whatsapp\/chats\?limit=/, 'calls GET /api/whatsapp/chats?limit=');
assert.match(adapter, /\/api\/whatsapp\/messages\?/, 'calls GET /api/whatsapp/messages?');
assert.match(adapter, /\/api\/whatsapp\/contacts\?q=/, 'calls GET /api/whatsapp/contacts?q=');
assert.match(adapter, /'\/api\/whatsapp\/send'/, 'calls POST /api/whatsapp/send');
assert.match(adapter, /'\/api\/whatsapp\/mark-read'/, 'calls POST /api/whatsapp/mark-read');
assert.match(adapter, /credentials:\s*'same-origin'/, 'requests use credentials: same-origin');
assert.match(adapter, /export const waAvatarUrl/, 'waAvatarUrl() exists');
assert.match(adapter, /export const waMediaUrl/, 'waMediaUrl() exists');
assert.match(adapter, /export const waHistory/, 'waHistory() exists');
assert.match(adapter, /export const waTranscribe/, 'waTranscribe() exists');

const screen = readFileSync(new URL('../src/screens/whatsapp/WhatsApp.tsx', import.meta.url), 'utf8');
assert.match(screen, /export function WhatsAppScreen/, 'WhatsAppScreen exists');
assert.match(screen, /<img className="fs-wa__qr" src=\{status\.qr\}/, 'renders the QR as an <img src={qr}>');
assert.match(screen, /width=\{280\}/, 'QR image is 280px');
assert.match(screen, /fs-modes__confirm/, 'uses the inline confirm pattern');
assert.doesNotMatch(screen, /window\.confirm/, 'never uses window.confirm');
assert.match(screen, /whatsapp-stop-confirm/, 'Stop has an inline confirm step');
assert.match(screen, /whatsapp-unlink-confirm/, 'Unlink has an inline confirm step');
assert.match(screen, /This unlinks the phone; you will need to scan again\./, 'Unlink warns about re-scanning');
assert.match(screen, /window\.setInterval\(reloadStatus,\s*fast \? 2000 : 15000\)/, 'polls status every 2s (starting/qr/disconnected) or 15s otherwise');
assert.match(screen, /window\.setInterval\(reload,\s*10000\)/, 'refreshes the open chat every 10s');
assert.match(screen, /fs-wa__panes/, 'renders the two-pane layout');
assert.match(screen, /Pick a chat/, 'empty state: no chat selected');
assert.match(screen, /No messages in this chat yet/, 'empty state: no messages (full history, not just 48h)');
assert.match(screen, /e\.key === 'Enter' && !e\.shiftKey/, 'Enter sends, Shift+Enter is a newline');
assert.match(screen, /KIND_LABEL/, 'non-text kinds render a [kind] placeholder');
assert.match(screen, /Mark as read/, 'has a Mark as read action');
assert.match(screen, /waMessages\(\{\s*chat:\s*chat\.jid,\s*hours:\s*24 \* 365,\s*limit:\s*300\s*\}\)/, 'requests a full year of history, not just 48h');
assert.match(screen, /Load older messages/, 'has a Load older messages action');
assert.match(screen, /waHistory\(chat\.jid,\s*100\)/, 'Load older calls waHistory');
assert.match(screen, /waAvatarUrl\(/, 'renders avatars via waAvatarUrl');
assert.match(screen, /waMediaUrl\(/, 'renders media via waMediaUrl');
assert.match(screen, /waTranscribe\(/, 'can transcribe a voice note via waTranscribe');
assert.match(screen, /<audio/, 'audio messages render an <audio> element');
assert.match(screen, /whatsapp-dictate/, 'composer has a dictation control');
assert.match(screen, /startDictation/, 'dictation reuses the shared speech adapter');
assert.match(screen, /onError=\{\(\) => setFailed\(true\)\}/, 'avatar falls back to an initial on image error');

const appShell = readFileSync(new URL('../src/shell/AppShell.tsx', import.meta.url), 'utf8');
assert.match(appShell, /const WhatsAppScreen = lazyChunk\(\(\) => import\('\.\.\/screens\/whatsapp\/WhatsApp'\)\.then\(\(m\) => \(\{ default: m\.WhatsAppScreen \}\)\)\)/, 'AppShell lazy-loads the WhatsApp screen');
assert.match(appShell, /<Route path="\/whatsapp" element=\{<WhatsAppScreen \/>\}/, 'AppShell routes /whatsapp');
assert.match(appShell, /pathname\.startsWith\('\/whatsapp'\)/, "AppShell's wide-screen list includes /whatsapp");

const routes = readFileSync(new URL('../src/shell/routes.ts', import.meta.url), 'utf8');
assert.match(routes, /\{ path: '\/whatsapp', label: 'WhatsApp', icon: MessageCircle \}/, 'TOOLS lists /whatsapp right after Connectors');
assert.match(routes, /'\/whatsapp',/, 'SERVER_ROUTES lists /whatsapp');
const connectorsIdx = routes.indexOf("{ path: '/connectors'");
const whatsappIdx = routes.indexOf("{ path: '/whatsapp'");
assert.ok(connectorsIdx >= 0 && whatsappIdx > connectorsIdx, "'/whatsapp' comes right after '/connectors' in TOOLS");

console.log('whatsapp: ALL OK');
