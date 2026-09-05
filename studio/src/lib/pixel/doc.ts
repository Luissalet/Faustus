/**
 * The layered document: layers with offsets, mask sub-layers and adjustment
 * stacks, plus flattening, snapshots and project serialisation. Ported from
 * the legacy `state.js`, `layer-helpers.js`, `composite-helpers.js` and the
 * draft/project code in `galleryEditor.js`; the DOM is not involved.
 */
import { renderAdjustmentStack, stackSignature, type Adjustment, type AdjustmentLayer } from './adjust';
import { cloneCanvas, ctx2d, loadImage, makeCanvas } from './canvas';

export interface MaskLayer {
  id: string;
  name: string;
  canvas: HTMLCanvasElement;
  visible: boolean;
}

export interface Layer {
  id: string;
  name: string;
  canvas: HTMLCanvasElement;
  visible: boolean;
  opacity: number;
  locked: boolean;
  isBase: boolean;
  offset: { x: number; y: number };
  masks: MaskLayer[];
  activeMaskId: string | null;
  adjustments: AdjustmentLayer[];
  /** Cache of the AI result behind an inpaint layer, so edge sliders re-shape alpha without a second model call. */
  inpaintSource?: { ai: HTMLCanvasElement; mask: HTMLCanvasElement; padPx: number };
  /** Pristine result of a background removal, for live edge cleanup. */
  edgeSource?: HTMLCanvasElement;
  adjCache?: { key: string; canvas: HTMLCanvasElement };
}

export interface Doc {
  width: number;
  height: number;
  layers: Layer[];
  activeLayerId: string | null;
  nextId: number;
}

export function newDoc(width: number, height: number): Doc {
  return { width, height, layers: [], activeLayerId: null, nextId: 1 };
}

export function createLayer(doc: Doc, name: string, width = doc.width, height = doc.height): Layer {
  return {
    id: `layer-${doc.nextId++}`,
    name,
    canvas: makeCanvas(width, height),
    visible: true,
    opacity: 1,
    locked: false,
    isBase: false,
    offset: { x: 0, y: 0 },
    masks: [],
    activeMaskId: null,
    adjustments: [],
  };
}

export function createMask(doc: Doc, name: string, width = doc.width, height = doc.height): MaskLayer {
  return { id: `mask-${doc.nextId++}`, name, canvas: makeCanvas(width, height), visible: true };
}

export function activeLayer(doc: Doc): Layer | null {
  return doc.layers.find((l) => l.id === doc.activeLayerId) ?? null;
}

export function activeMask(layer: Layer | null): MaskLayer | null {
  if (!layer || !layer.activeMaskId) return null;
  return layer.masks.find((m) => m.id === layer.activeMaskId) ?? null;
}

export function layerIndex(doc: Doc, id: string): number {
  return doc.layers.findIndex((l) => l.id === id);
}

export function invalidateAdjCache(layer: Layer): void {
  layer.adjCache = undefined;
}

/** The layer's pixels after its adjustment stack (memoised by signature). */
export function renderedLayer(layer: Layer, staged: Adjustment | null = null, skipId: string | null = null): HTMLCanvasElement {
  if (!layer.adjustments.length && !staged) return layer.canvas;
  const key = stackSignature(layer.adjustments, staged, skipId) + `|${layer.canvas.width}x${layer.canvas.height}`;
  if (layer.adjCache && layer.adjCache.key === key && !staged) return layer.adjCache.canvas;
  const out = renderAdjustmentStack(layer.canvas, layer.adjustments, staged, skipId);
  if (!staged) layer.adjCache = { key, canvas: out };
  return out;
}

/** Composite every visible layer into a document-sized canvas. */
export function flatten(doc: Doc, opts: { staged?: { layerId: string; adj: Adjustment; skipId: string | null } | null; only?: (l: Layer) => boolean } = {}): HTMLCanvasElement {
  const out = makeCanvas(doc.width, doc.height);
  const ctx = ctx2d(out);
  for (const layer of doc.layers) {
    if (!layer.visible) continue;
    if (opts.only && !opts.only(layer)) continue;
    const staged = opts.staged && opts.staged.layerId === layer.id ? opts.staged : null;
    const src = staged ? renderedLayer(layer, staged.adj, staged.skipId) : renderedLayer(layer);
    ctx.globalAlpha = layer.opacity;
    ctx.drawImage(src, layer.offset.x, layer.offset.y);
  }
  ctx.globalAlpha = 1;
  return out;
}

/** Union of every visible mask sub-layer as a white document-sized canvas, or null. */
export function mergedMask(doc: Doc): HTMLCanvasElement | null {
  const out = makeCanvas(doc.width, doc.height);
  const ctx = ctx2d(out);
  ctx.globalCompositeOperation = 'lighter';
  let any = false;
  for (const ly of doc.layers) {
    for (const mk of ly.masks) {
      if (!mk.visible || !mk.canvas.width) continue;
      ctx.drawImage(mk.canvas, 0, 0);
      any = true;
    }
  }
  ctx.globalCompositeOperation = 'source-over';
  return any ? out : null;
}

/** Small JPEG preview for the drafts list. */
export function thumbnail(doc: Doc, maxDim = 320, quality = 0.6): string | null {
  if (!doc.width || !doc.height) return null;
  try {
    const scale = Math.min(1, maxDim / Math.max(doc.width, doc.height));
    const c = makeCanvas(Math.round(doc.width * scale), Math.round(doc.height * scale));
    const ctx = ctx2d(c);
    ctx.drawImage(flatten(doc), 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', quality);
  } catch {
    return null;
  }
}

export function isLayerEmpty(layer: Layer): boolean {
  const { width, height } = layer.canvas;
  if (!width || !height) return true;
  const d = ctx2d(layer.canvas).getImageData(0, 0, width, height).data;
  const step = Math.max(1, Math.floor(Math.sqrt((width * height) / 40000)));
  for (let y = 0; y < height; y += step) {
    for (let x = 0; x < width; x += step) {
      if (d[(y * width + x) * 4 + 3] > 0) return false;
    }
  }
  return true;
}

/* ── Snapshots (undo/redo) ── */

export interface Snapshot {
  label: string;
  width: number;
  height: number;
  activeLayerId: string | null;
  nextId: number;
  layers: Layer[];
}

function cloneLayer(l: Layer): Layer {
  return {
    ...l,
    canvas: cloneCanvas(l.canvas),
    offset: { ...l.offset },
    masks: l.masks.map((m) => ({ ...m, canvas: cloneCanvas(m.canvas) })),
    adjustments: l.adjustments.map((a) => ({ ...a, params: JSON.parse(JSON.stringify(a.params)) })),
    adjCache: undefined,
  };
}

export function snapshot(doc: Doc, label: string): Snapshot {
  return { label, width: doc.width, height: doc.height, activeLayerId: doc.activeLayerId, nextId: doc.nextId, layers: doc.layers.map(cloneLayer) };
}

export function restore(doc: Doc, snap: Snapshot): void {
  doc.width = snap.width;
  doc.height = snap.height;
  doc.activeLayerId = snap.activeLayerId;
  doc.nextId = Math.max(doc.nextId, snap.nextId);
  doc.layers = snap.layers.map(cloneLayer);
}

/* ── Project / draft serialisation ── */

export interface ProjectJson {
  version: 2;
  imgWidth: number;
  imgHeight: number;
  activeLayerId: string | null;
  layers: {
    id: string;
    name: string;
    visible: boolean;
    opacity: number;
    locked: boolean;
    isBase: boolean;
    canvasW: number;
    canvasH: number;
    offset: { x: number; y: number };
    dataUrl: string;
    masks?: { id: string; name: string; visible: boolean; dataUrl: string }[];
    adjustments?: AdjustmentLayer[];
  }[];
}

export function serialize(doc: Doc): ProjectJson {
  return {
    version: 2,
    imgWidth: doc.width,
    imgHeight: doc.height,
    activeLayerId: doc.activeLayerId,
    layers: doc.layers.map((l) => ({
      id: l.id,
      name: l.name,
      visible: l.visible,
      opacity: l.opacity,
      locked: l.locked,
      isBase: l.isBase,
      canvasW: l.canvas.width,
      canvasH: l.canvas.height,
      offset: { ...l.offset },
      dataUrl: l.canvas.toDataURL('image/png'),
      masks: l.masks.map((m) => ({ id: m.id, name: m.name, visible: m.visible, dataUrl: m.canvas.toDataURL('image/png') })),
      adjustments: l.adjustments,
    })),
  };
}

export async function deserialize(data: Partial<ProjectJson> & { layers?: ProjectJson['layers'] }): Promise<Doc> {
  const width = Number(data.imgWidth) || 1, height = Number(data.imgHeight) || 1;
  const doc = newDoc(width, height);
  let maxId = 0;
  for (const raw of data.layers ?? []) {
    const img = await loadImage(raw.dataUrl);
    const layer = createLayer(doc, raw.name || 'Layer', raw.canvasW || img.width, raw.canvasH || img.height);
    layer.id = raw.id || layer.id;
    ctx2d(layer.canvas).drawImage(img, 0, 0);
    layer.visible = raw.visible !== false;
    layer.opacity = typeof raw.opacity === 'number' ? raw.opacity : 1;
    layer.locked = !!raw.locked;
    layer.isBase = !!raw.isBase;
    layer.offset = { x: raw.offset?.x || 0, y: raw.offset?.y || 0 };
    for (const m of raw.masks ?? []) {
      const mi = await loadImage(m.dataUrl);
      const mask = createMask(doc, m.name || 'Mask', mi.width, mi.height);
      mask.id = m.id || mask.id;
      mask.visible = m.visible !== false;
      ctx2d(mask.canvas).drawImage(mi, 0, 0);
      layer.masks.push(mask);
      maxId = Math.max(maxId, numericId(mask.id));
    }
    layer.adjustments = (raw.adjustments ?? []).map((a) => ({ ...a }));
    doc.layers.push(layer);
    maxId = Math.max(maxId, numericId(layer.id));
  }
  doc.nextId = Math.max(doc.nextId, maxId + 1);
  doc.activeLayerId = data.activeLayerId && doc.layers.some((l) => l.id === data.activeLayerId) ? data.activeLayerId : (doc.layers[doc.layers.length - 1]?.id ?? null);
  return doc;
}

function numericId(id: string): number {
  const m = /-(\d+)$/.exec(id);
  return m ? Number(m[1]) : 0;
}
