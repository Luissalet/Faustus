import { ApiError, asArray, getJson } from './api';

/**
 * The image gallery: `/api/gallery/*`, in the shapes gallery.js used.
 * Albums, tags (yours and the vision model's), favourites, upload, rotate,
 * rename, delete, zip. The pixel editor is its own module.
 */

export interface GalleryImage {
  id: string;
  filename: string;
  url: string;
  prompt: string;
  caption: string;
  model: string;
  size: string;
  quality: string;
  tags: string[];
  aiTags: string[];
  sessionId: string;
  sessionName: string;
  albumId: string;
  favorite: boolean;
  takenAt: string | null;
  camera: string;
  gps: { lat: number | null; lng: number | null } | null;
  width: number | null;
  height: number | null;
  fileSize: number | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface Album {
  id: string;
  name: string;
  description: string;
  coverUrl: string | null;
  count: number;
  createdAt: string | null;
}

export interface GalleryPage {
  items: GalleryImage[];
  total: number;
  totalTagged: number;
  tags: string[];
  models: string[];
}

export interface GalleryStats {
  photos: number;
  sizeHuman: string;
  favorites: number;
  albums: number;
}

export interface GalleryQuery {
  search?: string;
  tag?: string[];
  model?: string;
  album?: string;
  favorites?: boolean;
  sort?: 'recent' | 'oldest' | 'shuffle';
  seed?: number;
  offset?: number;
  limit?: number;
}

const str = (v: unknown) => (typeof v === 'string' ? v : '');
const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const splitTags = (v: unknown) =>
  str(v)
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean);

function imageFrom(raw: Record<string, unknown>): GalleryImage {
  const gps = raw.gps && typeof raw.gps === 'object' ? (raw.gps as { lat?: unknown; lng?: unknown }) : null;
  const filename = str(raw.filename);
  return {
    id: String(raw.id),
    filename,
    url: str(raw.url) || (filename ? `/api/generated-image/${filename}` : ''),
    prompt: str(raw.prompt),
    caption: str(raw.caption),
    model: str(raw.model),
    size: str(raw.size),
    quality: str(raw.quality),
    tags: splitTags(raw.user_tags ?? raw.tags),
    aiTags: splitTags(raw.ai_tags),
    sessionId: str(raw.session_id),
    sessionName: str(raw.session_name),
    albumId: str(raw.album_id),
    favorite: Boolean(raw.favorite),
    takenAt: str(raw.taken_at) || null,
    camera: str(raw.camera).trim(),
    gps: gps && (num(gps.lat) !== null || num(gps.lng) !== null) ? { lat: num(gps.lat), lng: num(gps.lng) } : null,
    width: num(raw.width),
    height: num(raw.height),
    fileSize: num(raw.file_size),
    createdAt: str(raw.created_at) || null,
    updatedAt: str(raw.updated_at) || null,
  };
}

function albumFrom(raw: Record<string, unknown>): Album {
  return { id: String(raw.id), name: str(raw.name), description: str(raw.description), coverUrl: str(raw.cover_url) || null, count: num(raw.count) ?? 0, createdAt: str(raw.created_at) || null };
}

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown; error?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (typeof body.error === 'string') detail = body.error;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

const jsonInit = (method: string, body?: unknown): RequestInit => ({
  method,
  credentials: 'same-origin',
  headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

const enc = (id: string) => encodeURIComponent(id);

export async function loadGallery(query: GalleryQuery = {}, signal?: AbortSignal): Promise<GalleryPage> {
  const q = new URLSearchParams();
  if (query.search) q.set('search', query.search);
  if (query.tag?.length) q.set('tag', query.tag.join(','));
  if (query.model) q.set('model', query.model);
  if (query.album) q.set('album', query.album);
  if (query.favorites) q.set('favorites', 'true');
  if (query.sort) q.set('sort', query.sort);
  if (query.seed !== undefined) q.set('seed', String(query.seed));
  q.set('offset', String(query.offset ?? 0));
  q.set('limit', String(query.limit ?? 48));
  const data = await getJson<Record<string, unknown>>(`/api/gallery/library?${q}`, signal);
  return {
    items: asArray<Record<string, unknown>>(data, 'items').map(imageFrom),
    total: num(data.total) ?? 0,
    totalTagged: num(data.total_tagged) ?? 0,
    tags: asArray<unknown>(data, 'tags').map(String),
    models: asArray<unknown>(data, 'models').map(String).filter(Boolean),
  };
}

export async function getImage(id: string): Promise<GalleryImage> {
  return imageFrom(await getJson<Record<string, unknown>>(`/api/gallery/${enc(id)}`));
}

export async function galleryStats(): Promise<GalleryStats> {
  const d = await getJson<Record<string, unknown>>('/api/gallery/stats');
  return { photos: num(d.total_photos) ?? 0, sizeHuman: str(d.total_size_human), favorites: num(d.favorites) ?? 0, albums: num(d.albums) ?? 0 };
}

export async function patchImage(id: string, patch: { tags?: string; favorite?: boolean; album_id?: string }): Promise<void> {
  await ok(await fetch(`/api/gallery/${enc(id)}`, jsonInit('PATCH', patch)), 'gallery/patch');
}

export async function toggleFavorite(id: string): Promise<boolean> {
  const r = await ok(await fetch(`/api/gallery/${enc(id)}/favorite`, jsonInit('POST')), 'gallery/favorite');
  return Boolean(((await r.json()) as { favorite?: boolean }).favorite);
}

export async function deleteImage(id: string): Promise<void> {
  await ok(await fetch(`/api/gallery/${enc(id)}`, jsonInit('DELETE')), 'gallery/delete');
}

export async function renameImage(id: string, name: string): Promise<void> {
  await ok(await fetch(`/api/gallery/${enc(id)}/rename`, jsonInit('POST', { name })), 'gallery/rename');
}

export async function rotateImage(id: string, angle = 90): Promise<void> {
  await ok(await fetch(`/api/gallery/${enc(id)}/rotate`, jsonInit('POST', { angle })), 'gallery/rotate');
}

/** Tags the image with the vision model; the server answers with an error string when none is configured. */
export async function aiTagImage(id: string): Promise<string[]> {
  const r = await ok(await fetch(`/api/gallery/${enc(id)}/ai-tag`, jsonInit('POST')), 'gallery/ai-tag');
  const d = (await r.json()) as { ok?: boolean; ai_tags?: string; error?: string };
  if (d.error) throw new ApiError(d.error, 400);
  return splitTags(d.ai_tags);
}

/** Queues every untagged image (up to `limit`) for the vision model. */
export async function aiTagBatch(limit = 100): Promise<{ queued: number; untagged: number }> {
  const r = await ok(await fetch('/api/gallery/ai-tag-batch', jsonInit('POST', { limit })), 'gallery/ai-tag-batch');
  const d = (await r.json()) as { queued?: unknown; total_untagged?: unknown };
  return { queued: num(d.queued) ?? 0, untagged: num(d.total_untagged) ?? 0 };
}

export async function uploadImages(files: File[], albumId?: string, onEach?: (done: number, total: number) => void): Promise<{ uploaded: number; failed: string[] }> {
  let uploaded = 0;
  const failed: string[] = [];
  for (const [i, file] of files.entries()) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    if (albumId) fd.append('album_id', albumId);
    try {
      await ok(await fetch('/api/gallery/upload', { method: 'POST', credentials: 'same-origin', body: fd }), 'gallery/upload');
      uploaded++;
    } catch {
      failed.push(file.name);
    }
    onEach?.(i + 1, files.length);
  }
  return { uploaded, failed };
}

export async function downloadZip(ids: string[]): Promise<Blob> {
  const r = await ok(await fetch('/api/gallery/download-zip', jsonInit('POST', { ids })), 'gallery/zip');
  return r.blob();
}

/* ── Albums ── */

export async function listAlbums(): Promise<Album[]> {
  return asArray<Record<string, unknown>>(await getJson<unknown>('/api/gallery/albums'), 'albums').map(albumFrom);
}

export async function createAlbum(name: string, description = ''): Promise<string> {
  const r = await ok(await fetch('/api/gallery/albums', jsonInit('POST', { name, description })), 'gallery/albums');
  return String(((await r.json()) as { id?: unknown }).id ?? '');
}

export async function updateAlbum(id: string, patch: { name?: string; description?: string; cover_id?: string }): Promise<void> {
  await ok(await fetch(`/api/gallery/albums/${enc(id)}`, jsonInit('PUT', patch)), 'gallery/albums/update');
}

export async function deleteAlbum(id: string): Promise<void> {
  await ok(await fetch(`/api/gallery/albums/${enc(id)}`, jsonInit('DELETE')), 'gallery/albums/delete');
}

export async function addToAlbum(albumId: string, ids: string[]): Promise<void> {
  await ok(await fetch(`/api/gallery/albums/${enc(albumId)}/add`, jsonInit('POST', { image_ids: ids })), 'gallery/albums/add');
}

export async function removeFromAlbum(albumId: string, ids: string[]): Promise<void> {
  await ok(await fetch(`/api/gallery/albums/${enc(albumId)}/remove`, jsonInit('POST', { image_ids: ids })), 'gallery/albums/remove');
}

/* ── Tag housekeeping ── */

export async function clearUserTags(): Promise<void> {
  await ok(await fetch('/api/gallery/clear-user-tags', jsonInit('POST')), 'gallery/clear-user-tags');
}

export async function clearAiTags(): Promise<void> {
  await ok(await fetch('/api/gallery/clear-ai-tags', jsonInit('POST')), 'gallery/clear-ai-tags');
}

export async function dedupeTags(): Promise<void> {
  await ok(await fetch('/api/gallery/dedupe-tags', jsonInit('POST')), 'gallery/dedupe-tags');
}

export function humanSize(bytes: number | null): string {
  if (bytes === null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
