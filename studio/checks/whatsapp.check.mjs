// WhatsApp: the adapter talks to /api/whatsapp* the way
// routes/whatsapp_routes.py + src/whatsapp_bridge.py define it, the screen
// renders the setup panel per status, the QR image, the two-pane
// WhatsApp-Web-like chat layout — replies, reactions, edits, delivery
// ticks, live typing/presence, search, attachments/voice notes and "Ask
// Faustus" — with inline (never native) confirms for every destructive
// action. Static source assertions only, no bundling — the same style
// process-center.check.mjs uses.
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

// Wave 3: WhatsApp-Web-like actions.
assert.match(adapter, /status\?:\s*'pending' \| 'sent' \| 'delivered' \| 'read' \| 'played'/, 'WaMessage.status is typed');
assert.match(adapter, /reactions\?:/, 'WaMessage.reactions field exists');
assert.match(adapter, /reply_to\?:/, 'WaMessage.reply_to field exists');
assert.match(adapter, /edited\?:\s*boolean/, 'WaMessage.edited field exists');
assert.match(adapter, /deleted\?:\s*boolean/, 'WaMessage.deleted field exists');
assert.match(adapter, /forwarded\?:\s*boolean/, 'WaMessage.forwarded field exists');
assert.match(adapter, /mentions\?:\s*string\[\]/, 'WaMessage.mentions field exists');
assert.match(adapter, /export const waSend = \(to: string, text: string, opts\?: WaSendOpts\)/, 'waSend() keeps the 2-arg call working and gains opts');
assert.match(adapter, /export const waReact/, 'waReact() exists');
assert.match(adapter, /'\/api\/whatsapp\/react'/, 'calls POST /api/whatsapp/react');
assert.match(adapter, /export const waDelete/, 'waDelete() exists');
assert.match(adapter, /'\/api\/whatsapp\/delete'/, 'calls POST /api/whatsapp/delete');
assert.match(adapter, /export const waForward/, 'waForward() exists');
assert.match(adapter, /'\/api\/whatsapp\/forward'/, 'calls POST /api/whatsapp/forward');
assert.match(adapter, /export const waEdit/, 'waEdit() exists');
assert.match(adapter, /'\/api\/whatsapp\/edit'/, 'calls POST /api/whatsapp/edit');
assert.match(adapter, /export const waTyping/, 'waTyping() exists');
assert.match(adapter, /'\/api\/whatsapp\/typing'/, 'calls POST /api/whatsapp/typing');
assert.match(adapter, /export const waSubscribe/, 'waSubscribe() exists');
assert.match(adapter, /'\/api\/whatsapp\/subscribe'/, 'calls POST /api/whatsapp/subscribe');
assert.match(adapter, /export async function waSearch/, 'waSearch() exists');
assert.match(adapter, /\/api\/whatsapp\/search\?/, 'calls GET /api/whatsapp/search?');
assert.match(adapter, /export async function waUpload/, 'waUpload() exists');
assert.match(adapter, /new FormData\(\)/, 'waUpload() builds a FormData body');
assert.match(adapter, /'\/api\/whatsapp\/upload'/, 'calls POST /api/whatsapp/upload');
assert.match(adapter, /export const waAssist/, 'waAssist() exists');
assert.match(adapter, /'\/api\/whatsapp\/assist'/, 'calls POST /api/whatsapp/assist');

const screen = readFileSync(new URL('../src/screens/whatsapp/WhatsApp.tsx', import.meta.url), 'utf8');
const bubble = readFileSync(new URL('../src/screens/whatsapp/WhatsAppBubble.tsx', import.meta.url), 'utf8');
const composer = readFileSync(new URL('../src/screens/whatsapp/WhatsAppComposer.tsx', import.meta.url), 'utf8');
const assist = readFileSync(new URL('../src/screens/whatsapp/WhatsAppAssist.tsx', import.meta.url), 'utf8');
const allUi = screen + bubble + composer + assist;

assert.match(screen, /export function WhatsAppScreen/, 'WhatsAppScreen exists');
assert.match(screen, /<img className="fs-wa__qr" src=\{status\.qr\}/, 'renders the QR as an <img src={qr}>');
assert.match(screen, /width=\{280\}/, 'QR image is 280px');
assert.match(allUi, /fs-modes__confirm/, 'uses the inline confirm pattern');
assert.doesNotMatch(allUi, /window\.confirm/, 'never uses window.confirm');
assert.match(screen, /whatsapp-stop-confirm/, 'Stop has an inline confirm step');
assert.match(screen, /whatsapp-unlink-confirm/, 'Unlink has an inline confirm step');
assert.match(screen, /This unlinks the phone; you will need to scan again\./, 'Unlink warns about re-scanning');
assert.match(screen, /window\.setInterval\(reloadStatus,\s*fast \? 2000 : 15000\)/, 'polls status every 2s (starting/qr/disconnected) or 15s otherwise');
assert.match(screen, /window\.setInterval\(reload,\s*10000\)/, 'refreshes the open chat every 10s');
assert.match(screen, /window\.setInterval\(reloadChats,\s*10000\)/, 'refreshes the chat list every 10s');
assert.match(screen, /fs-wa__panes/, 'renders the two-pane layout');
assert.match(screen, /Pick a chat/, 'empty state: no chat selected');
assert.match(screen, /No messages in this chat yet/, 'empty state: no messages (full history, not just 48h)');
assert.match(composer, /e\.key === 'Enter' && !e\.shiftKey/, 'Enter sends, Shift+Enter is a newline');
assert.match(screen, /Mark as read/, 'has a Mark as read action');
assert.match(screen, /waMessages\(\{\s*chat:\s*chat\.jid,\s*hours:\s*24 \* 365,\s*limit:\s*300\s*\}\)/, 'requests a full year of history, not just 48h');
assert.match(screen, /Load older messages/, 'has a Load older messages action');
assert.match(screen, /waHistory\(chat\.jid,\s*100\)/, 'Load older calls waHistory');
assert.match(allUi, /waAvatarUrl\(/, 'renders avatars via waAvatarUrl');
assert.match(bubble, /waMediaUrl\(/, 'renders media via waMediaUrl');
assert.match(bubble, /waTranscribe\(/, 'can transcribe a voice note via waTranscribe');
assert.match(bubble, /<audio/, 'audio messages render an <audio> element');
assert.match(composer, /whatsapp-dictate/, 'composer has a dictation control');
assert.match(composer, /startDictation/, 'dictation reuses the shared speech adapter');
assert.match(bubble, /onError=\{\(\) => setFailed\(true\)\}/, 'avatar falls back to an initial on image error');

// Wave 3: date separators, receipts, edits/deletes/forwards, reactions, replies.
assert.match(bubble, /dayLabel/, 'groups bubbles with a date-separator helper');
assert.match(screen, /kind === 'sep'/, 'renders a date separator row between days');
assert.match(bubble, /StatusTicks/, 'own bubbles render delivery/read ticks');
assert.match(bubble, /t\('edited'\)/, "edited messages show an 'edited' label");
assert.match(bubble, /t\('This message was deleted'\)/, 'deleted messages render the revoked placeholder');
assert.match(bubble, /t\('Forwarded'\)/, 'forwarded messages show a Forwarded label');
assert.match(bubble, /QuotedBlock/, 'a reply renders a quoted block above the text');
assert.match(bubble, /fs-wa__reactions/, 'renders a reactions row grouped by emoji');
assert.match(bubble, /fs-wa__bubble-actions/, 'bubbles have a hover/focus action toolbar');
assert.match(bubble, /whatsapp-reply"/, "hover toolbar has a Reply action (testid whatsapp-reply)");
assert.match(bubble, /whatsapp-react"/, "hover toolbar has a React action (testid whatsapp-react)");
assert.match(bubble, /whatsapp-forward"/, "hover toolbar has a Forward action (testid whatsapp-forward)");
assert.match(bubble, /navigator\.clipboard\.writeText/, 'Copy uses the clipboard');
assert.match(bubble, /waEdit\(/, 'own text messages can be edited inline via waEdit');
assert.match(bubble, /waDelete\(/, 'own messages can be deleted for everyone via waDelete');
assert.match(bubble, /whatsapp-delete-confirm/, 'Delete for everyone has an inline two-step confirm');
assert.match(bubble, /waForward\(/, 'Forward uses waForward');
assert.match(bubble, /waReact\(/, 'React uses waReact');

// Header: presence line + subscribe.
assert.match(screen, /waSubscribe\(chat\.jid\)/, 'opening a chat subscribes to its presence');
assert.match(screen, /whatsapp-presence/, 'renders a live presence line under the chat name');
assert.match(screen, /composing:\s*'typing…'/, "presence 'composing' shows 'typing…'");
assert.match(screen, /recording:\s*'recording audio…'/, "presence 'recording' shows 'recording audio…'");
assert.match(screen, /available:\s*'online'/, "presence 'available' shows 'online'");

// Composing typing indicator + read receipts.
assert.match(composer, /waTyping\(chat, 'composing'\)/, 'typing sends composing at most once per throttle window');
assert.match(composer, /waTyping\(chat, 'paused'\)/, 'typing sends paused after the pause timeout');
assert.match(composer, /TYPING_THROTTLE_MS = 4000/, 'typing throttle is 4s');
assert.match(composer, /TYPING_PAUSE_MS = 5000/, 'typing pause is 5s');
assert.match(screen, /chat\.unread/, 'read receipts only fire when the chat has unread messages');
assert.match(screen, /document\.hidden/, 'read receipts are skipped while the tab is hidden');
assert.match(screen, /waMarkRead\(chat\.jid\)\.catch/, 'auto mark-read calls waMarkRead once the chat has settled');

// Search: in-chat and global.
assert.match(screen, /whatsapp-search"/, 'chat pane header has a search toggle (testid whatsapp-search)');
assert.match(screen, /waSearch\(q\.trim\(\), chat\.jid\)/, 'in-chat search calls waSearch scoped to the chat');
assert.match(screen, /waSearch\(q\)/, 'the list pane also searches messages globally (no chat filter)');
assert.match(screen, /length < 3/, 'global message search only fires once the filter is 3+ chars');

// Composer: attach, voice note, dictation kept distinct.
assert.match(composer, /whatsapp-attach"/, 'composer has an attach control (testid whatsapp-attach)');
assert.match(composer, /type="file"/, 'attach uses a hidden file input');
assert.match(composer, /waUpload\(/, 'attach and voice notes go through waUpload');
assert.match(composer, /whatsapp-record"/, 'composer has a voice-note record control (testid whatsapp-record)');
assert.match(composer, /MediaRecorder/, 'voice notes are recorded with MediaRecorder');
assert.match(composer, /audio\/webm;codecs=opus/, 'voice notes record audio/webm;codecs=opus');
assert.match(composer, /voice:\s*true/, 'the recorded voice note is uploaded with voice: true');
assert.match(composer, /t\('Record voice note'\)/, "the record button is labelled distinctly from Dictate");
assert.match(composer, /fs-wa__reply-strip/, 'replying shows a "replying to" strip above the composer');
assert.match(composer, /onClearReply/, 'the reply strip can be cleared');

// Images/documents.
assert.match(bubble, /download className="fs-wa__doc-link"/, 'document bubbles download');

// Ask Faustus.
assert.match(screen, /AskFaustus/, 'renders the Ask Faustus panel in the chat pane');
assert.match(assist, /whatsapp-assist"/, 'Ask Faustus trigger has testid whatsapp-assist');
assert.match(assist, /Sparkles/, 'Ask Faustus uses the sparkle icon');
assert.match(assist, /waAssist\(/, 'Ask Faustus calls waAssist');
assert.match(assist, /'Summarise this chat'/, 'quick action: summarise');
assert.match(assist, /'Draft a reply'/, 'quick action: draft a reply');
assert.match(assist, /'Translate to Spanish'/, 'quick action: translate to Spanish');
assert.match(assist, /'Translate to English'/, 'quick action: translate to English');
assert.match(assist, /t\('Use as draft'\)/, "the result offers 'Use as draft'");
assert.match(assist, /onUseAsDraft/, "'Use as draft' fills the composer, never sends");
assert.match(assist, /t\('Copy'\)/, "the result offers 'Copy'");
assert.match(assist, /t\('Regenerate'\)/, "draft_reply results offer 'Regenerate'");
assert.match(assist, /t\('Thinking…'\)/, "shows a 'Thinking…' loading state");
assert.match(assist, /\/studio\?draft=\$\{encodeURIComponent/, "'Open in Faustus' links to /studio?draft=");
assert.doesNotMatch(assist, /waSend\(|waUpload\(/, 'Ask Faustus never sends a message itself');

// Layout stays full-height; bubbles never stretch vertically; max 65% width.
const css = readFileSync(new URL('../src/screens/whatsapp/whatsapp.css', import.meta.url), 'utf8');
assert.match(css, /block-size: calc\(100dvh - 2 \* var\(--fs-space-7\)\)/, 'the screen still owns full viewport height');
assert.match(css, /\.fs-wa__bubble-row \{[^}]*max-width: none/, 'a chat row still spans the pane, not the shell 65ch cap');
assert.match(css, /\.fs-wa__bubbles \{[^}]*align-content: start/, 'bubbles hug their content and never stretch vertically');
assert.match(css, /max-inline-size:\s*65%/, 'a bubble is at most 65% of the pane width');

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

// Wave 3b: photos open in an in-app overlay with zoom (never a new tab); an
// "Archived" row at the top of the list, as WhatsApp has.
const lightbox = readFileSync(new URL('../src/screens/whatsapp/WhatsAppLightbox.tsx', import.meta.url), 'utf8');
assert.match(lightbox, /export function Lightbox/, 'Lightbox exists');
assert.match(lightbox, /RadixDialog\.Root/, 'lightbox uses the Radix dialog (focus, Escape, inert background)');
assert.match(lightbox, /onWheel/, 'lightbox zooms with the wheel');
assert.match(lightbox, /scale\(\$\{scale\}\)/, 'lightbox applies the zoom as a transform');
const bubbleSrc = readFileSync(new URL('../src/screens/whatsapp/WhatsAppBubble.tsx', import.meta.url), 'utf8');
assert.doesNotMatch(bubbleSrc, /target="_blank"/, 'photos never open in a new tab');
assert.match(bubbleSrc, /whatsapp-image-open/, 'photo bubbles open the overlay');
const screenSrc = readFileSync(new URL('../src/screens/whatsapp/WhatsApp.tsx', import.meta.url), 'utf8');
assert.match(screenSrc, /<Lightbox src=/, 'chat pane renders the lightbox');
assert.match(screenSrc, /whatsapp-archived/, 'list has the Archived row');
assert.match(screenSrc, /showArchived \? c\.archived : !c\.archived/, 'Archived row toggles between live and archived chats');
console.log('whatsapp (3b): ALL OK');
