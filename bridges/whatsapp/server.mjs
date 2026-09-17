// bridges/whatsapp/server.mjs — Faustus ↔ WhatsApp bridge.
//
// A small loopback HTTP service in front of the WhatsApp Web multi-device
// protocol (Baileys). It pairs a personal account by QR, keeps the session
// under WA_AUTH_DIR, remembers contacts/chats/messages in WA_DATA_DIR, and
// lets Faustus (and only Faustus: bearer WA_TOKEN, loopback only) read
// chats and send texts. Nothing here talks to a model: the bridge is data
// in, data out; Faustus decides what to do with it and asks the person
// before sending.
//
//   GET  /status                 → {status, me, qr (data URL while pairing), counts}
//   GET  /chats?limit=50         → [{jid, name, is_group, last_ts, last_text, unread, presence, muted, pinned, archived}]
//   GET  /messages?chat=&since=&limit=&unread=1 → [{id, chat, chat_name, from, from_name, from_me, ts, text, kind, status?, reactions?, reply_to?, edited?, deleted?, forwarded?, mentions?}]
//   GET  /search?q=&chat=&limit=50 → [WaMessage] newest first, substring (accent/case-insensitive) over text
//   GET  /contacts?q=            → [{jid, name, phone}]
//   GET  /avatar?jid=            → image/jpeg (profile picture, cached a day) | 404
//   GET  /media/<id>.<ext>       → a voice note / photo / document already pulled
//   POST /history {chat, count}  → asks the phone for older messages of that chat (async)
//   POST /send {to, text?, quote?, media?, mentions?} → {ok, to, jid, id, ts} (to = jid | phone | contact name)
//   POST /react {id, emoji}      → {ok}  (empty emoji removes the reaction)
//   POST /delete {id}            → {ok}  (revoke for everyone, own messages only, 403 otherwise)
//   POST /forward {id, to}       → {ok, id}
//   POST /edit {id, text}        → {ok}  (own text messages only)
//   POST /typing {chat, state}   → {ok}  (state = composing | recording | paused)
//   POST /subscribe {chat}       → {ok}  (presenceSubscribe, so presence.update starts flowing for that chat)
//   POST /mark-read {chat?}      → {ok, read: n}  (also sends real read receipts to WhatsApp)
//   POST /resolve {to}           → {jid, name} | 409 {candidates}
//   POST /logout                 → unlinks the account (deletes the session)
//
// Status values: starting | qr | connected | disconnected | logged_out.
import { createServer } from "node:http";
import { existsSync, mkdirSync, readFileSync, appendFileSync, writeFileSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";
import pino from "pino";
import QRCode from "qrcode";
import makeWASocketImport, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  isJidGroup,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState,
  proto,
} from "@whiskeysockets/baileys";

const makeWASocket = makeWASocketImport.default ?? makeWASocketImport;
const __dirname = dirname(fileURLToPath(import.meta.url));

const PORT = Number(process.env.WA_PORT || 8790);
const TOKEN = (process.env.WA_TOKEN || "").trim();
const AUTH_DIR = process.env.WA_AUTH_DIR || join(__dirname, "..", "..", "data", "whatsapp", "auth");
const DATA_DIR = process.env.WA_DATA_DIR || join(__dirname, "..", "..", "data", "whatsapp");
const MAX_MESSAGES = Number(process.env.WA_MAX_MESSAGES || 20000);
const KEEP_DAYS = Number(process.env.WA_KEEP_DAYS || 60);

if (!TOKEN) {
  console.error("WA_TOKEN is required (Faustus writes it to data/whatsapp/token)");
  process.exit(2);
}
mkdirSync(AUTH_DIR, { recursive: true });
mkdirSync(DATA_DIR, { recursive: true });

// The library logs failures under `error` (not pino's `err`), which serialises an Error as `{}`;
// map both keys so the log says what actually broke.
const logger = pino({ level: process.env.WA_LOG_LEVEL || "warn",
  serializers: { err: pino.stdSerializers.err, error: pino.stdSerializers.err } });
const MESSAGES_FILE = join(DATA_DIR, "messages.jsonl");
const CONTACTS_FILE = join(DATA_DIR, "contacts.json");

// ---------------------------------------------------------------- store ---
const state = { status: "starting", qr: null, me: null, lastError: null, connectedAt: null };
const contacts = new Map(); // jid -> {name, notify, phone}
const chats = new Map();    // jid -> {name, is_group, last_ts, last_text, unread}
const lidMap = new Map();   // "<n>@lid" -> "<phone>@s.whatsapp.net" (WhatsApp now addresses people by an opaque LID)
let messages = [];          // newest last
const seenIds = new Set();
const LID_FILE = join(DATA_DIR, "lids.json");

// Raw WAMessage per id (bounded), needed to quote/forward/react/delete/edit — Baileys wants
// the exact original key/content for those, not just what we keep in the flattened row.
const rawById = new Map();
const RAW_MAX = 5000;
function rememberRaw(msg) {
  const id = msg?.key?.id;
  if (!id) return;
  rawById.delete(id); rawById.set(id, msg); // re-insert to keep it fresh at the end
  while (rawById.size > RAW_MAX) rawById.delete(rawById.keys().next().value);
}

const chatPresence = new Map(); // chat jid -> 'available'|'unavailable'|'composing'|'recording'|null, live only
const STATUS_ORDER = ["pending", "sent", "delivered", "read", "played"];
const STATUS_NAME = { 2: "sent", 3: "delivered", 4: "read", 5: "played" }; // proto.WebMessageInfo.Status (0 ERROR, 1 PENDING skipped)

// Patch one stored row in place and append a `_patch` line so it survives a restart (loadStore merges these).
function patchMessage(id, patch) {
  const row = messages.find((m) => m.id === id);
  if (!row) return null;
  Object.assign(row, patch);
  try { appendFileSync(MESSAGES_FILE, JSON.stringify({ id, ...patch, _patch: true }) + "\n"); } catch {}
  return row;
}

function bumpStatus(id, status) {
  const row = messages.find((m) => m.id === id);
  if (!row || !row.from_me) return;
  if (STATUS_ORDER.indexOf(status) <= STATUS_ORDER.indexOf(row.status || "pending")) return;
  patchMessage(id, { status });
}

// Per-sender reaction list on a row: empty emoji removes that sender's reaction.
function applyReaction(id, { emoji, from, from_name, from_me }) {
  const row = messages.find((m) => m.id === id);
  if (!row) return;
  const kept = (row.reactions || []).filter((r) => r.from !== from);
  if (emoji) kept.push({ emoji, from, from_name, from_me });
  row.reactions = kept;
  try { appendFileSync(MESSAGES_FILE, JSON.stringify({ id, reactions: kept, _patch: true }) + "\n"); } catch {}
}

function isLid(jid) { return typeof jid === "string" && jid.endsWith("@lid"); }

// The phone-number jid for a LID when we have learnt it; otherwise the jid itself.
function canon(jid) {
  if (!jid) return jid;
  const n = jidNormalizedUser(jid);
  return lidMap.get(n) || n;
}

function learnLid(lid, pn) {
  if (!lid || !pn || !isLid(lid) || isLid(pn)) return;
  const l = jidNormalizedUser(lid), p = jidNormalizedUser(pn);
  if (lidMap.get(l) === p) return;
  lidMap.set(l, p);
  // fold anything filed under the LID into the phone-number identity
  const c = contacts.get(l);
  if (c) { contacts.set(p, { ...(contacts.get(p) || {}), ...c, phone: p.split("@")[0] }); contacts.delete(l); }
  const ch = chats.get(l);
  if (ch) { const cur = chats.get(p); if (!cur || ch.last_ts >= cur.last_ts) chats.set(p, { ...(cur || {}), ...ch, jid: p }); chats.delete(l); }
  for (const m of messages) { if (m.chat === l) m.chat = p; if (m.from === l) m.from = p; }
  try { writeFileSync(LID_FILE, JSON.stringify(Object.fromEntries(lidMap))); } catch {}
}

function loadStore() {
  try {
    const raw = JSON.parse(readFileSync(LID_FILE, "utf8"));
    for (const [lid, pn] of Object.entries(raw)) lidMap.set(lid, pn);
  } catch {}
  try {
    const raw = JSON.parse(readFileSync(CONTACTS_FILE, "utf8"));
    for (const [jid, c] of Object.entries(raw)) contacts.set(canon(jid), c);
  } catch {}
  try {
    const cutoff = Date.now() / 1000 - KEEP_DAYS * 86400;
    const lines = readFileSync(MESSAGES_FILE, "utf8").split("\n").filter(Boolean);
    const byId = new Map();
    for (const line of lines.slice(-MAX_MESSAGES * 2)) {
      try {
        const m = JSON.parse(line);
        if (m._patch) { const cur = byId.get(m.id); if (cur) { delete m._patch; Object.assign(cur, m); } continue; }
        m.chat = canon(m.chat); m.from = canon(m.from);
        if (m.ts >= cutoff && !seenIds.has(m.id)) { messages.push(m); seenIds.add(m.id); byId.set(m.id, m); }
      } catch {}
    }
    sortMessages();
    for (const m of messages) touchChat(m);
  } catch {}
}

function sortMessages() {
  messages.sort((a, b) => a.ts - b.ts);
  if (messages.length > MAX_MESSAGES) { const drop = messages.splice(0, messages.length - MAX_MESSAGES); for (const d of drop) seenIds.delete(d.id); }
}

// Rewrite messages.jsonl from memory (after history pulls, which insert old rows).
function compactStore() {
  try { writeFileSync(MESSAGES_FILE, messages.map((m) => JSON.stringify(m)).join("\n") + (messages.length ? "\n" : "")); } catch {}
}

function saveContacts() {
  try { writeFileSync(CONTACTS_FILE, JSON.stringify(Object.fromEntries(contacts))); } catch {}
}

function nameOf(jid) {
  if (!jid) return "";
  const c = contacts.get(jid) || contacts.get(jidNormalizedUser(jid));
  if (c?.name) return c.name;
  if (c?.notify) return c.notify;
  const ch = chats.get(jid);
  if (ch?.name) return ch.name;
  if (isLid(jid)) return "";          // an opaque id is not a name
  if (isJidGroup(jid)) return "";     // group subject comes from metadata
  return jid.split("@")[0];
}

function phoneOf(jid) {
  const c = canon(jid);
  return isLid(c) ? "" : c.split("@")[0];
}

// Group subjects: fetched once for all groups on connect, lazily for new ones.
const groupsAsked = new Set();
async function learnGroup(jid) {
  if (!sock || !isJidGroup(jid) || groupsAsked.has(jid)) return;
  groupsAsked.add(jid);
  try {
    const meta = await sock.groupMetadata(jid);
    if (meta?.subject) { const cur = chats.get(jid) || { jid, is_group: true, last_ts: 0, last_text: "", unread: 0 }; cur.name = meta.subject; chats.set(jid, cur); }
    for (const p of meta?.participants || []) if (p.id && p.phoneNumber) learnLid(p.id, p.phoneNumber);
  } catch (e) { groupsAsked.delete(jid); logger.warn({ jid, err: e }, "group metadata failed"); }
}
async function learnAllGroups() {
  try {
    const all = await sock.groupFetchAllParticipating();
    for (const [jid, meta] of Object.entries(all || {})) {
      groupsAsked.add(jid);
      if (meta?.subject) { const cur = chats.get(jid) || { jid, is_group: true, last_ts: 0, last_text: "", unread: 0 }; cur.name = meta.subject; chats.set(jid, cur); }
      for (const p of meta?.participants || []) if (p.id && p.phoneNumber) learnLid(p.id, p.phoneNumber);
    }
  } catch (e) { logger.warn({ err: e }, "group list failed"); }
}

function touchChat(m) {
  const cur = chats.get(m.chat) || { jid: m.chat, name: "", is_group: isJidGroup(m.chat), last_ts: 0, last_text: "", unread: 0 };
  if (m.ts >= cur.last_ts) { cur.last_ts = m.ts; cur.last_text = m.text; }
  if (!m.from_me && m.unread) cur.unread += 1;
  chats.set(m.chat, cur);
}

function innerOf(msg) {
  const c = msg.message || {};
  return c.ephemeralMessage?.message || c.viewOnceMessage?.message || c;
}

// contextInfo lives on whichever content node actually carries it (a plain quoted/mentioned
// text is never `conversation`, it's `extendedTextMessage`, so this covers every real case).
function contextInfoOf(msg) {
  const inner = innerOf(msg);
  for (const k of ["extendedTextMessage", "imageMessage", "videoMessage", "audioMessage", "documentMessage", "stickerMessage", "contactMessage", "locationMessage"]) {
    if (inner[k]?.contextInfo) return inner[k].contextInfo;
  }
  return null;
}

function replyToOf(msg) {
  const ci = contextInfoOf(msg);
  if (!ci?.stanzaId || !ci?.quotedMessage) return null;
  const [text] = textOf({ message: ci.quotedMessage });
  const from = ci.participant ? canon(ci.participant) : null;
  const mine = from && state.me?.jid && (from === state.me.jid || from === canon(state.me.jid));
  return { id: ci.stanzaId, from_name: mine ? "me" : from ? (nameOf(from) || phoneOf(from)) : "", from_me: !!mine, text };
}

function textOf(msg) {
  const inner = innerOf(msg);
  if (inner.conversation) return [inner.conversation, "text"];
  if (inner.extendedTextMessage?.text) return [inner.extendedTextMessage.text, "text"];
  if (inner.imageMessage) return [inner.imageMessage.caption || "", "image"];
  if (inner.videoMessage) return [inner.videoMessage.caption || "", "video"];
  if (inner.audioMessage) return ["", "audio"];
  if (inner.documentMessage) return [inner.documentMessage.fileName || "", "document"];
  if (inner.stickerMessage) return ["", "sticker"];
  if (inner.locationMessage) return [`${inner.locationMessage.degreesLatitude},${inner.locationMessage.degreesLongitude}`, "location"];
  if (inner.contactMessage) return [inner.contactMessage.displayName || "", "contact"];
  if (inner.reactionMessage) return [inner.reactionMessage.text || "", "reaction"];
  if (inner.protocolMessage) return ["", "protocol"];
  return ["", "other"];
}

const MEDIA_DIR = join(DATA_DIR, "media");
const MEDIA_MAX_BYTES = Number(process.env.WA_MEDIA_MAX_BYTES || 25 * 1024 * 1024);
const MEDIA_EXT = { audio: "ogg", image: "jpg", video: "mp4", sticker: "webp", document: "" };
mkdirSync(MEDIA_DIR, { recursive: true });

function mediaNode(msg) {
  const c = msg.message || {};
  const inner = c.ephemeralMessage?.message || c.viewOnceMessage?.message || c;
  return inner.audioMessage || inner.imageMessage || inner.videoMessage || inner.stickerMessage || inner.documentMessage || null;
}

// Voice notes, photos and documents are pulled once and kept under media/<id>.<ext>
// so Faustus can play, show or transcribe them without touching WhatsApp again.
async function fetchMedia(msg, m) {
  const node = mediaNode(msg);
  if (!node || !sock) return;
  const size = Number(node.fileLength?.low ?? node.fileLength ?? 0);
  if (size > MEDIA_MAX_BYTES) return;
  const ext = m.kind === "document" ? (String(node.fileName || "").split(".").pop() || "bin").toLowerCase().replace(/[^a-z0-9]/g, "") : MEDIA_EXT[m.kind];
  if (!ext) return;
  const file = join(MEDIA_DIR, `${m.id}.${ext}`);
  if (!existsSync(file)) {
    try {
      const buf = await downloadMediaMessage(msg, "buffer", {}, { logger, reuploadRequest: sock.updateMediaMessage });
      writeFileSync(file, buf);
    } catch (e) { logger.warn({ id: m.id, err: e }, "media download failed"); return; }
  }
  m.media = `${m.id}.${ext}`; m.mime = String(node.mimetype || "");
  if (node.seconds) m.seconds = Number(node.seconds);
  if (node.ptt) m.voice = true;
  try { appendFileSync(MESSAGES_FILE, JSON.stringify({ id: m.id, media: m.media, mime: m.mime, seconds: m.seconds, voice: m.voice, _patch: true }) + "\n"); } catch {}
}

function ingest(msg, { unread = false } = {}) {
  const key = msg.key || {};
  if (!key.id || !key.remoteJid || key.remoteJid === "status@broadcast") return null;
  rememberRaw(msg); // keep the raw stanza even on a dedup hit — refreshes what quote/forward/react/delete/edit can see
  if (seenIds.has(key.id)) return null;
  const [text, kind] = textOf(msg);
  if (kind === "protocol") return null;
  // learn LID ↔ phone pairs the stanza carries before choosing identities
  if (key.senderPn && !key.fromMe) learnLid(key.participant || key.remoteJid, key.senderPn);
  if (key.participantPn && key.participant) learnLid(key.participant, key.participantPn);
  if (key.senderLid && !isLid(key.remoteJid) && !key.fromMe) learnLid(key.senderLid, key.remoteJid);
  const chat = canon(key.remoteJid);
  const from = key.fromMe ? (state.me?.jid || "me") : canon(key.participant || key.remoteJid);
  const m = {
    id: key.id, chat, chat_name: msg.pushName && !key.fromMe && !isJidGroup(chat) ? msg.pushName : nameOf(chat),
    from, from_name: key.fromMe ? "me" : (msg.pushName || nameOf(from)), from_me: !!key.fromMe,
    ts: Number(msg.messageTimestamp?.low ?? msg.messageTimestamp ?? Math.floor(Date.now() / 1000)),
    text: String(text || "").slice(0, 4000), kind, unread: unread && !key.fromMe,
    key: { remoteJid: key.remoteJid, fromMe: !!key.fromMe, id: key.id, ...(key.participant ? { participant: key.participant } : {}) },
  };
  const ci = contextInfoOf(msg);
  const reply_to = replyToOf(msg);
  if (reply_to) m.reply_to = reply_to;
  if (ci?.isForwarded) m.forwarded = true;
  if (ci?.mentionedJid?.length) m.mentions = ci.mentionedJid.map((j) => canon(j));
  if (key.fromMe) m.status = "pending"; // upgraded by messages.update / message-receipt.update as WhatsApp acks it
  if (msg.pushName && !key.fromMe && !contacts.get(from)?.name) {
    contacts.set(from, { ...(contacts.get(from) || {}), notify: msg.pushName, phone: phoneOf(from) });
  }
  messages.push(m); seenIds.add(m.id); touchChat(m);
  if (messages.length > MAX_MESSAGES) { const drop = messages.splice(0, messages.length - MAX_MESSAGES); for (const d of drop) seenIds.delete(d.id); }
  try { appendFileSync(MESSAGES_FILE, JSON.stringify(m) + "\n"); } catch {}
  if (isJidGroup(chat)) void learnGroup(chat);
  if (mediaNode(msg)) void fetchMedia(msg, m);
  return m;
}

// --------------------------------------------------------------- socket ---
let sock = null;
let stopping = false;

async function connect() {
  const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }));
  sock = makeWASocket({
    version,
    auth: { creds: authState.creds, keys: makeCacheableSignalKeyStore(authState.keys, logger) },
    logger,
    printQRInTerminal: false,
    browser: ["Faustus", "Desktop", "1.0"],
    syncFullHistory: true,          // pull old conversations at pairing time
    markOnlineOnConnect: false,
  });
  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("connection.update", async (u) => {
    if (u.qr) {
      state.status = "qr";
      try { state.qr = await QRCode.toDataURL(u.qr, { margin: 1, width: 320 }); } catch { state.qr = null; }
    }
    if (u.connection === "open") {
      state.status = "connected"; state.qr = null; state.connectedAt = Date.now();
      const me = sock.user || {};
      state.me = { jid: jidNormalizedUser(me.id || ""), name: me.name || "" };
      if (me.lid) learnLid(me.lid, me.id);
      void learnAllGroups();
    }
    if (u.connection === "close") {
      const code = u.lastDisconnect?.error?.output?.statusCode;
      state.lastError = String(u.lastDisconnect?.error?.message || code || "closed");
      if (code === DisconnectReason.loggedOut) {
        state.status = "logged_out"; state.qr = null;
        try { rmSync(AUTH_DIR, { recursive: true, force: true }); mkdirSync(AUTH_DIR, { recursive: true }); } catch {}
        if (!stopping) setTimeout(connect, 1500);   // back to a fresh QR
      } else if (!stopping) {
        state.status = "disconnected";
        setTimeout(connect, 3000);
      }
    }
  });
  sock.ev.on("messaging-history.set", ({ contacts: cs = [], chats: chs = [], messages: ms = [], syncType }) => {
    for (const c of cs) if (c.id) { if (c.lid) learnLid(c.lid, c.id); if (c.phoneNumber) learnLid(c.id, c.phoneNumber); const jid = canon(c.id); contacts.set(jid, { ...(contacts.get(jid) || {}), name: c.name || c.verifiedName || contacts.get(jid)?.name || "", notify: c.notify || contacts.get(jid)?.notify || "", phone: phoneOf(jid) }); }
    for (const ch of chs) if (ch.id) { if (ch.lidJid && ch.pnJid) learnLid(ch.lidJid, ch.pnJid); const jid = canon(ch.id); const cur = chats.get(jid) || { jid, is_group: isJidGroup(jid), last_ts: 0, last_text: "", unread: 0 }; cur.name = ch.name || cur.name || ""; cur.unread = Number(ch.unreadCount || cur.unread || 0); if (ch.archived !== undefined) cur.archived = !!ch.archived; if (ch.pinned !== undefined) cur.pinned = !!ch.pinned; if (ch.muteEndTime !== undefined) cur.muted = Number(ch.muteEndTime || 0) * 1000 > Date.now(); chats.set(jid, cur); }
    let added = 0;
    for (const m of ms) if (ingest(m, { unread: false })) added++;
    if (added) { sortMessages(); for (const m of messages) touchChat(m); compactStore(); }
    state.history = { at: Date.now(), added, syncType: String(syncType ?? "") };
    saveContacts();
  });
  sock.ev.on("groups.upsert", (gs) => { for (const g of gs) if (g.id && g.subject) { const cur = chats.get(g.id) || { jid: g.id, is_group: true, last_ts: 0, last_text: "", unread: 0 }; cur.name = g.subject; chats.set(g.id, cur); groupsAsked.add(g.id); } });
  sock.ev.on("groups.update", (gs) => { for (const g of gs) if (g.id && g.subject) { const cur = chats.get(g.id); if (cur) cur.name = g.subject; } });
  sock.ev.on("chats.phoneNumberShare", ({ lid, jid }) => learnLid(lid, jid));
  sock.ev.on("contacts.upsert", (cs) => { for (const c of cs) if (c.id) { if (c.lid) learnLid(c.lid, c.id); if (c.phoneNumber) learnLid(c.id, c.phoneNumber); const jid = canon(c.id); contacts.set(jid, { ...(contacts.get(jid) || {}), name: c.name || c.verifiedName || contacts.get(jid)?.name || "", notify: c.notify || contacts.get(jid)?.notify || "", phone: phoneOf(jid) }); } saveContacts(); });
  sock.ev.on("contacts.update", (cs) => { for (const c of cs) if (c.id) { if (c.lid) learnLid(c.lid, c.id); if (c.phoneNumber) learnLid(c.id, c.phoneNumber); const jid = canon(c.id); const cur = contacts.get(jid) || { phone: phoneOf(jid) }; if (c.name) cur.name = c.name; if (c.notify) cur.notify = c.notify; contacts.set(jid, cur); } saveContacts(); });
  sock.ev.on("chats.upsert", (chs) => { for (const ch of chs) if (ch.id) { if (ch.lidJid && ch.pnJid) learnLid(ch.lidJid, ch.pnJid); const jid = canon(ch.id); const cur = chats.get(jid) || { jid, is_group: isJidGroup(jid), last_ts: 0, last_text: "", unread: 0 }; if (ch.name) cur.name = ch.name; chats.set(jid, cur); if (isJidGroup(jid)) void learnGroup(jid); } });
  sock.ev.on("messages.upsert", ({ messages: ms, type }) => { for (const m of ms) ingest(m, { unread: type === "notify" }); });

  // Receipts (own messages, 1:1), edits and revokes all arrive as `messages.update`.
  sock.ev.on("messages.update", (updates) => {
    for (const { key, update } of updates || []) {
      if (!key?.id || !update) continue;
      if (update.messageStubType === proto.WebMessageInfo.StubType.REVOKE) { patchMessage(key.id, { deleted: true }); continue; }
      const editedInner = update.message?.editedMessage?.message;
      if (editedInner) { const [text] = textOf({ message: editedInner }); patchMessage(key.id, { text: String(text || "").slice(0, 4000), edited: true }); continue; }
      if (typeof update.status === "number") { const name = STATUS_NAME[update.status]; if (name) bumpStatus(key.id, name); }
    }
  });
  // Group receipts arrive per-participant here instead of `messages.update`.
  sock.ev.on("message-receipt.update", (items) => {
    for (const { key, receipt } of items || []) {
      if (!key?.id || !key.fromMe) continue;
      if (receipt?.readTimestamp) bumpStatus(key.id, "read");
      else if (receipt?.receiptTimestamp) bumpStatus(key.id, "delivered");
    }
  });
  sock.ev.on("messages.reaction", (items) => {
    for (const { key, reaction } of items || []) {
      if (!key?.id) continue;
      const envelope = reaction?.key || {}; // the reaction stanza's own key carries who sent it
      const from_me = !!envelope.fromMe;
      const from = from_me ? (state.me?.jid || "me") : canon(envelope.participant || envelope.remoteJid);
      const from_name = from_me ? "me" : (nameOf(from) || phoneOf(from));
      applyReaction(key.id, { emoji: String(reaction?.text || ""), from, from_name, from_me });
    }
  });
  // Local-only deletes ("delete for me" on another linked device) — still render as revoked.
  sock.ev.on("messages.delete", (item) => {
    const keys = Array.isArray(item) ? item : (item?.keys || []);
    for (const k of keys) if (k?.id) patchMessage(k.id, { deleted: true });
  });
  sock.ev.on("presence.update", ({ id, presences }) => {
    const chat = canon(id);
    const vals = Object.values(presences || {}).map((p) => p?.lastKnownPresence).filter(Boolean);
    chatPresence.set(chat, vals.find((v) => v === "composing" || v === "recording") || vals[0] || null);
  });
  sock.ev.on("chats.update", (updates) => {
    for (const u of updates || []) {
      if (!u.id) continue;
      const jid = canon(u.id);
      const cur = chats.get(jid) || { jid, name: "", is_group: isJidGroup(jid), last_ts: 0, last_text: "", unread: 0 };
      if (u.muteEndTime !== undefined) cur.muted = !!u.muteEndTime && Number(u.muteEndTime) > Date.now();
      if (u.pinned !== undefined) cur.pinned = !!u.pinned;
      if (u.archived !== undefined) cur.archived = !!u.archived;
      if (u.unreadCount !== undefined && u.unreadCount !== null) cur.unread = u.unreadCount < 0 ? (cur.unread || 0) + 1 : u.unreadCount;
      chats.set(jid, cur);
    }
  });
}

// Profile pictures: one fetch per jid per day, kept under avatars/.
const AVATAR_DIR = join(DATA_DIR, "avatars");
mkdirSync(AVATAR_DIR, { recursive: true });
const avatarMiss = new Map(); // jid -> ts of last failed lookup
async function avatarFile(jid) {
  const file = join(AVATAR_DIR, `${jid.replace(/[^A-Za-z0-9]/g, "_")}.jpg`);
  try { const st = statSync(file); if (Date.now() - st.mtimeMs < 86400_000) return file; } catch {}
  if (Date.now() - (avatarMiss.get(jid) || 0) < 3600_000) return existsSync(file) ? file : null;
  if (!sock || state.status !== "connected") return existsSync(file) ? file : null;
  try {
    const u = await sock.profilePictureUrl(jid, "image", 8000);
    if (!u) throw new Error("none");
    const r = await fetch(u);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    writeFileSync(file, Buffer.from(await r.arrayBuffer()));
    return file;
  } catch { avatarMiss.set(jid, Date.now()); return existsSync(file) ? file : null; }
}

// ----------------------------------------------------------------- http ---
function json(res, code, body) {
  res.writeHead(code, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(body));
}

function norm(s) { return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").trim(); }

function resolveTarget(to) {
  const raw = String(to || "").trim();
  if (!raw) return { error: "empty target" };
  if (raw.includes("@")) return { jid: jidNormalizedUser(raw), name: nameOf(raw) };
  const digits = raw.replace(/[^\d]/g, "");
  if (digits.length >= 8 && /^[+\d\s()-]+$/.test(raw)) return { jid: `${digits}@s.whatsapp.net`, name: nameOf(`${digits}@s.whatsapp.net`) };
  const q = norm(raw);
  const pool = [];
  for (const [jid, c] of contacts) pool.push({ jid, name: c.name || c.notify || "", score: 0 });
  for (const [jid, ch] of chats) if (!pool.some((p) => p.jid === jid)) pool.push({ jid, name: ch.name || "", score: 0 });
  const exact = pool.filter((p) => norm(p.name) === q);
  if (exact.length === 1) return exact[0];
  const starts = pool.filter((p) => norm(p.name).startsWith(q));
  if (starts.length === 1) return starts[0];
  const contains = pool.filter((p) => norm(p.name).includes(q));
  if (contains.length === 1) return contains[0];
  const cands = (exact.length ? exact : starts.length ? starts : contains).slice(0, 8);
  if (!cands.length) return { error: `no contact or chat called «${raw}»` };
  return { ambiguous: cands.map((c) => ({ jid: c.jid, name: c.name })) };
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) { chunks.push(c); if (chunks.reduce((n, b) => n + b.length, 0) > 1_000_000) throw new Error("body too large"); }
  const s = Buffer.concat(chunks).toString("utf8");
  return s ? JSON.parse(s) : {};
}

const server = createServer(async (req, res) => {
  const auth = req.headers.authorization || "";
  if (auth !== `Bearer ${TOKEN}`) return json(res, 401, { error: "unauthorized" });
  const url = new URL(req.url, "http://127.0.0.1");
  try {
    if (req.method === "GET" && url.pathname === "/status") {
      return json(res, 200, { ...state, counts: { contacts: contacts.size, chats: chats.size, messages: messages.length } });
    }
    if (req.method === "GET" && url.pathname === "/chats") {
      const limit = Math.min(Number(url.searchParams.get("limit") || 50), 500);
      const rows = [...chats.values()]
        .map((c) => ({ ...c, name: c.name || nameOf(c.jid), presence: chatPresence.get(c.jid) ?? null, muted: !!c.muted, pinned: !!c.pinned, archived: !!c.archived }))
        .sort((a, b) => b.last_ts - a.last_ts).slice(0, limit);
      return json(res, 200, rows);
    }
    if (req.method === "GET" && url.pathname === "/search") {
      const q = norm(url.searchParams.get("q") || "");
      const limit = Math.min(Number(url.searchParams.get("limit") || 50), 500);
      let jid = null;
      const chat = url.searchParams.get("chat");
      if (chat) { const r = resolveTarget(chat); if (r.error) return json(res, 404, r); if (r.ambiguous) return json(res, 409, r); jid = r.jid; }
      const rows = messages.filter((m) => (!jid || m.chat === jid) && (!q || norm(m.text).includes(q)))
        .slice().reverse().slice(0, limit)
        .map((m) => ({ ...m, chat_name: nameOf(m.chat) || m.chat_name || "", from_name: m.from_me ? "me" : (nameOf(m.from) || m.from_name || "") }));
      return json(res, 200, rows);
    }
    if (req.method === "GET" && url.pathname === "/messages") {
      const chat = url.searchParams.get("chat");
      const since = Number(url.searchParams.get("since") || 0);
      const limit = Math.min(Number(url.searchParams.get("limit") || 200), 2000);
      const unread = url.searchParams.get("unread") === "1";
      let jid = null;
      if (chat) { const r = resolveTarget(chat); if (r.error) return json(res, 404, r); if (r.ambiguous) return json(res, 409, r); jid = r.jid; }
      const rows = messages.filter((m) => (!jid || m.chat === jid) && m.ts >= since && (!unread || m.unread)).slice(-limit)
        .map((m) => ({ ...m, chat_name: nameOf(m.chat) || m.chat_name || "", from_name: m.from_me ? "me" : (nameOf(m.from) || m.from_name || "") }));
      return json(res, 200, rows);
    }
    if (req.method === "GET" && url.pathname === "/avatar") {
      const r = resolveTarget(url.searchParams.get("jid") || "");
      if (r.error || r.ambiguous) return json(res, 404, { error: "unknown jid" });
      const file = await avatarFile(r.jid);
      if (!file) return json(res, 404, { error: "no picture" });
      res.writeHead(200, { "content-type": "image/jpeg", "cache-control": "private, max-age=3600" });
      return res.end(readFileSync(file));
    }
    if (req.method === "GET" && url.pathname.startsWith("/media/")) {
      const name = decodeURIComponent(url.pathname.slice("/media/".length));
      if (!/^[A-Za-z0-9_-]+\.[a-z0-9]{1,8}$/.test(name)) return json(res, 400, { error: "bad media name" });
      const file = join(MEDIA_DIR, name);
      if (!existsSync(file)) return json(res, 404, { error: "no such media" });
      const m = messages.find((x) => x.media === name);
      const ext = name.split(".").pop();
      const type = m?.mime?.split(";")[0] || ({ ogg: "audio/ogg", jpg: "image/jpeg", mp4: "video/mp4", webp: "image/webp", pdf: "application/pdf" }[ext] || "application/octet-stream");
      res.writeHead(200, { "content-type": type, "cache-control": "private, max-age=86400" });
      return res.end(readFileSync(file));
    }
    if (req.method === "POST" && url.pathname === "/history") {
      // Ask the phone for older messages of one chat; they land through messaging-history.set.
      if (state.status !== "connected" || !sock) return json(res, 503, { error: `not connected (${state.status})` });
      const { chat, count } = await readBody(req);
      const r = resolveTarget(chat);
      if (r.error) return json(res, 404, r);
      if (r.ambiguous) return json(res, 409, r);
      const oldest = messages.find((m) => m.chat === r.jid && m.key);
      if (!oldest) return json(res, 404, { error: "no message of that chat to anchor the request" });
      const id = await sock.fetchMessageHistory(Math.min(Number(count || 50), 500), oldest.key, oldest.ts * 1000);
      return json(res, 200, { ok: true, requested: id, before_ts: oldest.ts, chat: r.jid });
    }
    if (req.method === "GET" && url.pathname === "/contacts") {
      const q = norm(url.searchParams.get("q") || "");
      const rows = [...contacts.entries()].map(([jid, c]) => ({ jid, name: c.name || c.notify || "", phone: phoneOf(jid) }))
        .filter((c) => c.name && (!q || norm(c.name).includes(q))).sort((a, b) => a.name.localeCompare(b.name)).slice(0, 500);
      return json(res, 200, rows);
    }
    if (req.method === "POST" && url.pathname === "/resolve") {
      const { to } = await readBody(req);
      const r = resolveTarget(to);
      return json(res, r.error ? 404 : r.ambiguous ? 409 : 200, r);
    }
    if (req.method === "POST" && url.pathname === "/send") {
      if (state.status !== "connected" || !sock) return json(res, 503, { error: `not connected (${state.status})` });
      const { to, text, quote, media, mentions } = await readBody(req);
      if ((!text || !String(text).trim()) && !media) return json(res, 400, { error: "text or media is required" });
      const r = resolveTarget(to);
      if (r.error) return json(res, 404, r);
      if (r.ambiguous) return json(res, 409, r);
      let quoted;
      if (quote) {
        quoted = rawById.get(quote);
        if (!quoted) return json(res, 409, { error: "cannot quote a message from before the bridge started" });
      }
      const jids = Array.isArray(mentions) && mentions.length ? mentions.map((j) => jidNormalizedUser(j)) : undefined;
      let content;
      if (media?.base64) {
        const buf = Buffer.from(String(media.base64), "base64");
        const mime = String(media.mime || "").toLowerCase();
        const caption = text != null ? String(text) : media.caption;
        if (mime.startsWith("image/")) content = { image: buf, caption, mentions: jids };
        else if (mime.startsWith("audio/") && media.voice) content = { audio: buf, ptt: true, mimetype: mime || "audio/ogg; codecs=opus" };
        else if (mime.startsWith("audio/")) content = { audio: buf, mimetype: mime };
        else if (mime.startsWith("video/")) content = { video: buf, caption, mentions: jids };
        else content = { document: buf, mimetype: mime || "application/octet-stream", fileName: media.filename || "file", caption };
      } else {
        content = { text: String(text), mentions: jids };
      }
      const sent = await sock.sendMessage(r.jid, content, quoted ? { quoted } : undefined);
      const m = ingest(sent, { unread: false });
      return json(res, 200, { ok: true, to: r.name || r.jid, jid: r.jid, id: sent?.key?.id, ts: m?.ts });
    }
    if (req.method === "POST" && url.pathname === "/react") {
      if (state.status !== "connected" || !sock) return json(res, 503, { error: `not connected (${state.status})` });
      const { id, emoji } = await readBody(req);
      const row = messages.find((m) => m.id === id);
      if (!row) return json(res, 404, { error: "unknown message id" });
      await sock.sendMessage(row.chat, { react: { text: String(emoji || ""), key: row.key } });
      const from = state.me?.jid || "me";
      applyReaction(id, { emoji: String(emoji || ""), from, from_name: "me", from_me: true });
      return json(res, 200, { ok: true });
    }
    if (req.method === "POST" && url.pathname === "/delete") {
      if (state.status !== "connected" || !sock) return json(res, 503, { error: `not connected (${state.status})` });
      const { id } = await readBody(req);
      const row = messages.find((m) => m.id === id);
      if (!row) return json(res, 404, { error: "unknown message id" });
      if (!row.from_me) return json(res, 403, { error: "can only delete your own messages" });
      await sock.sendMessage(row.chat, { delete: row.key });
      patchMessage(id, { deleted: true });
      return json(res, 200, { ok: true });
    }
    if (req.method === "POST" && url.pathname === "/forward") {
      if (state.status !== "connected" || !sock) return json(res, 503, { error: `not connected (${state.status})` });
      const { id, to } = await readBody(req);
      const raw = rawById.get(id);
      if (!raw) return json(res, 409, { error: "original message no longer available" });
      const r = resolveTarget(to);
      if (r.error) return json(res, 404, r);
      if (r.ambiguous) return json(res, 409, r);
      const sent = await sock.sendMessage(r.jid, { forward: raw });
      ingest(sent, { unread: false });
      return json(res, 200, { ok: true, id: sent?.key?.id });
    }
    if (req.method === "POST" && url.pathname === "/edit") {
      if (state.status !== "connected" || !sock) return json(res, 503, { error: `not connected (${state.status})` });
      const { id, text } = await readBody(req);
      const row = messages.find((m) => m.id === id);
      if (!row) return json(res, 404, { error: "unknown message id" });
      if (!row.from_me) return json(res, 403, { error: "can only edit your own messages" });
      await sock.sendMessage(row.chat, { text: String(text || ""), edit: row.key });
      patchMessage(id, { text: String(text || "").slice(0, 4000), edited: true });
      return json(res, 200, { ok: true });
    }
    if (req.method === "POST" && url.pathname === "/typing") {
      if (state.status !== "connected" || !sock) return json(res, 503, { error: `not connected (${state.status})` });
      const { chat, state: presenceState } = await readBody(req);
      if (!["composing", "recording", "paused"].includes(presenceState)) return json(res, 400, { error: "state must be composing, recording or paused" });
      const r = resolveTarget(chat);
      if (r.error) return json(res, 404, r);
      if (r.ambiguous) return json(res, 409, r);
      await sock.sendPresenceUpdate(presenceState, r.jid);
      return json(res, 200, { ok: true });
    }
    if (req.method === "POST" && url.pathname === "/subscribe") {
      if (state.status !== "connected" || !sock) return json(res, 503, { error: `not connected (${state.status})` });
      const { chat } = await readBody(req);
      const r = resolveTarget(chat);
      if (r.error) return json(res, 404, r);
      if (r.ambiguous) return json(res, 409, r);
      await sock.presenceSubscribe(r.jid);
      return json(res, 200, { ok: true });
    }
    if (req.method === "POST" && url.pathname === "/mark-read") {
      const { chat } = await readBody(req);
      let r = null;
      if (chat) { r = resolveTarget(chat); if (r.error) return json(res, 404, r); if (r.ambiguous) return json(res, 409, r); }
      const unreadKeys = messages.filter((m) => (!r || m.chat === r.jid) && !m.from_me && m.unread && m.key).map((m) => m.key);
      if (unreadKeys.length && sock && state.status === "connected") {
        try { await sock.readMessages(unreadKeys); } catch (e) { logger.warn({ err: e }, "readMessages failed"); }
      }
      for (const m of messages) if (!r || m.chat === r.jid) m.unread = false;
      for (const [jid, c] of chats) if (!r || jid === r.jid) c.unread = 0;
      return json(res, 200, { ok: true, read: unreadKeys.length });
    }
    if (req.method === "POST" && url.pathname === "/logout") {
      stopping = true;
      try { await sock?.logout(); } catch {}
      try { rmSync(AUTH_DIR, { recursive: true, force: true }); mkdirSync(AUTH_DIR, { recursive: true }); } catch {}
      state.status = "logged_out"; state.qr = null; state.me = null;
      stopping = false;
      setTimeout(connect, 1000);
      return json(res, 200, { ok: true });
    }
    return json(res, 404, { error: "not found" });
  } catch (e) {
    return json(res, 500, { error: String(e?.message || e) });
  }
});

loadStore();
server.listen(PORT, "127.0.0.1", () => {
  console.log(JSON.stringify({ listening: PORT, auth_dir: AUTH_DIR, data_dir: DATA_DIR }));
  connect().catch((e) => { state.status = "disconnected"; state.lastError = String(e?.message || e); setTimeout(connect, 5000); });
});
process.on("SIGTERM", () => { stopping = true; try { sock?.end(); } catch {} server.close(() => process.exit(0)); setTimeout(() => process.exit(0), 1500); });
process.on("SIGINT", () => { stopping = true; try { sock?.end(); } catch {} process.exit(0); });
