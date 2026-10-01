import { readEmail, aiReply, leaveComposeHandoff } from '../adapters/email';
import type { UiEvent } from '../adapters/uiEvent';
import { EFFECTS, type EffectName } from './effects';
import { getCustomThemes, getTheme, PRESETS, saveCustomTheme, setTheme, themeFromPreset, type Colors, type Theme } from './appearance';

/** Studio's side of `ui_control` (decoded in adapters/uiEvent.ts); Studio.tsx
 * applies what touches its own state (toggles, mode, model, navigation). */

/** `open_panel` targets (the server's own names) → Studio routes. */
export const PANEL_ROUTES: Record<string, string> = {
  documents: '/library?type=documento',
  gallery: '/library?type=imagen',
  email: '/email',
  sessions: '/library?type=chats',
  notes: '/notes',
  memories: '/memory',
  skills: '/skills',
  settings: '/settings',
  cookbook: '/cookbook',
};

/** The theme a name means: Studio's own palette, one of the user's themes or a built-in preset. */
export function themeNamed(name: string): Theme | null {
  const want = name.trim().toLowerCase();
  if (!want) return null;
  if (want === 'studio') return { ...getTheme(), name: 'studio', colors: undefined, bgPattern: undefined, bgEffectColor: undefined };
  const custom = getCustomThemes();
  const mine = Object.keys(custom).find((n) => n.toLowerCase() === want);
  if (mine) return { ...custom[mine], name: mine };
  const preset = Object.keys(PRESETS).find((n) => n.toLowerCase() === want);
  return preset ? themeFromPreset(preset) : null;
}

const HEX = /^#[0-9a-f]{6}$/i;

/** `create_theme`: apply the agent's colours and keep them as one of the user's themes. */
export function themeFromAgent(name: string, colors: Record<string, unknown> | undefined, bg: Record<string, unknown> | undefined): Theme | null {
  const keys = ['bg', 'fg', 'panel', 'border', 'red'] as const;
  if (!name.trim() || !colors || !keys.every((k) => typeof colors[k] === 'string' && HEX.test(colors[k] as string))) return null;
  const current = getTheme();
  const theme: Theme = {
    name: name.trim().slice(0, 32),
    colors: Object.fromEntries(keys.map((k) => [k, colors[k] as string])) as unknown as Colors,
    font: current.font,
    density: current.density,
    textSize: current.textSize,
    frosted: bg?.frosted === true ? true : current.frosted,
  };
  const pattern = bg?.pattern;
  if (typeof pattern === 'string' && (EFFECTS as string[]).includes(pattern) && pattern !== 'none') theme.bgPattern = pattern as EffectName;
  if (typeof bg?.effectColor === 'string' && HEX.test(bg.effectColor)) theme.bgEffectColor = bg.effectColor;
  if (typeof bg?.effectIntensity === 'number') theme.bgEffectIntensity = bg.effectIntensity;
  if (typeof bg?.effectSize === 'number') theme.bgEffectSize = bg.effectSize;
  return theme;
}

/** Apply a theme event. Returns the name applied, or null when there is nothing to apply. */
export function applyThemeEvent(ev: UiEvent): { name: string; saved: boolean } | null {
  if (ev.kind === 'set_theme' && ev.themeName) {
    const theme = themeNamed(ev.themeName);
    if (!theme) return null;
    setTheme(theme);
    return { name: theme.name === 'studio' ? 'Studio' : theme.name, saved: true };
  }
  if (ev.kind === 'create_theme' && ev.themeName) {
    const theme = themeFromAgent(ev.themeName, ev.colors, ev.bg);
    if (!theme) return null;
    setTheme(theme);
    return { name: theme.name, saved: saveCustomTheme(theme.name) };
  }
  return null;
}

/**
 * `open_email_reply`: read the message, leave a reply draft for the mail
 * screen (the same hand-off the document editor uses) and return where to
 * go. `ai-reply` without a body asks the model for one first. A message
 * that cannot be read still opens, so the reply can be written from it.
 */
export async function emailReplyRoute(ev: UiEvent): Promise<string> {
  const folder = ev.folder || 'INBOX';
  const uid = ev.uid || '';
  const open = `/email?folder=${encodeURIComponent(folder)}&uid=${encodeURIComponent(uid)}`;
  if (!uid) return '/email';
  try {
    const mail = await readEmail(uid, folder, ev.accountId ?? null);
    const from = mail.fromName ? `${mail.fromName} <${mail.fromAddress}>` : mail.fromAddress;
    let body = ev.body ?? '';
    if (!body && ev.mode === 'ai-reply') {
      body = (
        await aiReply({ to: from, subject: mail.subject, originalBody: mail.body, uid, folder, accountId: ev.accountId ?? null, messageId: mail.messageId, fast: false, hint: '' })
      ).reply;
    }
    const others = ev.mode === 'reply-all' ? [mail.to, mail.cc].filter(Boolean).join(', ') : '';
    leaveComposeHandoff({
      to: from,
      cc: others,
      subject: /^re:/i.test(mail.subject) ? mail.subject : `Re: ${mail.subject}`,
      body,
      inReplyTo: mail.messageId,
      references: [mail.references, mail.messageId].filter(Boolean).join(' '),
    });
    return '/email?compose=handoff';
  } catch {
    return open;
  }
}

/* `highlight <selector> [label]` / `clear_highlight`: outline what the agent is pointing at. */
const HIGHLIGHT_CLASS = 'fs-ui-highlight';
export function highlight(selector: string, label?: string): number {
  let nodes: Element[] = [];
  try {
    nodes = Array.from(document.querySelectorAll(selector)).slice(0, 20);
  } catch {
    return 0;
  }
  for (const node of nodes) {
    node.classList.add(HIGHLIGHT_CLASS);
    if (label && node instanceof HTMLElement) node.dataset.uiHighlight = label;
  }
  if (nodes[0] instanceof HTMLElement) nodes[0].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  return nodes.length;
}
export function clearHighlights(): void {
  for (const node of Array.from(document.querySelectorAll(`.${HIGHLIGHT_CLASS}`))) {
    node.classList.remove(HIGHLIGHT_CLASS);
    if (node instanceof HTMLElement) delete node.dataset.uiHighlight;
  }
}
