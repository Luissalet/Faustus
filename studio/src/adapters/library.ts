import { asArray, getJson } from './api';

/**
 * One view model over two subsystems (UI-041).
 *
 * Images live in the gallery tables and documents in their own; the plan is
 * explicit that the Library federates them in the frontend rather than
 * waiting for a backend merge. What the user wants is "find the thing I
 * made", not "remember which tool made it".
 */
export interface Artifact {
  id: string;
  kind: 'imagen' | 'documento';
  title: string;
  subtitle?: string;
  /** Images only. Documents have no thumbnail and must not fake one. */
  imageUrl?: string;
  width?: number | null;
  height?: number | null;
  session?: string | null;
  createdAt?: string | null;
  meta?: string;
}

interface GalleryItem {
  id: number | string;
  filename?: string;
  url?: string;
  prompt?: string | null;
  caption?: string | null;
  model?: string | null;
  session_name?: string | null;
  width?: number | null;
  height?: number | null;
  file_size?: number | null;
  created_at?: string | null;
}

interface DocumentItem {
  id: number | string;
  title?: string | null;
  preview?: string | null;
  language?: string | null;
  session_name?: string | null;
  version_count?: number | null;
  updated_at?: string | null;
  created_at?: string | null;
}

export async function loadLibrary(signal?: AbortSignal): Promise<{
  artifacts: Artifact[];
  degraded: string[];
}> {
  const degraded: string[] = [];

  const [gallery, documents] = await Promise.all([
    getJson<unknown>('/api/gallery/library', signal).catch(() => {
      degraded.push('imágenes');
      return { items: [] };
    }),
    getJson<unknown>('/api/documents/library', signal).catch(() => {
      degraded.push('documentos');
      return { documents: [] };
    }),
  ]);

  const artifacts: Artifact[] = [
    ...asArray<GalleryItem>(gallery, 'items').map((item) => ({
      id: `img-${item.id}`,
      kind: 'imagen' as const,
      title: item.caption?.trim() || item.prompt?.trim() || item.filename || 'Imagen',
      subtitle: item.prompt && item.caption ? item.prompt : undefined,
      imageUrl: item.url ?? (item.filename ? `/api/generated-image/${item.filename}` : undefined),
      width: item.width,
      height: item.height,
      session: item.session_name,
      createdAt: item.created_at,
      meta: [item.model, item.width && item.height ? `${item.width}×${item.height}` : null]
        .filter(Boolean)
        .join(' · '),
    })),
    ...asArray<DocumentItem>(documents, 'documents').map((item) => ({
      id: `doc-${item.id}`,
      kind: 'documento' as const,
      title: item.title?.trim() || 'Documento sin título',
      subtitle: item.preview?.trim().slice(0, 160) || undefined,
      session: item.session_name,
      createdAt: item.updated_at ?? item.created_at,
      meta: [
        item.language,
        item.version_count ? `${item.version_count} versiones` : null,
      ]
        .filter(Boolean)
        .join(' · '),
    })),
  ];

  artifacts.sort(
    (a, b) => Date.parse(b.createdAt ?? '') - Date.parse(a.createdAt ?? '') || 0,
  );

  return { artifacts, degraded };
}
