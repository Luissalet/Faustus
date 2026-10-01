import { ApiError, getJson, responseReason } from './api';

/**
 * Chat bridges — Studio side of `routes/chat_bridge_routes.py`.
 *
 * The bridge itself lives in the server (`src/chat_bridges/`): this only
 * reads what the poller reports about itself and asks the server to check the
 * saved bot token. The token is never sent to the browser, not even masked.
 */

export interface BridgeSession {
  chat_id: string;
  title: string;
  session_id: string;
  updated: number;
  link: string;
}

export interface BridgeRefusedChat {
  chat_id: string;
  title: string;
  at: number;
}

export interface TelegramBridgeStatus {
  enabled: boolean;
  configured: boolean;
  running: boolean;
  bot_username: string;
  bot_name: string;
  last_error: string;
  last_error_at: number;
  disabled_reason: string;
  failures_in_a_row: number;
  mode: string;
  model: string;
  allowed_chats: string[];
  mapped_sessions: BridgeSession[];
  refused_chats: BridgeRefusedChat[];
  busy_chats: string[];
  queued_messages: number;
}

export const telegramStatus = (signal?: AbortSignal) => getJson<TelegramBridgeStatus>('/api/chat-bridges/telegram/status', signal);

export interface TelegramTestResult {
  ok: boolean;
  username?: string;
  name?: string;
  id?: number;
  error?: string;
}

export async function telegramTest(): Promise<TelegramTestResult> {
  const path = '/api/chat-bridges/telegram/test';
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as TelegramTestResult;
}
