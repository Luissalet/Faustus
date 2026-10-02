import { responseReason } from './api';

/**
 * Lean mode, pinned per chat: a thin typed mirror of `src/chat_mode.py` and
 * `routes/chat_mode_routes.py` (GET/POST /api/sessions/{id}/mode).
 *
 * `lean` drops the optional prompt blocks and the non-core tools (skills
 * index, repo map, instincts, MCP/plugin tools); `normal` is the usual
 * harness. The mode is stored with the session, and the prompt-shaping
 * settings are frozen with it, so a later global change only reaches new
 * chats. Lean never adds authority: the permission settings are untouched.
 */
export type ChatMode = 'lean' | 'normal';

export interface ChatModeView {
  mode: ChatMode;
  /** False while the chat has not chosen: `mode` is then the global default. */
  stored: boolean;
  default: ChatMode;
  pinned: boolean;
  pinned_at?: string | null;
  pinned_sha256?: string | null;
  pinned_keys?: string[];
  drops?: unknown;
}

const LEAN_WORDS = /^(lean|ligero|light|slim)$/i;
const NORMAL_WORDS = /^(normal|full|completo)$/i;

/** `/mode lean` and `/mode normal` (English or Spanish) are the chat mode;
 *  anything else belongs to the behaviour modes. Null when it is not one. */
export function parseChatModeWord(args: string): ChatMode | null {
  const word = args.trim();
  if (LEAN_WORDS.test(word)) return 'lean';
  if (NORMAL_WORDS.test(word)) return 'normal';
  return null;
}

async function payloadMessage(response: Response, path: string): Promise<string> {
  try {
    const body: unknown = await response.clone().json();
    const flat = body && typeof body === 'object' ? (body as Record<string, unknown>).error : null;
    if (typeof flat === 'string' && flat.trim()) return flat;
  } catch {
    /* fall through to the generic reason */
  }
  return responseReason(response, path);
}

async function request(path: string, init?: RequestInit): Promise<ChatModeView> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) throw new Error(await payloadMessage(response, path));
  return (await response.json()) as ChatModeView;
}

export function getChatMode(sessionId: string, signal?: AbortSignal): Promise<ChatModeView> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/mode`, { signal });
}

export function setChatMode(sessionId: string, mode: ChatMode): Promise<ChatModeView> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/mode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  });
}
