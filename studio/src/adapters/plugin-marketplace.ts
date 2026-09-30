import { ApiError, getJson } from './api';

export interface MarketplacePlugin {
  id: string;
  name: string;
  purpose: string;
  repository_url: string;
  required: boolean;
  source_kind: string | null;
  local_path: string | null;
  clone_path: string;
  state: 'not_installed' | 'cloned' | 'linked' | 'invalid_link' | 'conflict';
  can_install: boolean;
  error?: string;
}

export interface MarketplaceCatalogue {
  root: string;
  plugins: MarketplacePlugin[];
}

export const listMarketplace = () => getJson<MarketplaceCatalogue>('/api/plugin-marketplace');

async function change(id: string, action: string, body = {}): Promise<unknown> {
  const response = await fetch(`/api/plugin-marketplace/${encodeURIComponent(id)}/${action}`, {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({})) as { detail?: string };
  if (!response.ok) throw new ApiError(payload.detail || `Plugin action failed (${response.status})`, response.status);
  return payload;
}

export const installMarketplacePlugin = (id: string) => change(id, 'install');
export const linkMarketplacePlugin = (id: string, path: string) => change(id, 'link', { path });
export const unlinkMarketplacePlugin = (id: string) => change(id, 'unlink');
