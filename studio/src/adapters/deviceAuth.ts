/**
 * Signing in with a subscription you already pay for.
 *
 * GitHub Copilot and a ChatGPT subscription have no API key to paste: you
 * sign in the way a TV app does — the server shows a short code, you type it
 * into a page in your browser, and it polls until you have. The endpoint is
 * created by the server at the end; no credential ever passes through here.
 *
 * `/device/start` gives the code, the page and a poll id; `/device/poll`
 * answers pending / authorized / failed at the interval the provider asked
 * for; `/device/cancel` drops a flow that was abandoned, so an unfinished
 * sign-in does not sit in the server's memory until it expires.
 */

export interface DeviceProvider {
  /** The value `/setup <name>` uses, and the id in the picker. */
  id: string;
  label: string;
  prefix: string;
  /** What the user is signing in to, in one line. */
  note: string;
}

export const DEVICE_PROVIDERS: DeviceProvider[] = [
  {
    id: 'copilot',
    label: 'GitHub Copilot',
    prefix: '/api/copilot',
    note: 'Signs in with your GitHub account and uses your Copilot subscription. No API key.',
  },
  {
    id: 'chatgpt-subscription',
    label: 'ChatGPT subscription',
    prefix: '/api/chatgpt-subscription',
    note: 'Signs in with your OpenAI account and uses your ChatGPT plan. No API key.',
  },
];

export function deviceProvider(id: string): DeviceProvider | undefined {
  return DEVICE_PROVIDERS.find((p) => p.id === id);
}

export interface DeviceStart {
  pollId: string;
  userCode: string;
  /** The page to open. `complete` already carries the code. */
  verificationUri: string;
  verificationUriComplete: string;
  intervalMs: number;
  expiresInMs: number;
}

export type DevicePoll =
  | { status: 'pending'; detail?: string }
  | { status: 'authorized'; endpoint: Record<string, unknown> }
  | { status: 'failed'; error: string };

async function post<T>(url: string, body?: Record<string, string>): Promise<T> {
  const init: RequestInit = { method: 'POST', credentials: 'same-origin' };
  if (body) {
    const fd = new FormData();
    for (const [k, v] of Object.entries(body)) fd.append(k, v);
    init.body = fd;
  }
  const r = await fetch(url, init);
  const text = await r.text();
  let data: unknown = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    /* not json */
  }
  if (!r.ok) {
    const d = data as { detail?: unknown; error?: string };
    throw new Error(typeof d.detail === 'string' ? d.detail : d.error ?? `HTTP ${r.status}`);
  }
  return data as T;
}

export async function startDeviceFlow(provider: DeviceProvider, fields: Record<string, string> = {}): Promise<DeviceStart> {
  const d = await post<Record<string, unknown>>(`${provider.prefix}/device/start`, fields);
  const uri = String(d.verification_uri ?? '');
  return {
    pollId: String(d.poll_id ?? ''),
    userCode: String(d.user_code ?? ''),
    verificationUri: uri,
    verificationUriComplete: String(d.verification_uri_complete ?? '') || uri,
    intervalMs: (Number(d.interval) || 5) * 1000,
    expiresInMs: (Number(d.expires_in) || 900) * 1000,
  };
}

export async function pollDeviceFlow(provider: DeviceProvider, pollId: string): Promise<DevicePoll> {
  const d = await post<Record<string, unknown>>(`${provider.prefix}/device/poll`, { poll_id: pollId });
  const status = String(d.status ?? 'pending');
  if (status === 'authorized') return { status: 'authorized', endpoint: (d.endpoint as Record<string, unknown>) ?? {} };
  if (status === 'failed') return { status: 'failed', error: String(d.error ?? 'denied') };
  return { status: 'pending', detail: typeof d.detail === 'string' ? d.detail : undefined };
}

export async function cancelDeviceFlow(provider: DeviceProvider, pollId: string): Promise<void> {
  try {
    await post(`${provider.prefix}/device/cancel`, { poll_id: pollId });
  } catch {
    /* it expires on its own; nothing to tell the user */
  }
}
