// A page traverses the bridge's retained store, not the phone's whole history.
export function messagePage(messages, { chat, cursor = "", limit = 200, retentionLimit = null }) {
  if (typeof chat !== "string" || !chat.includes("@")) throw new Error("chat must be an exact WhatsApp JID");
  const size = Number(limit);
  if (!Number.isInteger(size) || size < 1 || size > 2000) throw new Error("limit must be between 1 and 2000");
  let after = null;
  if (cursor) {
    try { after = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8")); } catch { throw new Error("invalid cursor"); }
    if (!Array.isArray(after) || after.length !== 3 || after[0] !== chat || !Number.isFinite(after[1]) || typeof after[2] !== "string") throw new Error("invalid cursor for this chat");
  }
  const compare = (a, b) => Number(a.ts) - Number(b.ts) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const stored = messages.filter(m => m.chat === chat && typeof m.id === "string" && Number.isFinite(Number(m.ts))).slice().sort(compare);
  const remaining = after ? stored.filter(m => compare(m, { ts: after[1], id: after[2] }) > 0) : stored;
  const rows = remaining.slice(0, size);
  const last = rows.at(-1);
  return {
    messages: rows,
    next_cursor: last ? Buffer.from(JSON.stringify([chat, Number(last.ts), last.id])).toString("base64url") : cursor || null,
    has_more: remaining.length > rows.length,
    coverage: { scope: "retained_bridge_store", phone_history_complete: false, retained_chat_messages: stored.length, earliest_ts: stored[0]?.ts ?? null, retention_limit: retentionLimit, repeat_from_start_for_updates: true },
  };
}
