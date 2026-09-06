import { useEffect } from 'react';
import { create } from 'zustand';
import { chatActivity, EMPTY_ACTIVITY, type ChatActivity } from '../adapters/chat';
import { groupActivity, sessionActivity, type SessionActivity } from '../lib/activity';

export { groupActivity, sessionActivity };
export type { SessionActivity };

/**
 * What is running right now, for every list that wants to say so.
 *
 * A turn does not belong to the screen that started it: the server keeps the
 * run going when the browser walks away (`src/agent_runs.py`), and until this
 * existed the interface had no way to know. You left a conversation mid-turn
 * and every list went quiet — the chat looked frozen on whatever was last
 * written to it, which mid-approval reads as "asking permission, no buttons".
 *
 * `GET /api/chat/activity` answers for the whole account in one call, so this
 * is ONE poller shared by every screen (a store, not a hook per row), it only
 * ticks while the tab is visible, and it slows down when nothing is alive.
 */

interface ActivityState {
  activity: ChatActivity;
  apply: (activity: ChatActivity) => void;
}

const useActivityStore = create<ActivityState>((set) => ({
  activity: EMPTY_ACTIVITY,
  apply: (activity) => set({ activity }),
}));

/** While something is alive it is worth a close look; otherwise, rarely. */
const LIVE_MS = 4_000;
const IDLE_MS = 20_000;

let watchers = 0;
let timer = 0;
let reading = false;

function anythingLive(activity: ChatActivity): boolean {
  return activity.running.length > 0 || activity.awaiting.length > 0;
}

function schedule(delay: number): void {
  window.clearTimeout(timer);
  if (!watchers) return;
  timer = window.setTimeout(() => void tick(), delay);
}

async function tick(force = false): Promise<void> {
  if (!watchers || reading) return;
  // A hidden tab asks nothing on its own: the answer would be stale by the
  // time anyone looked, and the visibility listener reads again on the way
  // back. The FIRST read is not skipped, though — a dot that only appears
  // once you focus the window is a dot you never see (same reasoning as
  // shell/badges.ts), and a page can perfectly well load unfocused.
  if (!force && typeof document !== 'undefined' && document.visibilityState !== 'visible') {
    schedule(IDLE_MS);
    return;
  }
  reading = true;
  try {
    const next = await chatActivity();
    if (watchers) useActivityStore.getState().apply(next);
    schedule(anythingLive(next) ? LIVE_MS : IDLE_MS);
  } catch {
    // A failed poll is not news: keep the last picture and try again later.
    schedule(IDLE_MS);
  } finally {
    reading = false;
  }
}

/** Read now, without waiting for the next tick (a turn just started/stopped). */
export function refreshActivity(): void {
  if (!watchers) return;
  window.clearTimeout(timer);
  void tick(true);
}

/**
 * Subscribes this component to the shared picture. The first watcher starts
 * the poller and the last one to leave stops it.
 */
export function useChatActivity(): ChatActivity {
  const activity = useActivityStore((s) => s.activity);
  useEffect(() => {
    watchers += 1;
    if (watchers === 1) void tick(true);
    const onVisible = () => {
      if (document.visibilityState === 'visible') refreshActivity();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      watchers -= 1;
      document.removeEventListener('visibilitychange', onVisible);
      if (!watchers) window.clearTimeout(timer);
    };
  }, []);
  return activity;
}


