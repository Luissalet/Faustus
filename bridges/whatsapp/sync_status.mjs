// What the phone is still sending after the bridge (re)connects, reduced to what a
// screen can show: a phase, a percentage when WhatsApp gives one, and counts.
// Pure, so it is tested without an account.
const SYNC_QUIET_MS = 15000;   // no new batch for this long → history considered settled
const CATCH_UP_MAX_MS = 120000;  // never claim to be catching up for longer than this
export function syncView(s, now = Date.now()) {
  if (!s.startedAt) return { active: false, phase: "idle", progress: null, received: s.received, history: s.history };
  const historyBusy = s.history.lastAt != null && now - s.history.lastAt < SYNC_QUIET_MS && s.history.progress !== 100;
  const catchingUp = s.pending && now - s.startedAt < CATCH_UP_MAX_MS;
  const phase = catchingUp ? "catching_up" : historyBusy ? "history" : "idle";
  return { active: phase !== "idle", phase, progress: phase === "history" ? s.history.progress : null,
    received: s.received, history: s.history, startedAt: s.startedAt };
}
