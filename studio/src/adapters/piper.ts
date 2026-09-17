/** Piper TTS: installed voices, the recommended catalogue, and the two
 * admin-only actions (download a voice, install the engine binary). */

export interface PiperVoiceInfo {
  name: string;
  language: string;
  quality: string;
  installed: boolean;
}

export interface PiperStatus {
  dependency_installed: boolean;
  runtime: 'python' | 'binary' | null;
  installed_voices: PiperVoiceInfo[];
  catalogue: PiperVoiceInfo[];
}

async function callJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init });
  const text = await r.text();
  let data: unknown = {};
  try { data = text ? JSON.parse(text) : {}; } catch { /* not json */ }
  if (!r.ok) {
    const d = data as { detail?: unknown };
    throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${r.status}`);
  }
  return data as T;
}

export function loadPiperStatus(): Promise<PiperStatus> {
  return callJson<PiperStatus>('/api/tts/piper/voices');
}

export function downloadPiperVoice(name: string): Promise<{ success: boolean; name: string }> {
  return callJson('/api/tts/piper/voices/download', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  });
}

export function installPiperEngine(): Promise<{ success: boolean; path: string }> {
  return callJson('/api/tts/piper/install-binary', { method: 'POST' });
}
