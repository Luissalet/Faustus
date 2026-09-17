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
//   GET  /chats?limit=50         → [{jid, name, is_group, last_ts, last_text, unread}]
//   GET  /messages?chat=&since=&limit=&unread=1 → [{id, chat, chat_name, from, from_name, from_me, ts, text, kind}]
//   GET  /contacts?q=            → [{jid, name, phone}]
//   POST /send {to, text}        → {ok, to, jid, id}   (to = jid | phone | contact name)
//   POST /resolve {to}           → {jid, name} | 409 {candidates}
//   POST /logout                 → unlinks the account (deletes the session)
//
// Status values: starting | qr | connected | disconnected | logged_out.
import { createServer } from "node:http";
import { existsSync, mkdirSync, readFileSync, appendFileSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";
import pino from "pino";
import QRCode from "qrcode";
import makeWASocketImport, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  isJidGroup,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState,
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

const logger = pino({ level: process.env.WA_LOG_LEVEL || "warn" });
const MESSAGES_FILE = join(DATA_DIR, "messages.jsonl");
const CONTACTS_FILE = join(DATA_DIR, "contacts.json");

// ---------------------------------------------------------------- store ---
const state = { status: "starting", qr: null, me: null, lastError: null, connectedAt: null };
const contacts = new Map(); // jid -> {name, notify, phone}
const chats = new Map();    // jid -> {name, is_group, last_ts, last_text, unread}
let messages = [];          // newest last
const seenIds = new Set();

function loadStore() {
  try {
    const raw = JSON.parse(readFileSync(CONTACTS_FILE, "utf8"));
    for (const [jid, c] of Object.entries(raw)) contacts.set(jid, c);
  } catch {}
  try {
    const cutoff = Date.now() / 1000 - KEEP_DAYS * 86400;
    const lines = readFileSync(MESSAGES_FILE, "utf8").split("\n").filter(Boolean);
    for (const line of lines.slice(-MAX_MESSAGES)) {
      try {
        const m = JSON.parse(line);
        if (m.ts >= cutoff && !seenIds.has(m.id)) { messages.push(m); seenIds.add(m.id); touchChat(m); }
      } catch {}
    }
  } catch {}
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
  return jid.split("@")[0];
}

function touchChat(m) {
  const cur = chats.get(m.chat) || { jid: m.chat, name: "", is_group: isJidGroup(m.chat), last_ts: 0, last_text: "", unread: 0 };
  if (m.ts >= cur.last_ts) { cur.last_ts = m.ts; cur.last_text = m.text; }
  if (!m.from_me && m.unread) cur.unread += 1;
  chats.set(m.chat, cur);
}

function textOf(msg) {
  const c = msg.message || {};
  const inner = c.ephemeralMessage?.message || c.viewOnceMessage?.message || c;
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

function ingest(msg, { unread = false } = {}) {
  const key = msg.key || {};
  if (!key.id || !key.remoteJid || key.remoteJid === "status@broadcast") return null;
  if (seenIds.has(key.id)) return null;
  const [text, kind] = textOf(msg);
  if (kind === "protocol") return null;
  const chat = jidNormalizedUser(key.remoteJid);
  const from = key.fromMe ? (state.me?.jid || "me") : jidNormalizedUser(key.participant || key.remoteJid);
  const m = {
    id: key.id, chat, chat_name: msg.pushName && !key.fromMe && !isJidGroup(chat) ? msg.pushName : nameOf(chat),
    from, from_name: key.fromMe ? "me" : (msg.pushName || nameOf(from)), from_me: !!key.fromMe,
    ts: Number(msg.messageTimestamp?.low ?? msg.messageTimestamp ?? Math.floor(Date.now() / 1000)),
    text: String(text || "").slice(0, 4000), kind, unread: unread && !key.fromMe,
  };
  if (msg.pushName && !key.fromMe && !contacts.get(from)?.name) {
    contacts.set(from, { ...(contacts.get(from) || {}), notify: msg.pushName, phone: from.split("@")[0] });
  }
  messages.push(m); seenIds.add(m.id); touchChat(m);
  if (messages.length > MAX_MESSAGES) { const drop = messages.splice(0, messages.length - MAX_MESSAGES); for (const d of drop) seenIds.delete(d.id); }
  try { appendFileSync(MESSAGES_FILE, JSON.stringify(m) + "\n"); } catch {}
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
    syncFullHistory: false,
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
  sock.ev.on("messaging-history.set", ({ contacts: cs = [], chats: chs = [], messages: ms = [] }) => {
    for (const c of cs) if (c.id) contacts.set(jidNormalizedUser(c.id), { name: c.name || c.verifiedName || "", notify: c.notify || "", phone: c.id.split("@")[0] });
    for (const ch of chs) if (ch.id) { const jid = jidNormalizedUser(ch.id); const cur = chats.get(jid) || { jid, is_group: isJidGroup(jid), last_ts: 0, last_text: "", unread: 0 }; cur.name = ch.name || cur.name || ""; cur.unread = Number(ch.unreadCount || cur.unread || 0); chats.set(jid, cur); }
    for (const m of ms) ingest(m, { unread: false });
    saveContacts();
  });
  sock.ev.on("contacts.upsert", (cs) => { for (const c of cs) if (c.id) contacts.set(jidNormalizedUser(c.id), { ...(contacts.get(jidNormalizedUser(c.id)) || {}), name: c.name || c.verifiedName || contacts.get(jidNormalizedUser(c.id))?.name || "", notify: c.notify || "", phone: c.id.split("@")[0] }); saveContacts(); });
  sock.ev.on("contacts.update", (cs) => { for (const c of cs) if (c.id) { const jid = jidNormalizedUser(c.id); const cur = contacts.get(jid) || { phone: jid.split("@")[0] }; if (c.name) cur.name = c.name; if (c.notify) cur.notify = c.notify; contacts.set(jid, cur); } saveContacts(); });
  sock.ev.on("chats.upsert", (chs) => { for (const ch of chs) if (ch.id) { const jid = jidNormalizedUser(ch.id); const cur = chats.get(jid) || { jid, is_group: isJidGroup(jid), last_ts: 0, last_text: "", unread: 0 }; if (ch.name) cur.name = ch.name; chats.set(jid, cur); } });
  sock.ev.on("messages.upsert", ({ messages: ms, type }) => { for (const m of ms) ingest(m, { unread: type === "notify" }); });
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
      const rows = [...chats.values()].map((c) => ({ ...c, name: c.name || nameOf(c.jid) })).sort((a, b) => b.last_ts - a.last_ts).slice(0, limit);
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
        .map((m) => ({ ...m, chat_name: m.chat_name || nameOf(m.chat), from_name: m.from_me ? "me" : (m.from_name || nameOf(m.from)) }));
      return json(res, 200, rows);
    }
    if (req.method === "GET" && url.pathname === "/contacts") {
      const q = norm(url.searchParams.get("q") || "");
      const rows = [...contacts.entries()].map(([jid, c]) => ({ jid, name: c.name || c.notify || "", phone: c.phone || jid.split("@")[0] }))
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
      const { to, text } = await readBody(req);
      if (!text || !String(text).trim()) return json(res, 400, { error: "text is required" });
      const r = resolveTarget(to);
      if (r.error) return json(res, 404, r);
      if (r.ambiguous) return json(res, 409, r);
      const sent = await sock.sendMessage(r.jid, { text: String(text) });
      const m = ingest(sent, { unread: false });
      return json(res, 200, { ok: true, to: r.name || r.jid, jid: r.jid, id: sent?.key?.id, ts: m?.ts });
    }
    if (req.method === "POST" && url.pathname === "/mark-read") {
      const { chat } = await readBody(req);
      const r = chat ? resolveTarget(chat) : null;
      for (const m of messages) if (!r || m.chat === r.jid) m.unread = false;
      for (const [jid, c] of chats) if (!r || jid === r.jid) c.unread = 0;
      return json(res, 200, { ok: true });
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
