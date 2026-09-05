/**
 * The image editor's server side: AI tools under `/api/image/*`, the gallery
 * save/replace/upload calls, editor drafts, and the picker of image models.
 * Everything speaks base64 PNG in and out, as the legacy editor did.
 */
import { ApiError, getJson } from './api';
import { loadGallery } from './gallery';
import { listEndpoints } from './settings';

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: unknown; error?: unknown };
      const d = j.detail ?? j.error;
      if (typeof d === 'string') detail = d;
      else if (d) detail = JSON.stringify(d);
    } catch {
      /* not json */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

async function postForm<T>(path: string, fd: FormData): Promise<T> {
  const res = await fetch(path, { method: 'POST', credentials: 'same-origin', body: fd });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: unknown; error?: unknown };
      const d = j.detail ?? j.error;
      if (typeof d === 'string') detail = d;
    } catch {
      /* not json */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

/** `{ image }` or `{ error }` — every image tool answers the same way. */
async function imageResult(path: string, body: Record<string, unknown>): Promise<string> {
  const data = await postJson<{ image?: string; error?: string }>(path, body);
  if (data.error) throw new Error(data.error);
  if (!data.image) throw new Error('No image came back');
  return data.image;
}

export interface ModelChoice {
  endpoint: string;
  model: string;
}

function withModel(body: Record<string, unknown>, choice?: ModelChoice | null): Record<string, unknown> {
  if (choice?.endpoint) body._endpoint = choice.endpoint;
  if (choice?.model) body._model = choice.model;
  return body;
}

export const inpaint = (args: { image: string; mask: string; prompt: string; width: number; height: number; strength: number; model?: ModelChoice | null }) =>
  imageResult('/api/image/inpaint', withModel({ image: args.image, mask: args.mask, prompt: args.prompt, width: args.width, height: args.height, strength: args.strength, feather: 0 }, args.model));

export const harmonize = (args: { image: string; prompt: string; colorMatch: number; seamFix: number; bodyMask: string; seamMask?: string | null; model?: ModelChoice | null }) =>
  imageResult('/api/image/harmonize', withModel({ image: args.image, prompt: args.prompt, color_match: args.colorMatch, seam_fix: args.seamFix, body_mask: args.bodyMask, ...(args.seamMask ? { seam_mask: args.seamMask } : {}) }, args.model));

export const sharpen = (image: string, amount: number) => imageResult('/api/image/sharpen', { image, amount });
export const denoise = (image: string, strength: number) => imageResult('/api/image/denoise', { image, strength });
export const upscaleLocal = (image: string, scale: number) => imageResult('/api/image/upscale-local', { image, scale });
export const removeBackground = (image: string, hintMask?: string | null) => imageResult('/api/image/remove-bg', { image, ...(hintMask ? { hint_mask: hintMask } : {}) });
export const enhanceFace = (image: string) => imageResult('/api/image/enhance-face', { image });

/** SAM: a mask from click points (`label` 1 = include, 0 = exclude), a box, or an object name. */
export const smartMask = (args: { image: string; points?: { x: number; y: number; label: number }[]; box?: [number, number, number, number]; text?: string }) =>
  imageResult('/api/image/mask', { image: args.image, ...(args.points?.length ? { points: args.points } : {}), ...(args.box ? { box: args.box } : {}), ...(args.text ? { text: args.text } : {}) });

export async function styleTransfer(blob: Blob, prompt: string, strength: number): Promise<string> {
  const fd = new FormData();
  fd.append('image', blob, 'style.png');
  fd.append('prompt', prompt);
  fd.append('strength', String(strength));
  const data = await postForm<{ image?: string; error?: string }>('/api/gallery/style-transfer', fd);
  if (data.error) throw new Error(data.error);
  if (!data.image) throw new Error('No image came back');
  return data.image;
}

export async function aiUpscale(blob: Blob, scale: number): Promise<string> {
  const fd = new FormData();
  fd.append('image', blob, 'upscale.png');
  fd.append('scale', String(scale));
  const data = await postForm<{ image?: string; error?: string }>('/api/gallery/ai-upscale', fd);
  if (data.error) throw new Error(data.error);
  if (!data.image) throw new Error('No image came back');
  return data.image;
}

/* ── Saving ── */

export async function replaceImage(id: string, blob: Blob, ext: 'png' | 'jpg'): Promise<void> {
  const fd = new FormData();
  fd.append('image', blob, `edited.${ext}`);
  await postForm<unknown>(`/api/gallery/${encodeURIComponent(id)}/replace`, fd);
}

export async function saveCopy(blob: Blob, ext: 'png' | 'jpg', albumId?: string | null): Promise<string | null> {
  const fd = new FormData();
  fd.append('file', blob, `edited.${ext}`);
  if (albumId) fd.append('album_id', albumId);
  const out = await postForm<{ id?: string; image?: { id?: string } }>('/api/gallery/upload', fd);
  return out.id ?? out.image?.id ?? null;
}

/* ── Drafts ── */

export interface DraftSummary {
  id: string;
  name: string;
  sourceImageId: string | null;
  width: number;
  height: number;
  thumbnail: string | null;
  updatedAt: string | null;
}

export interface DraftBody {
  name: string;
  source_image_id: string | null;
  width: number;
  height: number;
  payload: unknown;
  thumbnail: string | null;
}

function draftFrom(raw: Record<string, unknown>): DraftSummary {
  return {
    id: String(raw.id ?? ''),
    name: typeof raw.name === 'string' && raw.name ? raw.name : 'Untitled',
    sourceImageId: typeof raw.source_image_id === 'string' ? raw.source_image_id : null,
    width: Number(raw.width) || 0,
    height: Number(raw.height) || 0,
    thumbnail: typeof raw.thumbnail === 'string' ? raw.thumbnail : null,
    updatedAt: typeof raw.updated_at === 'string' ? raw.updated_at : null,
  };
}

export async function listDrafts(): Promise<DraftSummary[]> {
  const out = await getJson<{ drafts?: Record<string, unknown>[] }>('/api/editor-drafts');
  return (out.drafts ?? []).map(draftFrom);
}

export async function getDraft(id: string): Promise<{ summary: DraftSummary; payload: unknown } | null> {
  try {
    const out = await getJson<Record<string, unknown>>(`/api/editor-drafts/${encodeURIComponent(id)}`);
    if (!out || !out.payload) return null;
    return { summary: draftFrom(out), payload: out.payload };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export async function findDraftForImage(imageId: string): Promise<DraftSummary | null> {
  const drafts = await listDrafts();
  return drafts.find((d) => d.sourceImageId === imageId) ?? null;
}

export async function saveDraft(id: string | null, body: DraftBody): Promise<string | null> {
  if (id) {
    const res = await fetch(`/api/editor-drafts/${encodeURIComponent(id)}`, { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (res.status === 404) return saveDraft(null, body);
    if (!res.ok) throw new ApiError(`Draft save responded ${res.status}`, res.status);
    return id;
  }
  const out = await postJson<{ id?: string }>('/api/editor-drafts', body);
  return out.id ?? null;
}

export async function deleteDraft(id: string): Promise<void> {
  await fetch(`/api/editor-drafts/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' });
}

/* ── Image models ── */

export interface ImageModelOption {
  value: string; // `${baseUrl}::${model}`
  label: string;
  endpoint: string;
  model: string;
  inpaint: boolean;
  generate: boolean;
  online: boolean;
}

function modelCaps(modelId: string, endpointName: string, endpointType: string): { gen: boolean; inpaint: boolean } {
  const id = modelId.toLowerCase(), name = endpointName.toLowerCase(), type = endpointType.toLowerCase();
  const textOnly = /(?:^|[/\-_:])(gpt-?[345]|gpt-oss|claude|llama|qwen[^-]*chat|chat$|instruct$|coder)/i;
  if (textOnly.test(id) && !/image/i.test(id)) return { gen: false, inpaint: false };
  if (/dall-e-3/.test(id)) return { gen: true, inpaint: false };
  if (/dall-e-2/.test(id) || /gpt-image/.test(id)) return { gen: true, inpaint: true };
  if (/(?:^|[/\-_])(?:sd-?xl|sdxl|sd3|sd-|stable[\s-]*diffusion|flux|playground|pixart|kandinsky)/i.test(id)) {
    const isInpaintModel = /inpaint|edit|fill/i.test(id) || /inpaint|edit|fill/i.test(name);
    return { gen: !isInpaintModel || /base/i.test(id), inpaint: true };
  }
  if (type === 'image') {
    if (/inpaint|edit|fill/i.test(name)) return { gen: false, inpaint: true };
    return { gen: true, inpaint: true };
  }
  if (/inpaint|edit|fill/i.test(name)) return { gen: false, inpaint: true };
  if (/diffus|flux|sd|image/i.test(name)) return { gen: true, inpaint: true };
  return { gen: false, inpaint: false };
}

export async function listImageModels(signal?: AbortSignal): Promise<ImageModelOption[]> {
  const endpoints = await listEndpoints(signal);
  const out: ImageModelOption[] = [];
  for (const ep of endpoints) {
    if (!ep.enabled) continue;
    const models = ep.models.length ? ep.models : [''];
    const isImage = ep.modelType.toLowerCase() === 'image';
    const usable = ep.online || isImage;
    for (const modelId of models) {
      const caps = modelCaps(modelId || ep.name, ep.name, ep.modelType);
      if (!caps.gen && !caps.inpaint) continue;
      const short = modelId ? modelId.split('/').pop() ?? modelId : ep.name || ep.baseUrl;
      const hint = modelId && ep.name && ep.name !== modelId ? ` · ${ep.name}` : '';
      out.push({ value: `${ep.baseUrl}::${modelId}`, label: `${short}${hint}`, endpoint: ep.baseUrl, model: modelId, inpaint: caps.inpaint, generate: caps.gen, online: usable });
    }
  }
  return out;
}

export function parseModelChoice(value: string): ModelChoice | null {
  if (!value) return null;
  const idx = value.indexOf('::');
  if (idx < 0) return { endpoint: value, model: '' };
  return { endpoint: value.slice(0, idx), model: value.slice(idx + 2) };
}

/** Whether a Python package is installed on the server (null = unknown). */
export async function packageInstalled(name: string): Promise<boolean | null> {
  try {
    const data = await getJson<{ packages?: { name?: string; installed?: boolean }[] }>('/api/cookbook/packages');
    const pkg = (data.packages ?? []).find((p) => (p.name ?? '').toLowerCase() === name.toLowerCase());
    return pkg ? !!pkg.installed : null;
  } catch {
    return null;
  }
}

export interface LibraryPick {
  id: string;
  url: string;
  name: string;
}

export async function recentImages(limit = 60): Promise<LibraryPick[]> {
  const page = await loadGallery({ limit, sort: 'recent' });
  return page.items.map((i) => ({ id: i.id, url: i.url, name: i.filename }));
}
