import { useSyncExternalStore } from 'react';
import { getSettings, invalidateSettings, loadSettings, saveSettings, subscribeSettings } from '../adapters/settings';

export type GlobalApprovalMode = 'ask' | 'auto' | 'full';
type Snapshot = { mode: GlobalApprovalMode; ready: boolean; saving: boolean; error: string | null };
let snapshot: Snapshot = { mode: 'ask', ready: false, saving: false, error: null };
const listeners = new Set<() => void>();
let refreshing: Promise<void> | null = null;
let stop: (() => void) | null = null;
const STORAGE_SIGNAL = 'faustus-global-settings-changed';

function update(patch: Partial<Snapshot>) {
  const next = { ...snapshot, ...patch };
  if (Object.keys(next).every(key => next[key as keyof Snapshot] === snapshot[key as keyof Snapshot])) return;
  snapshot = next;
  listeners.forEach(listener => listener());
}
function readMode(value: unknown): GlobalApprovalMode {
  return value === 'auto' || value === 'full' ? value : 'ask';
}
export function getGlobalApprovalSnapshot(): Snapshot { return snapshot; }
export async function refreshGlobalApproval(): Promise<void> {
  if (snapshot.saving) return;
  if (refreshing) return refreshing;
  refreshing = loadSettings().then(value => { update({ mode: readMode(value.tool_approval_mode), ready: true, error: null }); })
    .catch(() => { update({ error: 'Could not refresh global approvals.' }); })
    .finally(() => { refreshing = null; });
  return refreshing;
}
export async function setGlobalApprovalMode(mode: GlobalApprovalMode): Promise<void> {
  if (snapshot.saving) return;
  update({ saving: true, error: null });
  try {
    const value = await saveSettings({ tool_approval_mode: mode });
    update({ mode: readMode(value.tool_approval_mode), ready: true });
  } catch (error) {
    update({ error: 'Could not save global approvals.' });
    throw error;
  } finally { update({ saving: false }); }
}
function start() {
  if (stop || typeof window === 'undefined') return;
  const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('faustus-global-settings') : null;
  const refresh = () => { invalidateSettings(); void refreshGlobalApproval(); };
  const unsubscribe = subscribeSettings((value, saved) => {
    update({ mode: readMode(value.tool_approval_mode), ready: true, error: null });
    if (saved) {
      channel?.postMessage('invalidate');
      try { localStorage.setItem(STORAGE_SIGNAL, String(Date.now())); } catch { /* storage is optional */ }
    }
  });
  if (channel) channel.onmessage = refresh;
  const onStorage = (event: StorageEvent) => { if (event.key === STORAGE_SIGNAL) refresh(); };
  const onVisible = () => { if (document.visibilityState !== 'hidden') refresh(); };
  window.addEventListener('focus', refresh);
  window.addEventListener('storage', onStorage);
  document.addEventListener('visibilitychange', onVisible);
  // Native API/MCP writers do not emit browser broadcasts. A visible page
  // refreshes the confirmed global value, without restarting a conversation.
  const timer = window.setInterval(onVisible, 3000);
  void getSettings().then(value => { update({ mode: readMode(value.tool_approval_mode), ready: true }); })
    .catch(() => { update({ error: 'Could not refresh global approvals.' }); });
  stop = () => {
    unsubscribe(); channel?.close(); window.clearInterval(timer);
    window.removeEventListener('focus', refresh); window.removeEventListener('storage', onStorage);
    document.removeEventListener('visibilitychange', onVisible); stop = null;
  };
}
export function subscribeGlobalApprovals(listener: () => void): () => void {
  listeners.add(listener); start();
  return () => { listeners.delete(listener); if (!listeners.size) stop?.(); };
}
export function useGlobalApprovals() {
  return useSyncExternalStore(subscribeGlobalApprovals, getGlobalApprovalSnapshot, getGlobalApprovalSnapshot);
}
