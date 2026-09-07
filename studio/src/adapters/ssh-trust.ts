import { ApiError } from './api';

export interface SshFingerprint {type: string; fingerprint: string}
export interface SshTrustState {host: string; state: 'paired' | 'unpaired' | 'changed'; paired: string[]; offered: SshFingerprint[]}
export function parseSshTrust(raw: unknown): SshTrustState {
  if (!raw || typeof raw !== 'object') throw new Error('invalid_ssh_trust_response');
  const d = raw as Record<string, unknown>;
  const valid = (v: unknown): v is string => typeof v === 'string' && /^SHA256:[A-Za-z0-9+/]{43}=?$/.test(v);
  if (typeof d.host !== 'string' || !d.host || !['paired', 'unpaired', 'changed'].includes(String(d.state))
    || !Array.isArray(d.paired) || !d.paired.every(valid) || !Array.isArray(d.offered) || !d.offered.length
    || !d.offered.every(v => v && typeof v === 'object' && typeof v.type === 'string' && valid(v.fingerprint))) throw new Error('invalid_ssh_trust_response');
  return {host:d.host, state:d.state as SshTrustState['state'], paired:d.paired, offered:d.offered as SshFingerprint[]};
}

export async function sshTrustAction(action: 'fingerprint' | 'pair' | 'unpair', host: string, port: string | undefined, signal: AbortSignal, fingerprint?: string) {
  const response = await fetch(`/api/cookbook/ssh/${action}`, {
    method:'POST', credentials:'same-origin', signal, headers:{'Content-Type':'application/json'},
    body:JSON.stringify({host, ssh_port:port || null, ...(fingerprint ? {fingerprint}: {})}),
  });
  if (!response.ok) throw new ApiError('ssh_trust_failed', response.status);
  return response.json() as Promise<unknown>;
}
