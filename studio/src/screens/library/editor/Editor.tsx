import { AlertTriangle, ArrowLeft, Check, ChevronDown, Crop, Crosshair, Eraser, FlipHorizontal2, FlipVertical2, FolderOpen, History as HistoryIcon, ImagePlus, Keyboard, Lasso, Maximize2, Minus, Move, Paintbrush, Plus, Redo2, RotateCw, Save, Scan, Scaling, Sparkles, Stamp, Trash2, Undo2, Wand2, WandSparkles, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Popover, Skeleton, Toast } from '../../../components';
import { getImage } from '../../../adapters/gallery';
import * as api from '../../../adapters/imageTools';
import { adjustmentLabel, canvasToBlob, ctx2d, deserialize, imageToCanvas, loadImage, makeCanvas, serialize, thumbnail, toBase64Png, type AdjustmentType, type ProjectJson } from '../../../lib/pixel';
import { locale, t } from '../../../i18n';
import { ASK_SUGGESTIONS, matchSuggestions, parseAsk, type AskSuggestion } from './ask';
import { AdjustDialog, BlurDialog, CanvasSizeDialog, LibraryPickDialog, ShortcutsDialog, type AdjustTarget, type BlurKind } from './dialogs';
import { PixelEditor, type Tool } from './engine';
import { LayersPane } from './LayersPane';
import type { Runner } from './runner';
import { isTyping, Stage, stageZoom } from './Stage';
import { ToolPane, TOOL_LABELS } from './ToolPane';
import '../../library.css';
import '../editor.css';

/**
 * The image editor (`/library/edit`). A workbench: tools on the left, the
 * picture in the middle, the active tool's controls and the layers on the
 * right. Everything the previous editor did is here — layers with masks
 * and adjustments, crop, transform, brush, eraser, clone, lasso, wand,
 * smart select, inpaint, background removal, sharpen/denoise/faces,
 * harmonize, style, upscale, filters, drafts, save over / as copy /
 * download, project files — with one "Ask" field for the edits you would
 * rather describe.
 */
export function EditorScreen() {
  const [params] = useSearchParams();
  const img = params.get('img');
  const draft = params.get('draft');
  const fresh = params.get('new');
  if (!img && !draft && !fresh) return <Landing />;
  return <Workbench key={`${img}|${draft}|${fresh}`} imageId={img} draftId={draft} fresh={fresh} />;
}

/* ── Landing: new canvas, or resume a draft ── */

function Landing() {
  const navigate = useNavigate();
  const [drafts, setDrafts] = useState<api.DraftSummary[] | null>(null);
  const [sizeOpen, setSizeOpen] = useState(false);
  const refresh = useCallback(() => {
    api.listDrafts().then(setDrafts).catch(() => setDrafts([]));
  }, []);
  useEffect(refresh, [refresh]);
  return (
    <div className="fs-screen fs-ed-landing" data-testid="editor-landing">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Image editor')}</h1>
          <p className="fs-screen__sub">{t('Open a picture from the library, start a blank canvas, or pick up a draft.')}</p>
        </div>
        <div className="fs-ed__row">
          <Button icon={FolderOpen} label={t('Open from the library')} onClick={() => navigate('/library?type=imagen')} />
          <Button variant="primary" icon={Plus} label={t('New canvas')} onClick={() => setSizeOpen(true)} />
        </div>
      </header>
      <section className="fs-ed-landing__drafts">
        <h2>{t('Drafts')}</h2>
        {!drafts && <Skeleton label={t('Loading drafts')} count={2} />}
        {drafts && !drafts.length && <EmptyState title={t('No drafts')} body={t('Edits save themselves as drafts while you work; the ones you have not finished show up here.')} headingLevel={3} />}
        {drafts && drafts.length > 0 && (
          <ul className="fs-ed-landing__list">
            {drafts.map((d) => (
              <li key={d.id}>
                <button type="button" className="fs-ed-landing__draft" onClick={() => navigate(`/library/edit?draft=${encodeURIComponent(d.id)}`)}>
                  {d.thumbnail ? <img src={d.thumbnail} alt="" /> : <span className="fs-ed-landing__blank" aria-hidden="true" />}
                  <span className="fs-ed-landing__meta">
                    <strong>{d.name}</strong>
                    <span>
                      {d.width} × {d.height}
                      {d.updatedAt ? ` · ${new Date(d.updatedAt).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' })}` : ''}
                    </span>
                  </span>
                </button>
                <IconButton
                  icon={Trash2}
                  label={t('Delete draft')}
                  size="sm"
                  onClick={() => {
                    void api.deleteDraft(d.id).then(refresh);
                  }}
                />
              </li>
            ))}
          </ul>
        )}
      </section>
      <CanvasSizeDialog open={sizeOpen} onOpenChange={setSizeOpen} width={1024} height={1024} mode="new" onApply={(w, h) => navigate(`/library/edit?new=${w}x${h}`)} />
    </div>
  );
}

/* ── The workbench ── */

const TOOL_GROUPS: { title: string; tools: { id: Tool; icon: typeof Move; key?: string; ai?: boolean }[] }[] = [
  {
    title: 'Arrange',
    tools: [
      { id: 'move', icon: Move, key: 'V' },
      { id: 'crop', icon: Crop, key: 'C' },
      { id: 'transform', icon: Scaling, key: 'T' },
    ],
  },
  {
    title: 'Select',
    tools: [
      { id: 'lasso', icon: Lasso, key: 'L' },
      { id: 'wand', icon: Wand2, key: 'W' },
      { id: 'sam', icon: Scan, ai: true },
    ],
  },
  {
    title: 'Paint',
    tools: [
      { id: 'brush', icon: Paintbrush, key: 'B' },
      { id: 'eraser', icon: Eraser, key: 'E' },
      { id: 'clone', icon: Stamp, key: 'K' },
    ],
  },
  {
    title: 'Fix',
    tools: [
      { id: 'inpaint', icon: WandSparkles, key: 'M', ai: true },
      { id: 'rembg', icon: Crosshair, ai: true },
      { id: 'sharpen', icon: Sparkles, key: 'S', ai: true },
      { id: 'harmonize', icon: ImagePlus, ai: true },
      { id: 'style', icon: Paintbrush, ai: true },
      { id: 'upscale', icon: Maximize2, ai: true },
    ],
  },
];

const TOOL_KEYS: Record<string, Tool> = { v: 'move', c: 'crop', t: 'transform', b: 'brush', e: 'eraser', k: 'clone', l: 'lasso', w: 'wand', m: 'inpaint', s: 'sharpen' };

function Workbench({ imageId, draftId, fresh }: { imageId: string | null; draftId: string | null; fresh: string | null }) {
  const navigate = useNavigate();
  const ed = useMemo(() => new PixelEditor(), []);
  const version = useSyncExternalStore(ed.subscribe, ed.getVersion);
  const zoomApi = useMemo(() => stageZoom(ed), [ed]);
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [models, setModels] = useState<api.ImageModelOption[]>([]);
  const [rembgInstalled, setRembgInstalled] = useState<boolean | null>(null);
  const [sizeDialog, setSizeDialog] = useState(false);
  const [blur, setBlur] = useState<BlurKind | null>(null);
  const [adjust, setAdjust] = useState<AdjustTarget | null>(null);
  const [shortcuts, setShortcuts] = useState(false);
  const [libraryPick, setLibraryPick] = useState(false);
  const [confirmOver, setConfirmOver] = useState(false);
  const [sheet, setSheet] = useState<'tool' | 'layers' | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const projectRef = useRef<HTMLInputElement>(null);
  const noticeTimer = useRef<number>(0);

  const say = useCallback((msg: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text: msg, tone });
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4500);
  }, []);
  ed.onToast = say;

  /* ── Load ── */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (fresh) {
          const m = /^(\d+)x(\d+)$/.exec(fresh);
          const w = m ? Number(m[1]) : 1024, h = m ? Number(m[2]) : 1024;
          ed.loadBlank(w, h);
          ed.draftName = t('Untitled');
        } else if (draftId) {
          const d = await api.getDraft(draftId);
          if (!d) throw new Error(t('That draft no longer exists.'));
          const doc = await deserialize(d.payload as ProjectJson);
          if (cancelled) return;
          ed.draftId = d.summary.id;
          ed.draftName = d.summary.name;
          ed.imageId = d.summary.sourceImageId;
          ed.loadDoc(doc);
        } else if (imageId) {
          const image = await getImage(imageId);
          if (cancelled) return;
          ed.imageId = image.id;
          ed.imageName = image.filename;
          ed.originalExt = /\.jpe?g$/i.test(image.filename) ? 'jpg' : 'png';
          ed.draftName = image.filename.replace(/\.[^.]+$/, '');
          const found = await api.findDraftForImage(image.id).catch(() => null);
          const full = found ? await api.getDraft(found.id).catch(() => null) : null;
          if (cancelled) return;
          if (full && full.payload) {
            const doc = await deserialize(full.payload as ProjectJson);
            if (cancelled) return;
            if (doc.layers.length) {
              ed.draftId = full.summary.id;
              ed.loadDoc(doc);
              say(t('Resumed your previous edit'));
            }
          }
          if (!ed.doc.layers.length) {
            const img = await loadImage(image.url);
            if (cancelled) return;
            ed.loadImage(img, t('Original'));
          }
        }
        if (!cancelled) setState('ready');
      } catch (e) {
        if (!cancelled) {
          setLoadError((e as Error).message);
          setState('error');
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ed, imageId, draftId, fresh, say]);

  useEffect(() => {
    const ac = new AbortController();
    api.listImageModels(ac.signal).then(setModels).catch(() => setModels([]));
    api.packageInstalled('rembg').then(setRembgInstalled);
    return () => ac.abort();
  }, []);

  /* ── Drafts: save quietly after every change ── */
  const persistTimer = useRef<number>(0);
  const persistBusy = useRef<Promise<void> | null>(null);
  const persistAgain = useRef(false);
  /** True between a change and the draft that captures it; the leave guard watches this, not `dirty`. */
  const unsaved = useRef(false);
  const persist = useCallback(async () => {
    if (!ed.doc.layers.length) return;
    if (persistBusy.current) {
      persistAgain.current = true;
      return;
    }
    const body: api.DraftBody = { name: ed.draftName || t('Untitled'), source_image_id: ed.imageId, width: ed.doc.width, height: ed.doc.height, payload: serialize(ed.doc), thumbnail: thumbnail(ed.doc) };
    persistBusy.current = api
      .saveDraft(ed.draftId, body)
      .then((id) => {
        if (id) ed.draftId = id;
        if (!persistAgain.current) unsaved.current = false;
      })
      .catch(() => undefined)
      .then(() => {
        persistBusy.current = null;
        if (persistAgain.current) {
          persistAgain.current = false;
          void persist();
        }
      });
  }, [ed]);
  ed.onPersist = () => {
    unsaved.current = true;
    window.clearTimeout(persistTimer.current);
    persistTimer.current = window.setTimeout(() => void persist(), 1500);
  };
  useEffect(() => () => window.clearTimeout(persistTimer.current), []);

  /* ── AI runs ── */
  const busyRun = useCallback(
    async (label: string, work: () => Promise<void>) => {
      if (ed.busy) return;
      ed.setBusy(label);
      try {
        await work();
      } catch (e) {
        const msg = (e as Error).message || String(e);
        say(t('{what} failed: {error}', { what: TOOL_LABELS[ed.tool] ? t(TOOL_LABELS[ed.tool]) : label, error: msg }), 'warn');
      } finally {
        ed.setBusy(null);
      }
    },
    [ed, say],
  );

  const flatBlob = useCallback(() => canvasToBlob(ed.flat(), 'image/png'), [ed]);
  const decode = (b64: string) => loadImage(`data:image/png;base64,${b64}`);

  const run = useMemo<Runner>(
    () => ({
      inpaint: (kind) =>
        busyRun(kind, async () => {
          const model = api.parseModelChoice(ed.inpaintModel);
          let restoreMask: (() => void) | null = null;
          if (kind === 'outpaint') {
            const outMask = ed.outpaintMask();
            if (!outMask) {
              say(t('Nothing to outpaint: the canvas is fully covered.'));
              return;
            }
            const mask = ed.ensureMask();
            if (!mask) return;
            const saved = ctx2d(mask.canvas).getImageData(0, 0, mask.canvas.width, mask.canvas.height);
            ctx2d(mask.canvas).clearRect(0, 0, mask.canvas.width, mask.canvas.height);
            ctx2d(mask.canvas).drawImage(outMask, 0, 0);
            mask.visible = true;
            restoreMask = () => {
              ctx2d(mask.canvas).clearRect(0, 0, mask.canvas.width, mask.canvas.height);
              ctx2d(mask.canvas).putImageData(saved, 0, 0);
            };
          }
          try {
            const payload = ed.inpaintPayload();
            if (!payload) {
              say(t('Brush the area you want to inpaint first.'));
              return;
            }
            let prompt = ed.inpaintPrompt.trim();
            let strength = ed.inpaintStrength / 100;
            if (kind === 'generate' && !prompt) {
              say(t('Write what should fill the masked area.'));
              return;
            }
            if (kind === 'remove') {
              const openai = (model?.endpoint ?? '').toLowerCase().includes('api.openai.com');
              prompt = openai ? (prompt ? `Remove ${prompt}. Fill seamlessly with the surrounding background, photorealistic, no objects, no people.` : 'Remove the masked area. Fill seamlessly with the surrounding background, photorealistic, no objects, no people.') : 'seamless natural background, photorealistic, continuation of surrounding scene, empty area, no objects, no people, no text, clean';
              if (!openai) strength = 0.99;
            }
            if (kind === 'outpaint') {
              prompt = prompt || 'seamless natural continuation of the surrounding image, photorealistic, matching style, no objects, no people, no text';
              strength = 0.99;
            }
            const b64 = await api.inpaint({ image: toBase64Png(payload.image), mask: toBase64Png(payload.mask), prompt, width: ed.doc.width, height: ed.doc.height, strength, model });
            const img = await decode(b64);
            ed.addInpaintResult(img, kind === 'generate' ? prompt : kind === 'remove' ? t('Removed') : t('Outpaint'), payload.hard, payload.padPx);
            say(t('Done. Drag Edge feather and Edge to blend the result in.'));
          } finally {
            restoreMask?.();
            ed.composite();
          }
        }),
      removeBg: () =>
        busyRun('rembg', async () => {
          const b64 = await api.removeBackground(toBase64Png(ed.flat()), ed.selectionHint());
          const img = await decode(b64);
          ed.addResultLayer(img, t('Background removed'), { hideOthers: true, edgeTunable: true });
          say(t('Background removed. The original layers are hidden, not gone.'));
        }),
      sharpen: () =>
        busyRun('sharpen', async () => {
          const img = await decode(await api.sharpen(toBase64Png(ed.flat()), ed.sharpenAmount));
          ed.addResultLayer(img, t('Sharpened'));
        }),
      denoise: () =>
        busyRun('denoise', async () => {
          const img = await decode(await api.denoise(toBase64Png(ed.flat()), 0.55));
          ed.addResultLayer(img, t('Denoised'));
        }),
      enhanceFace: () =>
        busyRun('face', async () => {
          const img = await decode(await api.enhanceFace(toBase64Png(ed.flat())));
          ed.addResultLayer(img, t('Enhanced faces'));
        }),
      harmonize: () =>
        busyRun('harmonize', async () => {
          const bodyFeather = Math.max(6, Math.round(Math.min(ed.doc.width, ed.doc.height) * 0.012));
          const seamFeather = Math.max(8, Math.round(Math.min(ed.doc.width, ed.doc.height) * 0.015));
          const bodyMask = ed.bodyMask(bodyFeather);
          if (!bodyMask) {
            say(t('Harmonize needs a second layer over the base photo; there is nothing to match against.'));
            return;
          }
          const seamFix = ed.harmonizeSeam / 100;
          const img = await decode(
            await api.harmonize({ image: toBase64Png(ed.flat()), prompt: ed.harmonizePrompt.trim() || 'photorealistic, natural lighting, seamless blend', colorMatch: ed.harmonizeColor / 100, seamFix, bodyMask, seamMask: seamFix > 0.01 ? ed.seamMask(seamFeather) : null, model: api.parseModelChoice(ed.harmonizeModel) }),
          );
          ed.addResultLayer(img, t('Harmonized'));
        }),
      style: () =>
        busyRun('style', async () => {
          const prompt = ed.stylePrompt.trim();
          if (!prompt) {
            say(t('Write a style prompt first.'));
            return;
          }
          const img = await decode(await api.styleTransfer(await flatBlob(), prompt, ed.styleStrength / 100));
          ed.addResultLayer(img, `${t('Style')}: ${prompt.slice(0, 24)}`);
        }),
      samFind: () =>
        busyRun('sam', async () => {
          const input = ed.samInput();
          if (!input.points.length && !input.query) {
            say(t('Click the object or name it first.'));
            return;
          }
          const b64 = await api.smartMask({ image: toBase64Png(ed.flat()), points: input.points, text: input.query || undefined });
          const img = await decode(b64);
          ed.applySamMask(imageToCanvas(img, ed.doc.width, ed.doc.height), ed.selectMode);
        }),
      upscaleAi: () =>
        busyRun('upscale', async () => {
          const img = await decode(await api.upscaleLocal(toBase64Png(ed.flat()), 2));
          ed.addResultLayer(img, t('AI upscaled'), { resizeDoc: true });
          say(t('Upscaled to {w} × {h}', { w: ed.doc.width, h: ed.doc.height }));
        }),
      upscaleLocal: (factor) => {
        ed.scale(factor);
        say(t('Upscaled to {w} × {h}', { w: ed.doc.width, h: ed.doc.height }));
      },
      autoMatch: () =>
        busyRun('match', async () => {
          if (!ed.autoMatchInpaint()) say(t('Nothing to match against.'));
        }),
    }),
    [busyRun, ed, flatBlob, say],
  );

  /* ── Import ── */
  const importFiles = useCallback(
    async (files: File[]) => {
      for (const f of files) {
        try {
          const url = URL.createObjectURL(f);
          const img = await loadImage(url);
          URL.revokeObjectURL(url);
          const c = imageToCanvas(img, img.width, img.height);
          ed.addLayerFromCanvas(f.name.replace(/\.[^.]+$/, '') || t('Imported'), c, { x: Math.round((ed.doc.width - c.width) / 2), y: Math.round((ed.doc.height - c.height) / 2) });
          ed.setTool('move');
        } catch (e) {
          say((e as Error).message, 'warn');
        }
      }
    },
    [ed, say],
  );

  const importClipboard = useCallback(async () => {
    try {
      const items = await navigator.clipboard.read();
      for (const item of items) {
        const type = item.types.find((x) => x.startsWith('image/'));
        if (!type) continue;
        const blob = await item.getType(type);
        await importFiles([new File([blob], t('Pasted'), { type })]);
        return;
      }
      say(t('The clipboard has no image.'));
    } catch {
      say(t('The browser did not allow reading the clipboard. Paste with Ctrl+V instead.'));
    }
  }, [importFiles, say]);

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      if (isTyping(e as unknown as KeyboardEvent)) return;
      const files = [...(e.clipboardData?.files ?? [])].filter((f) => f.type.startsWith('image/'));
      if (files.length) {
        e.preventDefault();
        void importFiles(files);
      }
    };
    window.addEventListener('paste', onPaste);
    return () => window.removeEventListener('paste', onPaste);
  }, [importFiles]);

  /* ── Save ── */
  const encode = useCallback(async () => {
    const jpg = ed.originalExt === 'jpg';
    const blob = await canvasToBlob(ed.flat(), jpg ? 'image/jpeg' : 'image/png', jpg ? 0.92 : undefined);
    return { blob, ext: jpg ? ('jpg' as const) : ('png' as const) };
  }, [ed]);

  const saveOver = useCallback(async () => {
    if (!ed.imageId) return;
    setConfirmOver(false);
    setSaving('over');
    try {
      const { blob, ext } = await encode();
      await api.replaceImage(ed.imageId, blob, ext);
      ed.dirty = false;
      say(t('Saved over the original ({size} MB)', { size: (blob.size / 1024 / 1024).toFixed(1) }));
    } catch (e) {
      say(t('Save failed: {error}', { error: (e as Error).message }), 'warn');
    } finally {
      setSaving(null);
    }
  }, [ed, encode, say]);

  const saveCopy = useCallback(async () => {
    setSaving('copy');
    try {
      const { blob, ext } = await encode();
      const id = await api.saveCopy(blob, ext);
      ed.dirty = false;
      if (ed.draftId) {
        void api.deleteDraft(ed.draftId);
        ed.draftId = null;
      }
      say(id ? t('Saved as a new image in the library') : t('Saved a copy to the library'));
    } catch (e) {
      say(t('Save failed: {error}', { error: (e as Error).message }), 'warn');
    } finally {
      setSaving(null);
    }
  }, [ed, encode, say]);

  const download = useCallback((blob: Blob, name: string) => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    window.setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }, []);

  const downloadPng = useCallback(async () => {
    download(await canvasToBlob(ed.flat(), 'image/png'), `${ed.draftName || 'image'}.png`);
  }, [download, ed]);

  const saveProject = useCallback(() => {
    const json = JSON.stringify(serialize(ed.doc));
    download(new Blob([json], { type: 'application/json' }), `${ed.draftName || 'project'}.faustus.json`);
  }, [download, ed]);

  const loadProject = useCallback(
    async (file: File) => {
      try {
        const data = JSON.parse(await file.text()) as ProjectJson;
        const doc = await deserialize(data);
        if (!doc.layers.length) throw new Error(t('The file has no layers.'));
        ed.saveState('Load project');
        ed.loadDoc(doc);
        say(t('Project loaded'));
      } catch (e) {
        say(t('Could not open the project: {error}', { error: (e as Error).message }), 'warn');
      }
    },
    [ed, say],
  );

  /* ── Ask ── */
  const [ask, setAsk] = useState('');
  const [askIndex, setAskIndex] = useState(0);
  const [askFocus, setAskFocus] = useState(false);
  const suggestions = useMemo(() => (ask ? matchSuggestions(ask) : ASK_SUGGESTIONS.slice(0, 6)), [ask]);
  const runAsk = useCallback(
    (text: string) => {
      const action = parseAsk(text);
      if (!action) return;
      setAsk('');
      switch (action.kind) {
        case 'rotate':
          ed.rotate(action.deg);
          break;
        case 'flip':
          ed.flip(action.axis);
          break;
        case 'rembg':
          ed.setTool('rembg');
          void run.removeBg();
          break;
        case 'upscale':
          ed.setTool('upscale');
          if (action.factor === 2) void run.upscaleAi();
          else run.upscaleLocal(action.factor);
          break;
        case 'denoise':
          ed.setTool('sharpen');
          void run.denoise();
          break;
        case 'face':
          ed.setTool('sharpen');
          void run.enhanceFace();
          break;
        case 'sharpen':
          ed.set('sharpenAmount', action.amount);
          ed.setTool('sharpen');
          void run.sharpen();
          break;
        case 'style':
          ed.set('stylePrompt', action.prompt);
          ed.set('styleStrength', action.strength);
          ed.setTool('style');
          void run.style();
          break;
      }
    },
    [ed, run],
  );
  const pickSuggestion = (s: AskSuggestion, go: boolean) => {
    if (go && !s.insert.endsWith(': ')) runAsk(s.insert);
    else setAsk(s.insert);
  };

  /* ── Keyboard ── */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e)) return;
      if (document.getElementById('fs-overlay-root')?.childElementCount) return;
      const ctrl = e.ctrlKey || e.metaKey;
      const k = e.key.toLowerCase();
      if (e.key === '?') {
        setShortcuts(true);
        return;
      }
      if (e.key === 'Escape') {
        if (ed.transform) {
          ed.cancelTransform();
          ed.setTool('move');
        } else if (ed.crop) ed.cancelCrop();
        else ed.clearSelection();
        return;
      }
      if (e.key === 'Enter') {
        if (ed.transform) ed.applyTransform();
        else if (ed.crop) ed.applyCrop();
        return;
      }
      if (ctrl) {
        if (k === 'z') {
          e.preventDefault();
          if (e.shiftKey) ed.redo();
          else ed.undo();
        } else if (k === 'y') {
          e.preventDefault();
          ed.redo();
        } else if (k === 's' && !e.altKey) {
          e.preventDefault();
          if (e.shiftKey) void saveCopy();
          else if (ed.imageId) setConfirmOver(true);
          else void saveCopy();
        } else if (k === 't' && e.shiftKey) {
          e.preventDefault();
          setSizeDialog(true);
        } else if (k === 't' && e.altKey) {
          e.preventDefault();
          ed.setTool('transform');
        } else if (k === 'j' && e.altKey) {
          e.preventDefault();
          ed.addLayer();
        } else if (k === 'i' && e.altKey) {
          e.preventDefault();
          ed.invertSelection();
        } else if (k === 'a') {
          e.preventDefault();
          ed.selectAll();
        } else if (k === 'd' && e.shiftKey) {
          e.preventDefault();
          ed.clearSelection();
        } else if (k === 'c' && ed.hasSelection()) {
          e.preventDefault();
          ed.copySelectionToLayer(false);
        } else if (k === 'x' && ed.hasSelection()) {
          e.preventDefault();
          ed.copySelectionToLayer(true);
        }
        return;
      }
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (ed.hasSelection()) {
          e.preventDefault();
          ed.deleteSelectedPixels();
        }
        return;
      }
      if (e.key === '[' || e.key === ']') {
        ed.set('brushSize', Math.max(1, Math.min(800, Math.round(ed.brushSize * (e.key === '[' ? 0.9 : 1.1)))));
        return;
      }
      const tool = TOOL_KEYS[k];
      if (tool && !e.altKey) ed.setTool(tool);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [ed, saveCopy]);

  /* ── Leave guard ── */
  useEffect(() => {
    const onLeave = (e: BeforeUnloadEvent) => {
      if (unsaved.current) e.preventDefault();
    };
    window.addEventListener('beforeunload', onLeave);
    return () => window.removeEventListener('beforeunload', onLeave);
  }, [ed]);

  const backHref = ed.imageId ? `/library?type=imagen&img=${encodeURIComponent(ed.imageId)}` : '/library/edit';

  if (state === 'error') {
    return (
      <div className="fs-screen">
        <EmptyState title={t('Could not open the image')} body={loadError} primaryAction={{ label: t('Back to the library'), onClick: () => navigate('/library?type=imagen') }} />
      </div>
    );
  }

  const layer = ed.active;
  const history = ed.historyLabels;

  return (
    <div className="fs-ed" data-testid="editor" data-loading={state === 'loading' || undefined} data-sheet={sheet ?? undefined}>
      <header className="fs-ed__top">
        <div className="fs-ed__top-left">
          <IconButton icon={ArrowLeft} label={t('Back to the library')} onClick={() => navigate(backHref)} />
          <div className="fs-ed__title">
            <input className="fs-ed__name" value={ed.draftName} aria-label={t('Name')} onChange={(e) => ed.set('draftName', e.target.value)} onBlur={() => ed.onPersist?.()} />
            <span className="fs-ed__dims">
              {ed.doc.width} × {ed.doc.height}
              {ed.dirty && <span className="fs-ed__dirty" title={t('Unsaved changes; a draft is kept automatically')} />}
            </span>
          </div>
          <div className="fs-ed__cluster">
            <IconButton icon={Undo2} label={t('Undo (Ctrl+Z)')} disabled={!ed.undoStack.length} onClick={() => ed.undo()} />
            <IconButton icon={Redo2} label={t('Redo (Ctrl+Shift+Z)')} disabled={!ed.redoStack.length} onClick={() => ed.redo()} />
            <Popover trigger={<IconButton icon={HistoryIcon} label={t('History')} />} className="fs-ed__history" testId="history">
              <ol className="fs-ed__history-list">
                {history.map((label, i) => (
                  <li key={`${i}-${label}`} data-now={i === history.length - 1 || undefined}>
                    <button type="button" onClick={() => ed.jumpHistory(i)}>
                      {t(label)}
                    </button>
                  </li>
                ))}
              </ol>
            </Popover>
          </div>
          <div className="fs-ed__cluster fs-ed__zoom">
            <IconButton icon={Minus} label={t('Zoom out')} size="sm" onClick={zoomApi.zoomOut} />
            <output aria-live="off">{Math.round(ed.zoom * 100)}%</output>
            <IconButton icon={Plus} label={t('Zoom in')} size="sm" onClick={zoomApi.zoomIn} />
            <Button size="sm" variant="ghost" label={t('Fit')} onClick={zoomApi.fit} />
            <Button size="sm" variant="ghost" label="1:1" title={t('Actual size')} onClick={zoomApi.actual} />
          </div>
        </div>

        <form
          className="fs-ed__ask"
          onSubmit={(e) => {
            e.preventDefault();
            if (askFocus && suggestions.length && ask && suggestions[askIndex]) pickSuggestion(suggestions[askIndex], true);
            else runAsk(ask);
          }}
        >
          <Sparkles size={14} aria-hidden="true" />
          <input
            value={ask}
            placeholder={t('Ask: remove the background, rotate left, sharpen…')}
            aria-label={t('Describe an edit')}
            onChange={(e) => {
              setAsk(e.target.value);
              setAskIndex(0);
            }}
            onFocus={() => setAskFocus(true)}
            onBlur={() => window.setTimeout(() => setAskFocus(false), 120)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                setAskIndex((i) => (e.key === 'ArrowDown' ? (i + 1) % suggestions.length : (i - 1 + suggestions.length) % suggestions.length));
              } else if (e.key === 'Tab' && suggestions[askIndex]) {
                e.preventDefault();
                pickSuggestion(suggestions[askIndex], false);
              } else if (e.key === 'Escape') (e.target as HTMLInputElement).blur();
            }}
          />
          {askFocus && suggestions.length > 0 && (
            <ul className="fs-ed__ask-list" role="listbox">
              {suggestions.map((s, i) => (
                <li key={s.insert} role="option" aria-selected={i === askIndex}>
                  <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => pickSuggestion(s, true)}>
                    <strong>{t(s.label)}</strong>
                    <span>{t(s.hint)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </form>

        <div className="fs-ed__top-right">
          <Menu
            align="end"
            trigger={<Button size="sm" variant="ghost" label={t('Image')} icon={ChevronDown} iconPosition="right" />}
            items={[
              { label: t('Canvas size…'), icon: Scaling, onSelect: () => setSizeDialog(true) },
              null,
              { label: t('Rotate 90° clockwise'), icon: RotateCw, onSelect: () => ed.rotate(90) },
              { label: t('Rotate 90° counter-clockwise'), icon: RotateCw, onSelect: () => ed.rotate(270) },
              { label: t('Rotate 180°'), icon: RotateCw, onSelect: () => ed.rotate(180) },
              { label: t('Flip horizontal'), icon: FlipHorizontal2, onSelect: () => ed.flip('h') },
              { label: t('Flip vertical'), icon: FlipVertical2, onSelect: () => ed.flip('v') },
              null,
              { label: t('Upscale…'), icon: Maximize2, onSelect: () => ed.setTool('upscale') },
            ]}
          />
          <Menu
            align="end"
            trigger={<Button size="sm" variant="ghost" label={t('Filter')} icon={ChevronDown} iconPosition="right" />}
            items={[
              { label: t('Gaussian blur…'), disabled: !layer, onSelect: () => setBlur('gaussian') },
              { label: t('Zoom blur…'), disabled: !layer, onSelect: () => setBlur('zoom') },
              { label: t('Motion blur…'), disabled: !layer, onSelect: () => setBlur('motion') },
              null,
              ...(['brightness-contrast', 'hue-saturation', 'levels', 'color-balance'] as AdjustmentType[]).map((type) => ({ label: `${t(adjustmentLabel(type))}…`, disabled: !layer, onSelect: () => layer && setAdjust({ layerId: layer.id, type }) })),
            ]}
          />
          <Menu
            align="end"
            trigger={<Button size="sm" variant="ghost" label={t('Import')} icon={ImagePlus} />}
            items={[
              { label: t('From a file…'), onSelect: () => fileRef.current?.click() },
              { label: t('From the clipboard'), onSelect: () => void importClipboard() },
              { label: t('From the library…'), onSelect: () => setLibraryPick(true) },
            ]}
          />
          <IconButton icon={Keyboard} label={t('Keyboard shortcuts (?)')} onClick={() => setShortcuts(true)} />
          <Menu
            align="end"
            trigger={<Button size="sm" variant="primary" label={saving ? t('Saving…') : t('Save')} icon={Save} loading={!!saving} />}
            items={[
              { label: t('Save over the original (Ctrl+S)'), disabled: !ed.imageId, onSelect: () => setConfirmOver(true) },
              { label: t('Save as a copy in the library (Ctrl+Shift+S)'), onSelect: () => void saveCopy() },
              { label: t('Download PNG'), onSelect: () => void downloadPng() },
              null,
              { label: t('Save project file (.json, keeps every layer)'), onSelect: saveProject },
              { label: t('Open a project file…'), onSelect: () => projectRef.current?.click() },
            ]}
          />
        </div>
      </header>

      <nav className="fs-ed__rail" aria-label={t('Tools')}>
        {TOOL_GROUPS.map((g) => (
          <div key={g.title} className="fs-ed__group" role="group" aria-label={t(g.title)}>
            {g.tools.map((tool) => (
              <button
                key={tool.id}
                type="button"
                className="fs-ed__tool"
                aria-pressed={ed.tool === tool.id}
                data-ai={tool.ai || undefined}
                title={`${t(TOOL_LABELS[tool.id])}${tool.key ? ` (${tool.key})` : ''}`}
                onClick={() => {
                  ed.setTool(tool.id);
                  setSheet('tool');
                }}
              >
                <tool.icon size={18} aria-hidden="true" />
                <span className="fs-ed__tool-label">{t(TOOL_LABELS[tool.id])}</span>
              </button>
            ))}
          </div>
        ))}
      </nav>

      <div className="fs-ed__stage">
        {state === 'loading' ? <Skeleton label={t('Loading the image')} height="100%" radius="preview" /> : <Stage ed={ed} version={version} onDropFiles={(files) => void importFiles(files)} />}
        {ed.busy && (
          <div className="fs-ed__busy" role="status">
            <span className="fs-ed__spinner" aria-hidden="true" />
            {t('Working…')}
          </div>
        )}
      </div>

      <aside className="fs-ed__inspector" aria-label={t('Inspector')}>
        <div className="fs-ed__sheet-tabs" role="tablist">
          <button type="button" role="tab" aria-selected={sheet !== 'layers'} onClick={() => setSheet('tool')}>
            {t(TOOL_LABELS[ed.tool])}
          </button>
          <button type="button" role="tab" aria-selected={sheet === 'layers'} onClick={() => setSheet('layers')}>
            {t('Layers')} · {ed.doc.layers.length}
          </button>
          <IconButton icon={X} label={t('Close')} size="sm" onClick={() => setSheet(null)} />
        </div>
        <div className="fs-ed__pane" data-pane="tool">
          <ToolPane ed={ed} models={models} run={run} rembgInstalled={rembgInstalled} onOpenCookbook={() => { window.location.href = '/?shell=legacy'; }} />
        </div>
        <div className="fs-ed__pane" data-pane="layers">
          <LayersPane ed={ed} version={version} onAdjust={(layerId, type, adjId) => setAdjust({ layerId, type, adjId })} />
        </div>
      </aside>

      <input ref={fileRef} type="file" accept="image/*" multiple hidden onChange={(e) => { const files = [...(e.target.files ?? [])]; e.target.value = ''; void importFiles(files); }} />
      <input ref={projectRef} type="file" accept="application/json,.json" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void loadProject(f); }} />

      <CanvasSizeDialog open={sizeDialog} onOpenChange={setSizeDialog} width={ed.doc.width} height={ed.doc.height} mode="resize" onApply={(w, h, anchor) => { ed.resizeCanvas(w, h, anchor); setSizeDialog(false); }} />
      <BlurDialog ed={ed} kind={blur} onClose={() => setBlur(null)} />
      <AdjustDialog ed={ed} target={adjust} onClose={() => setAdjust(null)} />
      <ShortcutsDialog open={shortcuts} onOpenChange={setShortcuts} />
      <LibraryPickDialog
        open={libraryPick}
        onOpenChange={setLibraryPick}
        onPick={(pick) => {
          setLibraryPick(false);
          void loadImage(pick.url).then((img) => {
            const c = makeCanvas(img.width, img.height);
            ctx2d(c).drawImage(img, 0, 0);
            ed.addLayerFromCanvas(pick.name.replace(/\.[^.]+$/, '') || t('Imported'), c, { x: Math.round((ed.doc.width - c.width) / 2), y: Math.round((ed.doc.height - c.height) / 2) });
            ed.setTool('move');
          }).catch((e: Error) => say(e.message, 'warn'));
        }}
      />
      <ConfirmOverDialog open={confirmOver} onOpenChange={setConfirmOver} name={ed.imageName} onConfirm={() => void saveOver()} onCopy={() => void saveCopy()} />

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <AlertTriangle size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}

function ConfirmOverDialog({ open, onOpenChange, name, onConfirm, onCopy }: { open: boolean; onOpenChange: (o: boolean) => void; name: string; onConfirm: () => void; onCopy: () => void }) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t('Save over the original?')}
      description={t('{name} in the library is replaced with the flattened result. The previous pixels are gone; layers stay only in the draft and in project files.', { name: name || t('The image') })}
      testId="save-over"
      footer={
        <>
          <Button variant="ghost" label={t('Cancel')} onClick={() => onOpenChange(false)} />
          <Button
            variant="secondary"
            label={t('Save as a copy instead')}
            onClick={() => {
              onOpenChange(false);
              onCopy();
            }}
          />
          <Button variant="danger-solid" label={t('Replace the original')} onClick={onConfirm} />
        </>
      }
    />
  );
}
