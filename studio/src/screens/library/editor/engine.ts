/**
 * The pixel editor's engine: document, tools, selection, history and the
 * two canvases it paints (picture + overlay). React renders the chrome from
 * `snapshot()` and forwards pointer events in document coordinates; every
 * pixel operation lives here or in `lib/pixel`, never in a component.
 */
import {
  activeLayer,
  activeMask,
  alphaBounds,
  applyMaskAlpha,
  buildLassoMask,
  chamferFeather,
  cloneCanvas,
  cloneSegment,
  combineMasks,
  computeSnap,
  createLayer,
  createMask,
  cropDoc,
  ctx2d,
  cutMaskAlpha,
  dilateMask,
  drawCheckerboard,
  featherMask,
  flatten,
  flipDoc,
  floodFillMask,
  fullMask,
  gaussianBlur,
  hasPixels,
  invalidateAdjCache,
  invertMask,
  makeCanvas,
  mergedMask,
  motionBlur,
  newDoc,
  renderedLayer,
  resizeDoc,
  restore,
  rotateDoc,
  scaleDoc,
  shapeEdge,
  snapshot as takeSnapshot,
  strokeSegment,
  tracePolygon,
  transformLayerPixels,
  zoomBlur,
  type Adjustment,
  type AdjustmentLayer,
  type Doc,
  type HandleId,
  type Layer,
  type MaskLayer,
  type Point,
  type Snapshot,
  type SnapGuide,
} from '../../../lib/pixel';

export type Tool = 'move' | 'crop' | 'transform' | 'brush' | 'eraser' | 'clone' | 'lasso' | 'wand' | 'sam' | 'inpaint' | 'rembg' | 'sharpen' | 'harmonize' | 'style' | 'upscale';

export type SelectMode = 'replace' | 'add' | 'subtract';

export interface Modifiers {
  shift: boolean;
  alt: boolean;
  ctrl: boolean;
}

export interface StrokeSettings {
  opacity: number; // 0..100
  flow: number;
  softness: number; // 0..300
}

export interface OverlayColors {
  selection: string;
  mask: string;
  guide: string;
  handle: string;
  crop: string;
  clone: string;
}

export interface TransformSession {
  layerId: string;
  origCanvas: HTMLCanvasElement;
  origOffset: { x: number; y: number };
  origW: number;
  origH: number;
  width: number;
  height: number;
  rotation: number;
  flipH: boolean;
  flipV: boolean;
  aspectLock: boolean;
}

export interface CropRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type SettingKey =
  | 'color'
  | 'brushSize'
  | 'brush'
  | 'eraser'
  | 'clone'
  | 'wandTolerance'
  | 'wandLive'
  | 'selectMode'
  | 'selectionVisible'
  | 'maskTint'
  | 'inpaintErase'
  | 'inpaintPrompt'
  | 'inpaintStrength'
  | 'inpaintModel'
  | 'sharpenAmount'
  | 'harmonizePrompt'
  | 'harmonizeColor'
  | 'harmonizeSeam'
  | 'harmonizeModel'
  | 'stylePrompt'
  | 'styleStrength'
  | 'styleModel'
  | 'samQuery'
  | 'draftName';

const MAX_HISTORY = 30;
const HANDLE_PX = 8;

export class PixelEditor {
  doc: Doc = newDoc(1, 1);
  imageId: string | null = null;
  imageName = '';
  originalExt: 'png' | 'jpg' = 'png';
  draftId: string | null = null;
  draftName = '';
  dirty = false;

  tool: Tool = 'move';
  previousTool: Tool = 'move';
  color = '#e06c75';
  brushSize = 24;
  brush: StrokeSettings = { opacity: 100, flow: 100, softness: 100 };
  eraser: StrokeSettings = { opacity: 100, flow: 100, softness: 100 };
  clone: StrokeSettings = { opacity: 100, flow: 100, softness: 100 };
  wandTolerance = 24;
  wandLive = false;
  selectMode: SelectMode = 'replace';
  selFeather = 0;
  selGrow = 0;
  selectionVisible = true;
  maskVisible = true;
  maskTint = '#ff6e6e';
  inpaintErase = false;
  inpaintPrompt = '';
  inpaintStrength = 75;
  inpaintModel = '';
  lastInpaintLayerId: string | null = null;
  inpaintFeather = 0;
  inpaintEdge = 0;
  lastEdgeLayerId: string | null = null;
  edgeFeather = 0;
  edgeGrow = 0;
  sharpenAmount = 50;
  harmonizePrompt = '';
  harmonizeColor = 65;
  harmonizeSeam = 0;
  harmonizeModel = '';
  stylePrompt = '';
  styleStrength = 55;
  styleModel = '';
  samQuery = '';

  zoom = 1;
  panX = 0;
  panY = 0;
  fitMode: 'fit' | 'actual' | 'custom' = 'fit';

  undoStack: Snapshot[] = [];
  redoStack: Snapshot[] = [];
  /** Labels of the states that led here, oldest first; the last is "now". */
  historyLabels: string[] = [];

  selection: HTMLCanvasElement | null = null;
  selectionRaw: HTMLCanvasElement | null = null;
  lassoPoints: Point[] = [];
  lassoActive = false;
  crop: CropRect | null = null;
  transform: TransformSession | null = null;
  hoveredHandle: HandleId | null = null;
  stagedAdjustment: { layerId: string; adj: Adjustment; skipId: string | null } | null = null;
  filterPreview: { layerId: string; canvas: HTMLCanvasElement } | null = null;
  guides: SnapGuide[] = [];
  cloneSource: Point | null = null;
  cursor: Point | null = null;
  busy: string | null = null;

  private main: HTMLCanvasElement | null = null;
  private overlay: HTMLCanvasElement | null = null;
  private colors: OverlayColors = { selection: '#4cc2ff', mask: '#ff6e6e', guide: '#ffb020', handle: '#ffffff', crop: '#ffffff', clone: '#4cc2ff' };
  private listeners = new Set<() => void>();
  private version = 0;
  private drawing = false;
  private last: Point = { x: 0, y: 0 };
  private moveStart: Point | null = null;
  private moveOrigin: Point | null = null;
  private cropStart: Point | null = null;
  private cropMoveStart: { at: Point; rect: CropRect } | null = null;
  private transformHandle: HandleId | null = null;
  private transformStart: { at: Point; w: number; h: number; rot: number; center: Point } | null = null;
  private cloneStrokeStart: Point | null = null;
  private cloneSnapshot: HTMLCanvasElement | null = null;
  private wandSeed: Point | null = null;
  private wandCache: { layerId: string; data: Uint8ClampedArray; w: number; h: number } | null = null;
  private inpaintStrokeErase = false;
  private samPoints: { x: number; y: number; label: number }[] = [];
  onPersist: (() => void) | null = null;
  onToast: ((message: string) => void) | null = null;

  /* ── Subscriptions ── */

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getVersion = (): number => this.version;

  notify(): void {
    this.version++;
    for (const fn of this.listeners) fn();
  }

  private touch(): void {
    this.dirty = true;
    this.onPersist?.();
  }

  attach(main: HTMLCanvasElement, overlay: HTMLCanvasElement, colors: OverlayColors): void {
    this.main = main;
    this.overlay = overlay;
    this.colors = colors;
    this.composite();
  }

  setColors(colors: OverlayColors): void {
    this.colors = colors;
    this.drawOverlay();
  }

  /* ── Document lifecycle ── */

  loadBlank(width: number, height: number): void {
    this.doc = newDoc(width, height);
    const bg = createLayer(this.doc, 'Background');
    const bctx = ctx2d(bg.canvas);
    bctx.fillStyle = '#ffffff';
    bctx.fillRect(0, 0, width, height);
    bg.isBase = true;
    const edit = createLayer(this.doc, 'Edit');
    this.doc.layers.push(bg, edit);
    this.doc.activeLayerId = edit.id;
    this.resetSession();
    this.composite();
    this.notify();
  }

  loadImage(img: HTMLImageElement | HTMLCanvasElement, name: string): void {
    const width = img.width, height = img.height;
    this.doc = newDoc(width, height);
    const base = createLayer(this.doc, name || 'Original');
    ctx2d(base.canvas).drawImage(img, 0, 0);
    base.isBase = true;
    this.doc.layers.push(base);
    this.doc.activeLayerId = base.id;
    this.resetSession();
    this.composite();
    this.notify();
  }

  loadDoc(doc: Doc): void {
    this.doc = doc;
    this.resetSession();
    this.composite();
    this.notify();
  }

  private resetSession(): void {
    this.undoStack = [];
    this.redoStack = [];
    this.historyLabels = ['Opened'];
    this.selection = null;
    this.selectionRaw = null;
    this.lassoPoints = [];
    this.lassoActive = false;
    this.crop = null;
    this.transform = null;
    this.stagedAdjustment = null;
    this.filterPreview = null;
    this.cloneSource = null;
    this.lastInpaintLayerId = null;
    this.lastEdgeLayerId = null;
    this.wandCache = null;
    this.samPoints = [];
    this.dirty = false;
    this.fitMode = 'fit';
  }

  /* ── History ── */

  saveState(label: string): void {
    this.undoStack.push(takeSnapshot(this.doc, label));
    if (this.undoStack.length > MAX_HISTORY) this.undoStack.shift();
    this.redoStack = [];
    this.historyLabels = [...this.historyLabels, label].slice(-MAX_HISTORY - 1);
  }

  undo(): void {
    const snap = this.undoStack.pop();
    if (!snap) return;
    this.redoStack.push(takeSnapshot(this.doc, snap.label));
    restore(this.doc, snap);
    this.historyLabels = this.historyLabels.slice(0, -1);
    this.afterDocChange();
  }

  redo(): void {
    const snap = this.redoStack.pop();
    if (!snap) return;
    this.undoStack.push(takeSnapshot(this.doc, snap.label));
    restore(this.doc, snap);
    this.historyLabels = [...this.historyLabels, snap.label];
    this.afterDocChange();
  }

  /** Jump back `steps` states (0 = now). */
  jumpHistory(index: number): void {
    const back = this.historyLabels.length - 1 - index;
    for (let i = 0; i < back; i++) this.undo();
  }

  private afterDocChange(): void {
    this.selection = null;
    this.selectionRaw = null;
    this.lassoPoints = [];
    this.crop = null;
    this.transform = null;
    this.wandCache = null;
    this.touch();
    this.composite();
    this.notify();
  }

  /* ── Layers ── */

  get active(): Layer | null {
    return activeLayer(this.doc);
  }

  setActive(id: string): void {
    this.doc.activeLayerId = id;
    this.wandCache = null;
    this.notify();
  }

  addLayer(): Layer {
    this.saveState('Add layer');
    const layer = createLayer(this.doc, `Layer ${this.doc.layers.length + 1}`);
    const idx = this.doc.activeLayerId ? this.doc.layers.findIndex((l) => l.id === this.doc.activeLayerId) : -1;
    this.doc.layers.splice(idx >= 0 ? idx + 1 : this.doc.layers.length, 0, layer);
    this.doc.activeLayerId = layer.id;
    this.touch();
    this.composite();
    this.notify();
    return layer;
  }

  addLayerFromCanvas(name: string, canvas: HTMLCanvasElement, offset = { x: 0, y: 0 }, label = 'Import'): Layer {
    this.saveState(label);
    const layer = createLayer(this.doc, name, canvas.width, canvas.height);
    ctx2d(layer.canvas).drawImage(canvas, 0, 0);
    layer.offset = { ...offset };
    this.doc.layers.push(layer);
    this.doc.activeLayerId = layer.id;
    this.touch();
    this.composite();
    this.notify();
    return layer;
  }

  duplicateLayer(id: string): void {
    const src = this.doc.layers.find((l) => l.id === id);
    if (!src) return;
    this.saveState('Duplicate layer');
    const copy = createLayer(this.doc, `${src.name} copy`, src.canvas.width, src.canvas.height);
    ctx2d(copy.canvas).drawImage(src.canvas, 0, 0);
    copy.offset = { ...src.offset };
    copy.opacity = src.opacity;
    copy.adjustments = src.adjustments.map((a) => ({ ...a, params: JSON.parse(JSON.stringify(a.params)) }));
    const idx = this.doc.layers.indexOf(src);
    this.doc.layers.splice(idx + 1, 0, copy);
    this.doc.activeLayerId = copy.id;
    this.touch();
    this.composite();
    this.notify();
  }

  deleteLayer(id: string): void {
    const idx = this.doc.layers.findIndex((l) => l.id === id);
    if (idx < 0) return;
    this.saveState('Delete layer');
    this.doc.layers.splice(idx, 1);
    if (this.doc.activeLayerId === id) this.doc.activeLayerId = this.doc.layers[Math.max(0, idx - 1)]?.id ?? null;
    this.touch();
    this.composite();
    this.notify();
  }

  updateLayer(id: string, patch: Partial<Pick<Layer, 'name' | 'visible' | 'opacity' | 'locked'>>, record = true): void {
    const layer = this.doc.layers.find((l) => l.id === id);
    if (!layer) return;
    if (record && (patch.name !== undefined || patch.locked !== undefined)) this.saveState(patch.name !== undefined ? 'Rename layer' : 'Lock layer');
    Object.assign(layer, patch);
    this.touch();
    this.composite();
    this.notify();
  }

  moveLayer(id: string, to: number): void {
    const from = this.doc.layers.findIndex((l) => l.id === id);
    if (from < 0 || to < 0 || to >= this.doc.layers.length || from === to) return;
    this.saveState('Reorder layers');
    const [layer] = this.doc.layers.splice(from, 1);
    this.doc.layers.splice(to, 0, layer);
    this.touch();
    this.composite();
    this.notify();
  }

  mergeDown(id: string): void {
    const idx = this.doc.layers.findIndex((l) => l.id === id);
    if (idx <= 0) return;
    this.saveState('Merge down');
    const top = this.doc.layers[idx], below = this.doc.layers[idx - 1];
    const merged = this.mergeTwo(below, top);
    this.doc.layers.splice(idx - 1, 2, merged);
    this.doc.activeLayerId = merged.id;
    this.touch();
    this.composite();
    this.notify();
  }

  private mergeTwo(below: Layer, top: Layer): Layer {
    const bx = Math.min(below.offset.x, top.offset.x), by = Math.min(below.offset.y, top.offset.y);
    const ex = Math.max(below.offset.x + below.canvas.width, top.offset.x + top.canvas.width);
    const ey = Math.max(below.offset.y + below.canvas.height, top.offset.y + top.canvas.height);
    const out = createLayer(this.doc, below.name, ex - bx, ey - by);
    const ctx = ctx2d(out.canvas);
    for (const l of [below, top]) {
      if (!l.visible) continue;
      ctx.globalAlpha = l.opacity;
      ctx.drawImage(renderedLayer(l), l.offset.x - bx, l.offset.y - by);
    }
    ctx.globalAlpha = 1;
    out.offset = { x: bx, y: by };
    out.isBase = below.isBase;
    out.masks = [...below.masks, ...top.masks];
    return out;
  }

  mergeAll(): void {
    if (this.doc.layers.length < 2) return;
    this.saveState('Merge all');
    const flat = flatten(this.doc);
    const out = createLayer(this.doc, 'Merged');
    ctx2d(out.canvas).drawImage(flat, 0, 0);
    out.isBase = true;
    this.doc.layers = [out];
    this.doc.activeLayerId = out.id;
    this.touch();
    this.composite();
    this.notify();
  }

  flattenCopy(): void {
    this.saveState('Flatten copy');
    const flat = flatten(this.doc);
    const out = createLayer(this.doc, 'Flattened');
    ctx2d(out.canvas).drawImage(flat, 0, 0);
    this.doc.layers.push(out);
    this.doc.activeLayerId = out.id;
    this.touch();
    this.composite();
    this.notify();
  }

  /* ── Mask sub-layers ── */

  addMask(layerId: string, fromSelection: boolean): MaskLayer | null {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    if (!layer) return null;
    this.saveState('Add mask');
    const mask = createMask(this.doc, `Mask ${layer.masks.length + 1}`);
    if (fromSelection && this.selection) ctx2d(mask.canvas).drawImage(this.selection, 0, 0);
    layer.masks.push(mask);
    layer.activeMaskId = mask.id;
    this.touch();
    this.composite();
    this.notify();
    return mask;
  }

  ensureMask(): MaskLayer | null {
    const layer = this.active;
    if (!layer) return null;
    const current = activeMask(layer);
    if (current) return current;
    if (layer.masks.length) {
      layer.activeMaskId = layer.masks[0].id;
      return layer.masks[0];
    }
    return this.addMask(layer.id, false);
  }

  setActiveMask(layerId: string, maskId: string | null): void {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    if (!layer) return;
    layer.activeMaskId = maskId;
    this.doc.activeLayerId = layerId;
    this.notify();
  }

  updateMask(layerId: string, maskId: string, patch: Partial<Pick<MaskLayer, 'visible' | 'name'>>): void {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    const mask = layer?.masks.find((m) => m.id === maskId);
    if (!mask) return;
    Object.assign(mask, patch);
    this.composite();
    this.notify();
  }

  deleteMask(layerId: string, maskId: string): void {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    if (!layer) return;
    this.saveState('Delete mask');
    layer.masks = layer.masks.filter((m) => m.id !== maskId);
    if (layer.activeMaskId === maskId) layer.activeMaskId = layer.masks[0]?.id ?? null;
    this.touch();
    this.composite();
    this.notify();
  }

  /** Merge a mask into the one above it (union). */
  mergeMaskUp(layerId: string, maskId: string): void {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    if (!layer) return;
    const idx = layer.masks.findIndex((m) => m.id === maskId);
    if (idx <= 0) return;
    this.saveState('Merge masks');
    const target = layer.masks[idx - 1];
    const ctx = ctx2d(target.canvas);
    ctx.globalCompositeOperation = 'lighter';
    ctx.drawImage(layer.masks[idx].canvas, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
    layer.masks.splice(idx, 1);
    layer.activeMaskId = target.id;
    this.touch();
    this.composite();
    this.notify();
  }

  clearMasks(): void {
    const layer = this.active;
    if (!layer) return;
    this.saveState('Clear mask');
    for (const m of layer.masks) ctx2d(m.canvas).clearRect(0, 0, m.canvas.width, m.canvas.height);
    this.touch();
    this.composite();
    this.notify();
  }

  invertMasks(): void {
    const layer = this.active;
    if (!layer) return;
    const mask = this.ensureMask();
    if (!mask) return;
    this.saveState('Invert mask');
    mask.canvas = invertMask(mask.canvas);
    this.touch();
    this.composite();
    this.notify();
  }

  setMaskVisible(v: boolean): void {
    this.maskVisible = v;
    this.composite();
    this.notify();
  }

  /* ── Adjustment layers ── */

  stageAdjustment(layerId: string, adj: Adjustment | null, skipId: string | null = null): void {
    this.stagedAdjustment = adj ? { layerId, adj, skipId } : null;
    this.composite();
  }

  addAdjustment(layerId: string, adj: Adjustment, name: string): void {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    if (!layer) return;
    this.saveState(name);
    layer.adjustments.push({ ...adj, id: `adj-${this.doc.nextId++}`, name, visible: true, opacity: 1 });
    invalidateAdjCache(layer);
    this.stagedAdjustment = null;
    this.touch();
    this.composite();
    this.notify();
  }

  updateAdjustment(layerId: string, adjId: string, patch: Partial<AdjustmentLayer>, record = false): void {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    const adj = layer?.adjustments.find((a) => a.id === adjId);
    if (!layer || !adj) return;
    if (record) this.saveState('Edit adjustment');
    Object.assign(adj, patch);
    invalidateAdjCache(layer);
    this.stagedAdjustment = null;
    this.touch();
    this.composite();
    this.notify();
  }

  deleteAdjustment(layerId: string, adjId: string): void {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    if (!layer) return;
    this.saveState('Delete adjustment');
    layer.adjustments = layer.adjustments.filter((a) => a.id !== adjId);
    invalidateAdjCache(layer);
    this.touch();
    this.composite();
    this.notify();
  }

  bakeAdjustment(layerId: string, adjId: string): void {
    const layer = this.doc.layers.find((l) => l.id === layerId);
    const adj = layer?.adjustments.find((a) => a.id === adjId);
    if (!layer || !adj) return;
    this.saveState('Bake adjustment');
    const out = renderedLayer({ ...layer, adjustments: [adj], adjCache: undefined });
    layer.canvas = cloneCanvas(out);
    layer.adjustments = layer.adjustments.filter((a) => a.id !== adjId);
    invalidateAdjCache(layer);
    this.touch();
    this.composite();
    this.notify();
  }

  /* ── Filters (whole layer, previewed live) ── */

  previewFilter(kind: 'gaussian' | 'zoom' | 'motion', params: { radius?: number; strength?: number; angle?: number; length?: number }): void {
    const layer = this.active;
    if (!layer) return;
    const snap = this.filterPreview?.layerId === layer.id ? this.filterPreview.canvas : cloneCanvas(layer.canvas);
    this.filterPreview = { layerId: layer.id, canvas: snap };
    const ctx = ctx2d(layer.canvas);
    ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    if (kind === 'gaussian') gaussianBlur(snap, params.radius ?? 0, ctx);
    else if (kind === 'zoom') zoomBlur(snap, params.strength ?? 0, ctx);
    else motionBlur(snap, params.angle ?? 0, params.length ?? 0, ctx);
    invalidateAdjCache(layer);
    this.composite();
  }

  commitFilter(label: string): void {
    const prev = this.filterPreview;
    if (!prev) return;
    const layer = this.doc.layers.find((l) => l.id === prev.layerId);
    if (!layer) return;
    const result = cloneCanvas(layer.canvas);
    ctx2d(layer.canvas).clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    ctx2d(layer.canvas).drawImage(prev.canvas, 0, 0);
    this.saveState(label);
    ctx2d(layer.canvas).clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    ctx2d(layer.canvas).drawImage(result, 0, 0);
    this.filterPreview = null;
    invalidateAdjCache(layer);
    this.touch();
    this.composite();
    this.notify();
  }

  cancelFilter(): void {
    const prev = this.filterPreview;
    if (!prev) return;
    const layer = this.doc.layers.find((l) => l.id === prev.layerId);
    if (layer) {
      ctx2d(layer.canvas).clearRect(0, 0, layer.canvas.width, layer.canvas.height);
      ctx2d(layer.canvas).drawImage(prev.canvas, 0, 0);
      invalidateAdjCache(layer);
    }
    this.filterPreview = null;
    this.composite();
  }

  /* ── Whole-document geometry ── */

  rotate(deg: 90 | 180 | 270): void {
    if (!this.doc.layers.length) return;
    this.saveState(`Rotate ${deg}°`);
    rotateDoc(this.doc, deg);
    this.fitMode = 'fit';
    this.afterDocChange();
  }

  flip(axis: 'h' | 'v'): void {
    if (!this.doc.layers.length) return;
    this.saveState(axis === 'h' ? 'Flip horizontal' : 'Flip vertical');
    flipDoc(this.doc, axis);
    this.afterDocChange();
  }

  resizeCanvas(width: number, height: number, anchor: 'tl' | 'center' = 'center'): void {
    this.saveState('Canvas size');
    resizeDoc(this.doc, width, height, anchor);
    this.fitMode = 'fit';
    this.afterDocChange();
  }

  scale(factor: number): void {
    this.saveState(`Upscale ${factor}×`);
    scaleDoc(this.doc, factor);
    this.fitMode = 'fit';
    this.afterDocChange();
  }

  /* ── Tools ── */

  setTool(tool: Tool): void {
    if (tool === this.tool) return;
    if (this.transform) this.cancelTransform();
    if (this.tool === 'crop') this.crop = null;
    if (this.tool === 'lasso' && this.lassoActive) {
      this.lassoPoints = [];
      this.lassoActive = false;
    }
    this.previousTool = this.tool;
    this.tool = tool;
    if (tool === 'inpaint' && this.brushSize < 24) this.brushSize = 40;
    if (tool === 'transform') this.startTransform();
    this.drawOverlay();
    this.notify();
  }

  set<K extends SettingKey>(key: K, value: PixelEditor[K]): void {
    (this as Pick<PixelEditor, SettingKey>)[key] = value;
    if (key === 'selectionVisible' || key === 'maskTint') this.composite();
    else this.drawOverlay();
    this.notify();
  }

  /* ── Pointer handling (document coordinates) ── */

  pointerDown(p: Point, mods: Modifiers, button = 0): void {
    const layer = this.active;
    this.cursor = p;
    switch (this.tool) {
      case 'move': {
        if (!layer || layer.locked) return;
        this.moveStart = p;
        this.moveOrigin = { ...layer.offset };
        break;
      }
      case 'crop': {
        if (this.crop && this.inside(p, this.crop)) {
          this.cropMoveStart = { at: p, rect: { ...this.crop } };
        } else {
          this.cropStart = p;
          this.crop = { x: p.x, y: p.y, w: 0, h: 0 };
        }
        break;
      }
      case 'transform': {
        if (!this.transform) return;
        const handle = this.handleAt(p);
        this.transformHandle = handle;
        const box = this.transformBox();
        if (!box) return;
        this.transformStart = { at: p, w: this.transform.width, h: this.transform.height, rot: this.transform.rotation, center: { x: box.cx, y: box.cy } };
        if (!handle) {
          this.moveStart = p;
          const l = this.doc.layers.find((x) => x.id === this.transform?.layerId);
          this.moveOrigin = l ? { ...l.offset } : null;
        }
        break;
      }
      case 'brush':
      case 'eraser':
      case 'inpaint': {
        if (!layer || layer.locked) return;
        if (this.tool === 'inpaint' && !this.ensureMask()) return;
        this.inpaintStrokeErase = this.inpaintErase !== (mods.ctrl && mods.alt);
        this.saveState(this.tool === 'brush' ? 'Brush' : this.tool === 'eraser' ? 'Erase' : 'Paint mask');
        this.drawing = true;
        this.last = p;
        this.strokeTo({ x: p.x + 0.01, y: p.y + 0.01 });
        break;
      }
      case 'clone': {
        if (!layer || layer.locked) return;
        if (mods.alt || button === 2) {
          this.cloneSource = p;
          this.drawOverlay();
          this.notify();
          return;
        }
        if (!this.cloneSource) {
          this.onToast?.('Alt-click where you want to sample from first');
          return;
        }
        this.saveState('Clone');
        this.drawing = true;
        this.last = p;
        this.cloneStrokeStart = p;
        this.cloneSnapshot = cloneCanvas(flatten(this.doc));
        break;
      }
      case 'lasso': {
        this.lassoPoints = [p];
        this.lassoActive = true;
        break;
      }
      case 'wand': {
        if (!layer) return;
        const mode: SelectMode = mods.shift ? 'add' : mods.alt ? 'subtract' : this.selectMode;
        this.wandSeed = p;
        this.wandAt(p, mode);
        break;
      }
      case 'sam': {
        this.samPoints.push({ x: Math.round(p.x), y: Math.round(p.y), label: mods.alt ? 0 : 1 });
        this.drawOverlay();
        this.notify();
        break;
      }
      default:
        break;
    }
    this.drawOverlay();
  }

  pointerMove(p: Point, mods: Modifiers): void {
    this.cursor = p;
    const layer = this.active;
    if (this.drawing) {
      this.strokeTo(p);
      return;
    }
    if (this.tool === 'move' && this.moveStart && this.moveOrigin && layer) {
      let nx = this.moveOrigin.x + (p.x - this.moveStart.x);
      let ny = this.moveOrigin.y + (p.y - this.moveStart.y);
      this.guides = [];
      if (!mods.ctrl) {
        const snapped = computeSnap({ id: layer.id, width: layer.canvas.width, height: layer.canvas.height }, nx, ny, {
          zoom: this.zoom,
          canvasW: this.doc.width,
          canvasH: this.doc.height,
          others: this.doc.layers.map((l) => ({ id: l.id, visible: l.visible, width: l.canvas.width, height: l.canvas.height, offset: l.offset })),
        });
        nx = snapped.x;
        ny = snapped.y;
        this.guides = snapped.guides;
      }
      if (!this.moveRecorded) {
        this.saveState('Move layer');
        this.moveRecorded = true;
      }
      layer.offset = { x: Math.round(nx), y: Math.round(ny) };
      this.composite();
      return;
    }
    if (this.tool === 'crop') {
      if (this.cropStart) {
        const x = Math.min(this.cropStart.x, p.x), y = Math.min(this.cropStart.y, p.y);
        let w = Math.abs(p.x - this.cropStart.x), h = Math.abs(p.y - this.cropStart.y);
        if (mods.shift) w = h = Math.max(w, h);
        this.crop = this.clampRect({ x, y, w, h });
        this.drawOverlay();
        this.notify();
      } else if (this.cropMoveStart) {
        const r = this.cropMoveStart.rect;
        this.crop = this.clampRect({ x: r.x + (p.x - this.cropMoveStart.at.x), y: r.y + (p.y - this.cropMoveStart.at.y), w: r.w, h: r.h });
        this.drawOverlay();
      }
      return;
    }
    if (this.tool === 'transform' && this.transform) {
      if (this.transformStart && this.transformHandle) {
        this.dragTransform(p, mods);
        return;
      }
      if (this.moveStart && this.moveOrigin) {
        const l = this.doc.layers.find((x) => x.id === this.transform?.layerId);
        if (l) {
          l.offset = { x: Math.round(this.moveOrigin.x + (p.x - this.moveStart.x)), y: Math.round(this.moveOrigin.y + (p.y - this.moveStart.y)) };
          this.transform.origOffset = { ...l.offset };
          this.composite();
        }
        return;
      }
      const h = this.handleAt(p);
      if (h !== this.hoveredHandle) {
        this.hoveredHandle = h;
        this.notify();
      }
      return;
    }
    if (this.tool === 'lasso' && this.lassoActive) {
      const lastPt = this.lassoPoints[this.lassoPoints.length - 1];
      if (!lastPt || Math.hypot(p.x - lastPt.x, p.y - lastPt.y) > 1.5) this.lassoPoints.push(p);
      this.drawOverlay();
      return;
    }
    this.drawOverlay();
  }

  private moveRecorded = false;

  pointerUp(p: Point): void {
    this.cursor = p;
    if (this.drawing) {
      this.drawing = false;
      this.cloneSnapshot = null;
      this.touch();
      this.notify();
    }
    if (this.moveStart) {
      this.moveStart = null;
      this.moveOrigin = null;
      this.moveRecorded = false;
      this.guides = [];
      this.touch();
      this.notify();
    }
    if (this.cropStart) {
      this.cropStart = null;
      if (this.crop && (this.crop.w < 2 || this.crop.h < 2)) this.crop = null;
      this.notify();
    }
    if (this.cropMoveStart) this.cropMoveStart = null;
    if (this.transformStart) {
      this.transformStart = null;
      this.transformHandle = null;
      this.notify();
    }
    if (this.tool === 'lasso' && this.lassoActive) {
      this.lassoActive = false;
      if (this.lassoPoints.length >= 3) this.bakeLasso();
      else this.lassoPoints = [];
      this.notify();
    }
    this.drawOverlay();
  }

  pointerLeave(): void {
    this.cursor = null;
    this.drawOverlay();
  }

  private inside(p: Point, r: CropRect): boolean {
    return p.x >= r.x && p.y >= r.y && p.x <= r.x + r.w && p.y <= r.y + r.h;
  }

  private clampRect(r: CropRect): CropRect {
    const x = Math.max(0, Math.min(this.doc.width, r.x)), y = Math.max(0, Math.min(this.doc.height, r.y));
    return { x, y, w: Math.max(0, Math.min(this.doc.width - x, r.w)), h: Math.max(0, Math.min(this.doc.height - y, r.h)) };
  }

  /* ── Strokes ── */

  private strokeTo(p: Point): void {
    const layer = this.active;
    if (!layer) return;
    if (this.tool === 'clone') {
      if (!this.cloneSource || !this.cloneStrokeStart || !this.cloneSnapshot) return;
      const dx = this.cloneSource.x - this.cloneStrokeStart.x, dy = this.cloneSource.y - this.cloneStrokeStart.y;
      const off = layer.offset;
      cloneSegment(
        ctx2d(layer.canvas),
        this.cloneSnapshot,
        { x: this.last.x - off.x, y: this.last.y - off.y },
        { x: p.x - off.x, y: p.y - off.y },
        { x: this.last.x + dx, y: this.last.y + dy },
        { x: p.x + dx, y: p.y + dy },
        { size: this.brushSize, opacity: this.clone.opacity / 100, flow: this.clone.flow / 100, softness: this.clone.softness / 300 },
      );
      invalidateAdjCache(layer);
      this.last = p;
      this.composite();
      return;
    }
    const mask = this.tool === 'inpaint' ? activeMask(layer) : activeMask(layer) && (this.tool === 'brush' || this.tool === 'eraser') ? activeMask(layer) : null;
    const target = mask ? mask.canvas : layer.canvas;
    const off = mask ? { x: 0, y: 0 } : layer.offset;
    const settings = this.tool === 'eraser' ? this.eraser : this.brush;
    const erase = this.tool === 'eraser' || (this.tool === 'inpaint' && this.inpaintStrokeErase);
    strokeSegment(
      ctx2d(target),
      { x: this.last.x - off.x, y: this.last.y - off.y },
      { x: p.x - off.x, y: p.y - off.y },
      {
        size: this.brushSize,
        color: mask ? '#ffffff' : this.color,
        opacity: mask ? 1 : settings.opacity / 100,
        flow: mask ? 1 : settings.flow / 100,
        softness: mask ? 0 : settings.softness / 100,
        mode: erase ? 'erase' : 'paint',
      },
    );
    if (!mask) invalidateAdjCache(layer);
    this.last = p;
    this.composite();
  }

  /* ── Selection ── */

  private setSelection(raw: HTMLCanvasElement | null): void {
    this.selectionRaw = raw;
    this.selection = raw ? this.shapeSelection(raw) : null;
    this.composite();
    this.notify();
  }

  private shapeSelection(raw: HTMLCanvasElement): HTMLCanvasElement {
    let out = raw;
    if (this.selGrow) out = dilateMask(out, this.selGrow);
    if (this.selFeather > 0) out = chamferFeather(out, this.selFeather);
    return out;
  }

  refineSelection(feather: number, grow: number): void {
    this.selFeather = feather;
    this.selGrow = grow;
    if (this.selectionRaw) this.selection = this.shapeSelection(this.selectionRaw);
    this.composite();
    this.notify();
  }

  hasSelection(): boolean {
    return !!this.selection;
  }

  private bakeLasso(): void {
    const mask = buildLassoMask(this.lassoPoints, this.doc.width, this.doc.height, 0, 0, 0, 0);
    this.lassoPoints = [];
    this.setSelection(combineMasks(this.selectionRaw, mask, this.selectMode));
  }

  private wandAt(p: Point, mode: SelectMode): void {
    const layer = this.active;
    if (!layer) return;
    if (!this.wandCache || this.wandCache.layerId !== layer.id) {
      const { width, height } = layer.canvas;
      this.wandCache = { layerId: layer.id, data: ctx2d(layer.canvas).getImageData(0, 0, width, height).data, w: width, h: height };
    }
    const lx = Math.floor(p.x - layer.offset.x), ly = Math.floor(p.y - layer.offset.y);
    const local = floodFillMask(this.wandCache.data, this.wandCache.w, this.wandCache.h, lx, ly, this.wandTolerance);
    if (!local) return;
    const docMask = makeCanvas(this.doc.width, this.doc.height);
    ctx2d(docMask).drawImage(local, layer.offset.x, layer.offset.y);
    this.setSelection(combineMasks(this.selectionRaw, docMask, mode));
  }

  retuneWand(): void {
    if (this.wandSeed) this.wandAt(this.wandSeed, 'replace');
  }

  selectAll(): void {
    this.setSelection(fullMask(this.doc.width, this.doc.height));
  }

  clearSelection(): void {
    this.lassoPoints = [];
    this.lassoActive = false;
    this.samPoints = [];
    this.setSelection(null);
  }

  invertSelection(): void {
    if (!this.selectionRaw) return this.selectAll();
    this.setSelection(invertMask(this.selectionRaw));
  }

  deleteSelectedPixels(): void {
    const layer = this.active;
    if (!layer || !this.selection) return;
    this.saveState('Delete selection');
    cutMaskAlpha(layer.canvas, this.selection, -layer.offset.x, -layer.offset.y);
    invalidateAdjCache(layer);
    this.touch();
    this.composite();
    this.notify();
  }

  copySelectionToLayer(cut = false): void {
    const layer = this.active;
    if (!layer || !this.selection) return;
    this.saveState(cut ? 'Cut to layer' : 'Copy to layer');
    const copy = createLayer(this.doc, `${layer.name} selection`, layer.canvas.width, layer.canvas.height);
    ctx2d(copy.canvas).drawImage(renderedLayer(layer), 0, 0);
    applyMaskAlpha(copy.canvas, this.selection, -layer.offset.x, -layer.offset.y);
    copy.offset = { ...layer.offset };
    // Trim to the pixels that survived, so Move and Transform hug the object.
    const b = alphaBounds(copy.canvas, 1);
    if (b) {
      const trimmed = makeCanvas(b.w, b.h);
      ctx2d(trimmed).drawImage(copy.canvas, -b.x, -b.y);
      copy.canvas = trimmed;
      copy.offset = { x: layer.offset.x + b.x, y: layer.offset.y + b.y };
    }
    if (cut) cutMaskAlpha(layer.canvas, this.selection, -layer.offset.x, -layer.offset.y);
    const idx = this.doc.layers.indexOf(layer);
    this.doc.layers.splice(idx + 1, 0, copy);
    this.doc.activeLayerId = copy.id;
    invalidateAdjCache(layer);
    this.touch();
    this.composite();
    this.notify();
  }

  selectionToMask(): void {
    const layer = this.active;
    if (!layer || !this.selection) return;
    const mask = this.ensureMask();
    if (!mask) return;
    this.saveState('Selection to mask');
    const ctx = ctx2d(mask.canvas);
    ctx.globalCompositeOperation = 'lighter';
    ctx.drawImage(this.selection, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
    this.touch();
    this.composite();
    this.notify();
  }

  /** The selection as base64 PNG for tools that accept a hint, or null. */
  selectionHint(): string | null {
    return this.selection ? this.selection.toDataURL('image/png').split(',')[1] : null;
  }

  samInput(): { points: { x: number; y: number; label: number }[]; query: string } {
    return { points: [...this.samPoints], query: this.samQuery.trim() };
  }

  applySamMask(mask: HTMLCanvasElement, mode: SelectMode): void {
    this.samPoints = [];
    this.setSelection(combineMasks(this.selectionRaw, mask, mode));
  }

  /* ── Crop ── */

  applyCrop(): void {
    if (!this.crop || this.crop.w < 1 || this.crop.h < 1) return;
    this.saveState('Crop');
    cropDoc(this.doc, this.crop);
    this.crop = null;
    this.fitMode = 'fit';
    this.afterDocChange();
  }

  cancelCrop(): void {
    this.crop = null;
    this.drawOverlay();
    this.notify();
  }

  /* ── Transform ── */

  startTransform(): void {
    const layer = this.active;
    if (!layer) return;
    this.transform = {
      layerId: layer.id,
      origCanvas: cloneCanvas(layer.canvas),
      origOffset: { ...layer.offset },
      origW: layer.canvas.width,
      origH: layer.canvas.height,
      width: layer.canvas.width,
      height: layer.canvas.height,
      rotation: 0,
      flipH: false,
      flipV: false,
      aspectLock: true,
    };
    this.drawOverlay();
    this.notify();
  }

  updateTransform(patch: Partial<Pick<TransformSession, 'width' | 'height' | 'rotation' | 'flipH' | 'flipV' | 'aspectLock'>>): void {
    if (!this.transform) return;
    const tr = this.transform;
    if (patch.width !== undefined && tr.aspectLock && patch.height === undefined) patch.height = Math.round((patch.width * tr.origH) / tr.origW);
    if (patch.height !== undefined && tr.aspectLock && patch.width === undefined) patch.width = Math.round((patch.height * tr.origW) / tr.origH);
    Object.assign(tr, patch);
    this.previewTransform();
    this.notify();
  }

  private previewTransform(): void {
    const tr = this.transform;
    if (!tr) return;
    const layer = this.doc.layers.find((l) => l.id === tr.layerId);
    if (!layer) return;
    const out = transformLayerPixels(tr.origCanvas, { width: tr.width, height: tr.height, rotation: tr.rotation, flipH: tr.flipH, flipV: tr.flipV });
    const cx = tr.origOffset.x + tr.origW / 2, cy = tr.origOffset.y + tr.origH / 2;
    layer.canvas = out;
    layer.offset = { x: Math.round(cx - out.width / 2), y: Math.round(cy - out.height / 2) };
    invalidateAdjCache(layer);
    this.composite();
  }

  private transformBox(): { x: number; y: number; w: number; h: number; cx: number; cy: number } | null {
    const tr = this.transform;
    if (!tr) return null;
    const cx = tr.origOffset.x + tr.origW / 2, cy = tr.origOffset.y + tr.origH / 2;
    return { x: cx - tr.width / 2, y: cy - tr.height / 2, w: tr.width, h: tr.height, cx, cy };
  }

  private handlePositions(): { id: HandleId; x: number; y: number }[] {
    const tr = this.transform;
    const box = this.transformBox();
    if (!tr || !box) return [];
    const rad = (tr.rotation * Math.PI) / 180;
    const rot = (x: number, y: number) => ({ x: box.cx + (x - box.cx) * Math.cos(rad) - (y - box.cy) * Math.sin(rad), y: box.cy + (x - box.cx) * Math.sin(rad) + (y - box.cy) * Math.cos(rad) });
    const rotDist = 28 / this.zoom;
    return [
      { id: 'tl', ...rot(box.x, box.y) },
      { id: 'tr', ...rot(box.x + box.w, box.y) },
      { id: 'bl', ...rot(box.x, box.y + box.h) },
      { id: 'br', ...rot(box.x + box.w, box.y + box.h) },
      { id: 'rot', ...rot(box.cx, box.y - rotDist) },
    ];
  }

  private handleAt(p: Point): HandleId | null {
    const r = (HANDLE_PX + 4) / this.zoom;
    for (const h of this.handlePositions()) if (Math.hypot(p.x - h.x, p.y - h.y) <= r) return h.id;
    return null;
  }

  private dragTransform(p: Point, mods: Modifiers): void {
    const tr = this.transform, st = this.transformStart, handle = this.transformHandle;
    if (!tr || !st || !handle) return;
    if (handle === 'rot') {
      const a0 = Math.atan2(st.at.y - st.center.y, st.at.x - st.center.x);
      const a1 = Math.atan2(p.y - st.center.y, p.x - st.center.x);
      let deg = st.rot + ((a1 - a0) * 180) / Math.PI;
      if (mods.shift) deg = Math.round(deg / 15) * 15;
      tr.rotation = Math.round(deg * 10) / 10;
    } else {
      const sx = handle === 'tl' || handle === 'bl' ? -1 : 1;
      const sy = handle === 'tl' || handle === 'tr' ? -1 : 1;
      const rad = (-tr.rotation * Math.PI) / 180;
      const dx0 = p.x - st.at.x, dy0 = p.y - st.at.y;
      const dx = dx0 * Math.cos(rad) - dy0 * Math.sin(rad), dy = dx0 * Math.sin(rad) + dy0 * Math.cos(rad);
      let w = Math.max(4, st.w + dx * sx * 2), h = Math.max(4, st.h + dy * sy * 2);
      if (tr.aspectLock && !mods.shift) {
        const ratio = tr.origW / tr.origH;
        if (Math.abs(dx) > Math.abs(dy)) h = w / ratio;
        else w = h * ratio;
      }
      tr.width = Math.round(w);
      tr.height = Math.round(h);
    }
    this.previewTransform();
    this.notify();
  }

  applyTransform(): void {
    const tr = this.transform;
    if (!tr) return;
    const layer = this.doc.layers.find((l) => l.id === tr.layerId);
    if (layer) {
      const result = cloneCanvas(layer.canvas), offset = { ...layer.offset };
      layer.canvas = tr.origCanvas;
      layer.offset = tr.origOffset;
      this.saveState('Transform');
      layer.canvas = result;
      layer.offset = offset;
      invalidateAdjCache(layer);
    }
    this.transform = null;
    this.touch();
    this.composite();
    this.notify();
    if (this.tool === 'transform') this.setTool('move');
  }

  cancelTransform(): void {
    const tr = this.transform;
    if (!tr) return;
    const layer = this.doc.layers.find((l) => l.id === tr.layerId);
    if (layer) {
      layer.canvas = tr.origCanvas;
      layer.offset = tr.origOffset;
      invalidateAdjCache(layer);
    }
    this.transform = null;
    this.composite();
    this.notify();
  }

  /* ── AI result plumbing ── */

  /** Add the AI result as a new layer and keep the caches its sliders need. */
  addInpaintResult(img: HTMLImageElement, prompt: string, hardMask: HTMLCanvasElement, padPx: number): Layer {
    this.saveState('Inpaint');
    const short = prompt.trim().replace(/\s+/g, ' ').slice(0, 40);
    const layer = createLayer(this.doc, short ? `Inpaint: ${short}` : 'Inpaint result');
    const lctx = ctx2d(layer.canvas);
    lctx.imageSmoothingEnabled = true;
    lctx.imageSmoothingQuality = 'high';
    lctx.drawImage(img, 0, 0, this.doc.width, this.doc.height);
    layer.inpaintSource = { ai: cloneCanvas(layer.canvas), mask: cloneCanvas(hardMask), padPx };
    this.reshapeInpaint(layer, 0, 0);
    this.doc.layers.push(layer);
    this.doc.activeLayerId = layer.id;
    this.lastInpaintLayerId = layer.id;
    this.inpaintFeather = 0;
    this.inpaintEdge = 0;
    for (const ly of this.doc.layers) for (const mk of ly.masks) mk.visible = false;
    this.touch();
    this.composite();
    this.notify();
    return layer;
  }

  private reshapeInpaint(layer: Layer, feather: number, edge: number): void {
    const src = layer.inpaintSource;
    if (!src) return;
    let shaped = src.mask;
    if (edge !== 0) shaped = dilateMask(shaped, edge);
    const soft = featherMask(shaped, feather);
    const ctx = ctx2d(layer.canvas);
    ctx.save();
    ctx.globalCompositeOperation = 'source-over';
    ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    ctx.drawImage(src.ai, 0, 0);
    ctx.globalCompositeOperation = 'destination-in';
    ctx.drawImage(soft, 0, 0);
    ctx.restore();
    invalidateAdjCache(layer);
  }

  tuneInpaintEdge(feather: number, edge: number): void {
    const layer = this.doc.layers.find((l) => l.id === this.lastInpaintLayerId);
    if (!layer) return;
    this.inpaintFeather = feather;
    this.inpaintEdge = edge;
    this.reshapeInpaint(layer, feather, edge);
    this.touch();
    this.composite();
    this.notify();
  }

  /**
   * Match the last inpaint result to its surroundings with an adjustment
   * layer: compare the mean colour of the base pixels in a ring around the
   * mask with the result's mean inside it, and shift midtones + brightness.
   */
  autoMatchInpaint(): boolean {
    const layer = this.doc.layers.find((l) => l.id === this.lastInpaintLayerId);
    const src = layer?.inpaintSource;
    if (!layer || !src) return false;
    const w = this.doc.width, h = this.doc.height;
    const ring = dilateMask(src.mask, Math.max(8, src.padPx));
    const base = flatten(this.doc, { only: (l) => l.id !== layer.id });
    const bd = ctx2d(base).getImageData(0, 0, w, h).data;
    const rd = ctx2d(ring).getImageData(0, 0, w, h).data;
    const md = ctx2d(src.mask).getImageData(0, 0, w, h).data;
    const ad = ctx2d(src.ai).getImageData(0, 0, w, h).data;
    const outer = [0, 0, 0, 0], inner = [0, 0, 0, 0];
    for (let i = 0; i < md.length; i += 4) {
      if (md[i + 3] > 127) {
        inner[0] += ad[i];
        inner[1] += ad[i + 1];
        inner[2] += ad[i + 2];
        inner[3]++;
      } else if (rd[i + 3] > 127 && bd[i + 3] > 0) {
        outer[0] += bd[i];
        outer[1] += bd[i + 1];
        outer[2] += bd[i + 2];
        outer[3]++;
      }
    }
    if (!inner[3] || !outer[3]) return false;
    const mean = (acc: number[]) => [acc[0] / acc[3], acc[1] / acc[3], acc[2] / acc[3]];
    const o = mean(outer), n = mean(inner);
    const luma = (c: number[]) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    const brightness = Math.max(0.5, Math.min(1.5, luma(o) / Math.max(1, luma(n))));
    const shift = (a: number, b: number) => Math.max(-100, Math.min(100, Math.round((a - b) / 0.6)));
    const mid = { r: shift(o[0], n[0] * brightness), g: shift(o[1], n[1] * brightness), b: shift(o[2], n[2] * brightness) };
    this.saveState('Match colour');
    layer.adjustments.push(
      { id: `adj-${this.doc.nextId++}`, name: 'Brightness / Contrast', type: 'brightness-contrast', params: { brightness: Math.round(brightness * 100) / 100, contrast: 1 }, visible: true, opacity: 1 },
      { id: `adj-${this.doc.nextId++}`, name: 'Color balance', type: 'color-balance', params: { shadows: { r: 0, g: 0, b: 0 }, midtones: mid, highlights: { r: 0, g: 0, b: 0 } }, visible: true, opacity: 1 },
    );
    invalidateAdjCache(layer);
    this.touch();
    this.composite();
    this.notify();
    return true;
  }

  addResultLayer(img: HTMLImageElement, name: string, opts: { hideOthers?: boolean; edgeTunable?: boolean; resizeDoc?: boolean } = {}): Layer {
    this.saveState(name);
    if (opts.resizeDoc && (img.width !== this.doc.width || img.height !== this.doc.height)) {
      this.doc.width = img.width;
      this.doc.height = img.height;
      this.fitMode = 'fit';
    }
    const layer = createLayer(this.doc, name);
    const lctx = ctx2d(layer.canvas);
    lctx.imageSmoothingEnabled = true;
    lctx.imageSmoothingQuality = 'high';
    lctx.drawImage(img, 0, 0, layer.canvas.width, layer.canvas.height);
    if (opts.hideOthers) for (const l of this.doc.layers) l.visible = false;
    if (opts.edgeTunable) {
      layer.edgeSource = cloneCanvas(layer.canvas);
      this.lastEdgeLayerId = layer.id;
      this.edgeFeather = 0;
      this.edgeGrow = 0;
    }
    this.doc.layers.push(layer);
    this.doc.activeLayerId = layer.id;
    this.touch();
    this.composite();
    this.notify();
    return layer;
  }

  tuneEdge(feather: number, grow: number): void {
    const layer = this.doc.layers.find((l) => l.id === this.lastEdgeLayerId);
    if (!layer?.edgeSource) return;
    this.edgeFeather = feather;
    this.edgeGrow = grow;
    const shaped = shapeEdge(layer.edgeSource, feather, grow);
    const ctx = ctx2d(layer.canvas);
    ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    ctx.drawImage(shaped, 0, 0);
    invalidateAdjCache(layer);
    this.touch();
    this.composite();
    this.notify();
  }

  /** Flat image plus the (padded) inpaint mask the diffusion call needs. */
  inpaintPayload(): { image: HTMLCanvasElement; mask: HTMLCanvasElement; hard: HTMLCanvasElement; padPx: number } | null {
    const hard = mergedMask(this.doc);
    if (!hard || !hasPixels(hard, 2)) return null;
    const padPx = Math.min(80, Math.max(20, Math.round(Math.min(this.doc.width, this.doc.height) * 0.04)));
    return { image: flatten(this.doc), mask: dilateMask(hard, padPx), hard, padPx };
  }

  /** Mask of the transparent areas (blurred outward) for outpainting, or null when the canvas is full. */
  outpaintMask(): HTMLCanvasElement | null {
    const flat = flatten(this.doc);
    const w = this.doc.width, h = this.doc.height;
    const data = ctx2d(flat).getImageData(0, 0, w, h).data;
    const raw = makeCanvas(w, h);
    const rctx = ctx2d(raw);
    const img = rctx.createImageData(w, h);
    let empty = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] === 0) {
        img.data[i] = img.data[i + 1] = img.data[i + 2] = 255;
        img.data[i + 3] = 255;
        empty++;
      }
    }
    if (!empty) return null;
    rctx.putImageData(img, 0, 0);
    const expanded = makeCanvas(w, h);
    const ectx = ctx2d(expanded);
    ectx.filter = 'blur(12px)';
    ectx.drawImage(raw, 0, 0);
    ectx.filter = 'none';
    const ed = ectx.getImageData(0, 0, w, h);
    for (let i = 0; i < ed.data.length; i += 4) {
      const v = ed.data[i + 3] > 6 ? 255 : 0;
      ed.data[i] = ed.data[i + 1] = ed.data[i + 2] = v;
      ed.data[i + 3] = v;
    }
    ectx.putImageData(ed, 0, 0);
    return expanded;
  }

  /** Union of every visible layer above the bottom one (for harmonize). */
  foregroundUnion(): HTMLCanvasElement | null {
    const visible = this.doc.layers.filter((l) => l.visible);
    if (visible.length < 2) return null;
    const out = makeCanvas(this.doc.width, this.doc.height);
    const ctx = ctx2d(out);
    for (const l of visible.slice(1)) ctx.drawImage(l.canvas, l.offset.x, l.offset.y);
    if (!hasPixels(out, 2)) return null;
    const bin = makeCanvas(this.doc.width, this.doc.height);
    const bctx = ctx2d(bin);
    const src = ctx.getImageData(0, 0, out.width, out.height);
    const img = bctx.createImageData(out.width, out.height);
    for (let i = 0; i < src.data.length; i += 4) {
      const v = src.data[i + 3] > 0 ? 255 : 0;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
      img.data[i + 3] = 255;
    }
    bctx.putImageData(img, 0, 0);
    return bin;
  }

  bodyMask(featherPx: number): string | null {
    const bin = this.foregroundUnion();
    if (!bin) return null;
    const soft = makeCanvas(bin.width, bin.height);
    const sctx = ctx2d(soft);
    sctx.filter = `blur(${featherPx}px)`;
    sctx.drawImage(bin, 0, 0);
    sctx.filter = 'none';
    return soft.toDataURL('image/png').split(',')[1];
  }

  seamMask(featherPx: number): string | null {
    const bin = this.foregroundUnion();
    if (!bin) return null;
    const w = bin.width, h = bin.height;
    const blur = makeCanvas(w, h);
    const blctx = ctx2d(blur);
    blctx.filter = `blur(${featherPx}px)`;
    blctx.drawImage(bin, 0, 0);
    blctx.filter = 'none';
    const blurred = blctx.getImageData(0, 0, w, h);
    const mask = blctx.createImageData(w, h);
    for (let i = 0; i < blurred.data.length; i += 4) {
      const dist = Math.abs(blurred.data[i] - 128);
      const wt = Math.max(0, 255 - dist * 2);
      mask.data[i] = mask.data[i + 1] = mask.data[i + 2] = wt;
      mask.data[i + 3] = 255;
    }
    blctx.putImageData(mask, 0, 0);
    const soft = makeCanvas(w, h);
    const sctx = ctx2d(soft);
    sctx.filter = `blur(${Math.max(2, Math.floor(featherPx / 4))}px)`;
    sctx.drawImage(blur, 0, 0);
    sctx.filter = 'none';
    return soft.toDataURL('image/png').split(',')[1];
  }

  setBusy(label: string | null): void {
    this.busy = label;
    this.notify();
  }

  /* ── Rendering ── */

  flat(): HTMLCanvasElement {
    return flatten(this.doc);
  }

  composite(): void {
    const main = this.main;
    if (!main) return;
    if (main.width !== this.doc.width || main.height !== this.doc.height) {
      main.width = this.doc.width;
      main.height = this.doc.height;
    }
    const ctx = ctx2d(main);
    drawCheckerboard(ctx, main.width, main.height);
    ctx.drawImage(flatten(this.doc, { staged: this.stagedAdjustment }), 0, 0);
    if (this.maskVisible) {
      const union = mergedMask(this.doc);
      if (union) {
        const tint = makeCanvas(main.width, main.height);
        const tctx = ctx2d(tint);
        tctx.drawImage(union, 0, 0);
        tctx.globalCompositeOperation = 'source-in';
        tctx.fillStyle = this.maskTint;
        tctx.fillRect(0, 0, tint.width, tint.height);
        ctx.globalAlpha = 0.32;
        ctx.drawImage(tint, 0, 0);
        ctx.globalAlpha = 1;
      }
    }
    if (this.selection && this.selectionVisible) {
      const tint = makeCanvas(main.width, main.height);
      const tctx = ctx2d(tint);
      tctx.drawImage(this.selection, 0, 0);
      tctx.globalCompositeOperation = 'source-in';
      tctx.fillStyle = this.colors.selection;
      tctx.fillRect(0, 0, tint.width, tint.height);
      ctx.globalAlpha = 0.3;
      ctx.drawImage(tint, 0, 0);
      ctx.globalAlpha = 1;
    }
    this.drawOverlay();
  }

  drawOverlay(): void {
    const ov = this.overlay;
    if (!ov) return;
    if (ov.width !== this.doc.width || ov.height !== this.doc.height) {
      ov.width = this.doc.width;
      ov.height = this.doc.height;
    }
    const ctx = ctx2d(ov);
    ctx.clearRect(0, 0, ov.width, ov.height);
    const px = 1 / Math.max(this.zoom, 0.01);
    ctx.lineWidth = px * 1.5;
    if (this.lassoPoints.length > 1) {
      ctx.save();
      ctx.strokeStyle = this.colors.selection;
      ctx.setLineDash([6 * px, 4 * px]);
      tracePolygon(ctx, this.lassoPoints);
      ctx.stroke();
      ctx.restore();
    }
    if (this.crop) {
      ctx.save();
      ctx.fillStyle = 'rgba(0,0,0,0.55)';
      ctx.beginPath();
      ctx.rect(0, 0, ov.width, ov.height);
      ctx.rect(this.crop.x, this.crop.y, this.crop.w, this.crop.h);
      ctx.fill('evenodd');
      ctx.strokeStyle = this.colors.crop;
      ctx.strokeRect(this.crop.x, this.crop.y, this.crop.w, this.crop.h);
      ctx.setLineDash([4 * px, 4 * px]);
      ctx.globalAlpha = 0.5;
      for (let i = 1; i < 3; i++) {
        ctx.beginPath();
        ctx.moveTo(this.crop.x + (this.crop.w * i) / 3, this.crop.y);
        ctx.lineTo(this.crop.x + (this.crop.w * i) / 3, this.crop.y + this.crop.h);
        ctx.moveTo(this.crop.x, this.crop.y + (this.crop.h * i) / 3);
        ctx.lineTo(this.crop.x + this.crop.w, this.crop.y + (this.crop.h * i) / 3);
        ctx.stroke();
      }
      ctx.restore();
    }
    if (this.transform) {
      const box = this.transformBox();
      const handles = this.handlePositions();
      if (box) {
        ctx.save();
        ctx.translate(box.cx, box.cy);
        ctx.rotate((this.transform.rotation * Math.PI) / 180);
        ctx.strokeStyle = this.colors.handle;
        ctx.setLineDash([5 * px, 3 * px]);
        ctx.strokeRect(-box.w / 2, -box.h / 2, box.w, box.h);
        ctx.restore();
        ctx.save();
        ctx.fillStyle = this.colors.handle;
        ctx.strokeStyle = this.colors.selection;
        ctx.lineWidth = px * 2;
        const r = HANDLE_PX * px * 0.5;
        for (const h of handles) {
          ctx.beginPath();
          if (h.id === 'rot') ctx.arc(h.x, h.y, r * 1.2, 0, Math.PI * 2);
          else ctx.rect(h.x - r, h.y - r, r * 2, r * 2);
          ctx.fill();
          ctx.stroke();
        }
        const top = handles.find((h) => h.id === 'rot');
        const tl = handles.find((h) => h.id === 'tl'), tr = handles.find((h) => h.id === 'tr');
        if (top && tl && tr) {
          ctx.beginPath();
          ctx.moveTo((tl.x + tr.x) / 2, (tl.y + tr.y) / 2);
          ctx.lineTo(top.x, top.y);
          ctx.stroke();
        }
        ctx.restore();
      }
    }
    if (this.guides.length) {
      ctx.save();
      ctx.strokeStyle = this.colors.guide;
      ctx.lineWidth = px;
      for (const g of this.guides) {
        ctx.beginPath();
        if (g.vertical) {
          ctx.moveTo(g.at, 0);
          ctx.lineTo(g.at, ov.height);
        } else {
          ctx.moveTo(0, g.at);
          ctx.lineTo(ov.width, g.at);
        }
        ctx.stroke();
      }
      ctx.restore();
    }
    if (this.tool === 'clone' && this.cloneSource) {
      ctx.save();
      ctx.strokeStyle = this.colors.clone;
      ctx.lineWidth = px * 1.5;
      const r = Math.max(4 * px, this.brushSize / 2);
      const s = this.cloneSource;
      ctx.beginPath();
      ctx.arc(s.x, s.y, r, 0, Math.PI * 2);
      ctx.moveTo(s.x - r * 1.4, s.y);
      ctx.lineTo(s.x + r * 1.4, s.y);
      ctx.moveTo(s.x, s.y - r * 1.4);
      ctx.lineTo(s.x, s.y + r * 1.4);
      ctx.stroke();
      ctx.restore();
    }
    if (this.tool === 'sam' && this.samPoints.length) {
      ctx.save();
      for (const p of this.samPoints) {
        ctx.fillStyle = p.label ? this.colors.selection : this.colors.mask;
        ctx.strokeStyle = this.colors.handle;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 5 * px, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
      ctx.restore();
    }
    if (this.cursor && (this.tool === 'brush' || this.tool === 'eraser' || this.tool === 'clone' || this.tool === 'inpaint')) {
      ctx.save();
      ctx.strokeStyle = this.colors.handle;
      ctx.lineWidth = px;
      ctx.beginPath();
      ctx.arc(this.cursor.x, this.cursor.y, this.brushSize / 2, 0, Math.PI * 2);
      ctx.stroke();
      ctx.strokeStyle = 'rgba(0,0,0,0.6)';
      ctx.beginPath();
      ctx.arc(this.cursor.x, this.cursor.y, this.brushSize / 2 + px, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }
  }

  /** Bounds of the active layer's pixels in document space (for "fit to content"). */
  activeBounds(): { x: number; y: number; w: number; h: number } | null {
    const layer = this.active;
    if (!layer) return null;
    const b = alphaBounds(layer.canvas, 2);
    return b ? { x: b.x + layer.offset.x, y: b.y + layer.offset.y, w: b.w, h: b.h } : null;
  }
}
