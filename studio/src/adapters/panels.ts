import { useSyncExternalStore } from 'react';
import {
  BookOpen, Box, Boxes, Brain, Camera, ChartColumn, ChartLine, ChartPie, Clapperboard, Clock, Code, Cpu, FileText, Film,
  FlaskConical, Folder, Gauge, Globe, Heart, Image, LayoutDashboard, ListChecks, Mail, Map, MessageSquare, Music,
  Newspaper, Palette, Rocket, Ruler, ScrollText, Shield, Sparkles, Star, Table, Terminal, Timer, Users, Wallet, Wrench,
  Zap, Calendar, type LucideIcon,
} from 'lucide-react';
import { t } from '../i18n';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';

/**
 * User panels (`/api/panels`): pages Faustus builds from a description and mounts in the
 * sidebar. A panel's icon is a lucide name in kebab-case; only the icons listed here are
 * bundled, anything else falls back to the dashboard icon.
 */
export interface Panel {
  id: string; name: string; icon: string; description: string | null; order: number | null; version: number;
  created: string; updated: string;
}
export interface PanelVersion { version: number; bytes: number; saved: string }

export const PANEL_ICONS: Record<string, LucideIcon> = {
  'layout-dashboard': LayoutDashboard, 'bar-chart-3': ChartColumn, 'line-chart': ChartLine, 'pie-chart': ChartPie, box: Box,
  boxes: Boxes, 'book-open': BookOpen, palette: Palette, film: Film, clapperboard: Clapperboard, image: Image,
  'file-text': FileText, calendar: Calendar, clock: Clock, 'list-checks': ListChecks, table: Table, gauge: Gauge,
  wallet: Wallet, cpu: Cpu, globe: Globe, map: Map, music: Music, camera: Camera, star: Star, heart: Heart,
  'flask-conical': FlaskConical, wrench: Wrench, code: Code, terminal: Terminal, brain: Brain, sparkles: Sparkles,
  rocket: Rocket, shield: Shield, zap: Zap, folder: Folder, mail: Mail, 'message-square': MessageSquare, users: Users,
  'scroll-text': ScrollText, newspaper: Newspaper, ruler: Ruler, timer: Timer,
};

export function panelIcon(name: string | undefined): LucideIcon {
  return PANEL_ICONS[name ?? ''] ?? LayoutDashboard;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION,
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    /* not JSON */
  }
  if (!response.ok) {
    const o = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    throw new ApiError(String(o.error || o.detail || `${path} ${t('responded')} ${response.status}`), response.status);
  }
  return body as T;
}

const BASE = '/api/panels';
const enc = encodeURIComponent;

export async function listPanels(): Promise<Panel[]> {
  const r = await request<{ panels?: Panel[] }>(BASE);
  return Array.isArray(r.panels) ? r.panels : [];
}
export const loadPanel = (id: string) => request<Panel & { html: string }>(`${BASE}/${enc(id)}`);
export const updatePanel = (id: string, patch: Partial<Pick<Panel, 'name' | 'icon' | 'description'>>) =>
  request<Panel>(`${BASE}/${enc(id)}`, { method: 'PUT', body: JSON.stringify(patch) });
export const deletePanel = (id: string) => request<{ deleted: string }>(`${BASE}/${enc(id)}`, { method: 'DELETE' });
export const reorderPanels = (ids: string[]) => request<{ panels: Panel[] }>(`${BASE}/reorder`, { method: 'POST', body: JSON.stringify({ ids }) });
export async function panelVersions(id: string): Promise<{ current: number; versions: PanelVersion[] }> {
  return request(`${BASE}/${enc(id)}/versions`);
}
export const restorePanel = (id: string, version: number) =>
  request<Panel>(`${BASE}/${enc(id)}/restore`, { method: 'POST', body: JSON.stringify({ version }) });
export interface PanelCheck {
  ok: boolean; status: number | null; title: string; visible_text_chars: number; screenshot: string | null; notes: string[];
  console_errors: { text: string }[]; page_errors: string[]; failed_requests: { url: string; status?: number; error?: string }[];
}
export const checkPanel = (id: string) => request<PanelCheck>(`${BASE}/${enc(id)}/check`, { method: 'POST', body: '{}' });
export const pageHref = (id: string, bust?: number | string) => `${BASE}/${enc(id)}/page${bust !== undefined ? `?v=${bust}` : ''}`;
export const checkImageHref = (id: string, bust: number) => `${BASE}/${enc(id)}/check.png?v=${bust}`;

/** The text of the "new panel" chat: an opening for the user to finish. */
export function newPanelDraftHref(): string {
  return `/studio?draft=${enc(t('I want a panel that '))}`;
}

/* -- one shared list for the sidebar, the manager and the viewer ---------- */

let cache: Panel[] | null = null;
let started = false;
const listeners = new Set<() => void>();

function publish(next: Panel[]) {
  const same = cache && cache.length === next.length && cache.every((p, i) => JSON.stringify(p) === JSON.stringify(next[i]));
  if (same) return;
  cache = next;
  listeners.forEach((fn) => fn());
}

export async function refreshPanels(): Promise<void> {
  try {
    publish(await listPanels());
  } catch {
    if (cache === null) publish([]);
  }
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  if (!started) {
    started = true;
    void refreshPanels();
    // Panels are created by the agent in a chat: pick them up without a reload.
    window.setInterval(() => { if (!document.hidden) void refreshPanels(); }, 15000);
    window.addEventListener('focus', () => { void refreshPanels(); });
  }
  return () => { listeners.delete(fn); };
}

/** The user's panels in sidebar order; `null` until the first answer. */
export function usePanels(): Panel[] | null {
  return useSyncExternalStore(subscribe, () => cache, () => null);
}

export function usePanel(id: string | undefined): { panel: Panel | null | undefined } {
  const panels = usePanels();
  if (!panels) return { panel: undefined };
  return { panel: panels.find((p) => p.id === id) ?? null };
}
