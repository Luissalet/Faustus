import { Crosshair, Eye, EyeOff, Layers as LayersIcon, RefreshCw, Scissors, Sparkles, Trash2, Wand2, X } from 'lucide-react';
import { Button } from '../../../components';
import type { ImageModelOption } from '../../../adapters/imageTools';
import { brushToSlider, sliderToBrush } from '../../../lib/pixel';
import { t } from '../../../i18n';
import type { PixelEditor, SelectMode, StrokeSettings, Tool } from './engine';
import { dec, ModelSelect, pct, px, Row, Section, Segmented, signedPx, Slider, TextField } from './controls';
import type { Runner } from './runner';

export const TOOL_LABELS: Record<Tool, string> = {
  move: 'Move',
  crop: 'Crop',
  transform: 'Transform',
  brush: 'Brush',
  eraser: 'Eraser',
  clone: 'Clone stamp',
  lasso: 'Lasso',
  wand: 'Magic wand',
  sam: 'Smart select',
  inpaint: 'Inpaint',
  rembg: 'Remove background',
  sharpen: 'Sharpen',
  harmonize: 'Harmonize',
  style: 'Style',
  upscale: 'Upscale',
};

interface Props {
  ed: PixelEditor;
  models: ImageModelOption[];
  run: Runner;
  rembgInstalled: boolean | null;
  onOpenCookbook: (what: string) => void;
}

export function ToolPane({ ed, models, run, rembgInstalled, onOpenCookbook }: Props) {
  const busy = ed.busy;
  const modeOptions: { value: SelectMode; label: string; title: string }[] = [
    { value: 'replace', label: t('New'), title: t('Each pick replaces the selection') },
    { value: 'add', label: t('Add'), title: t('Add to the selection (Shift)') },
    { value: 'subtract', label: t('Subtract'), title: t('Take away from the selection (Alt)') },
  ];

  const brushSize = (
    <Slider id="ed-size" label={t('Size')} value={brushToSlider(ed.brushSize)} min={0} max={1000} onChange={(v) => ed.set('brushSize', sliderToBrush(v))} format={() => px(ed.brushSize)} hint={t('Brush diameter. [ and ] change it by 10%')} />
  );

  const strokeSliders = (key: 'brush' | 'eraser' | 'clone', s: StrokeSettings) => (
    <>
      <Slider id={`ed-${key}-opacity`} label={t('Opacity')} value={s.opacity} min={10} max={100} onChange={(v) => ed.set(key, { ...s, opacity: v })} format={pct} />
      <Slider id={`ed-${key}-flow`} label={t('Flow')} value={s.flow} min={5} max={100} onChange={(v) => ed.set(key, { ...s, flow: v })} format={pct} />
      <Slider id={`ed-${key}-softness`} label={t('Softness')} value={s.softness} min={0} max={300} onChange={(v) => ed.set(key, { ...s, softness: v })} format={(v) => pct(v / 3)} hint={t('Soft edge: the stroke fades out at its perimeter')} />
    </>
  );

  const selectionActions = (
    <Row wrap>
      <Button size="sm" label={t('Invert')} icon={RefreshCw} title={t('Invert the selection (Ctrl+Alt+I)')} disabled={!ed.hasSelection()} onClick={() => ed.invertSelection()} />
      <Button size="sm" label={t('Copy to layer')} icon={LayersIcon} title={t('Copy the selected pixels to a new layer (Ctrl+C)')} disabled={!ed.hasSelection()} onClick={() => ed.copySelectionToLayer(false)} />
      <Button size="sm" label={t('To mask')} icon={Scissors} title={t('Add the selection to the inpaint mask')} disabled={!ed.hasSelection()} onClick={() => ed.selectionToMask()} />
      <Button size="sm" variant="danger" label={t('Delete pixels')} icon={Trash2} title={t('Delete the selected pixels from the layer (Delete)')} disabled={!ed.hasSelection()} onClick={() => ed.deleteSelectedPixels()} />
      <Button size="sm" variant="ghost" label={t('Clear')} icon={X} disabled={!ed.hasSelection() && !ed.lassoPoints.length} onClick={() => ed.clearSelection()} />
    </Row>
  );

  const refine = (
    <>
      <Slider id="ed-sel-feather" label={t('Feather')} value={ed.selFeather} min={0} max={200} onChange={(v) => ed.refineSelection(v, ed.selGrow)} format={px} hint={t('Soften the selection edge')} />
      <Slider id="ed-sel-grow" label={t('Edge')} value={ed.selGrow} min={-40} max={40} onChange={(v) => ed.refineSelection(ed.selFeather, v)} format={signedPx} hint={t('Expand (+) or contract (−) the selection')} />
    </>
  );

  const visibility = (
    <Button size="sm" variant="ghost" icon={ed.selectionVisible ? Eye : EyeOff} label={ed.selectionVisible ? t('Hide overlay') : t('Show overlay')} onClick={() => ed.set('selectionVisible', !ed.selectionVisible)} />
  );

  switch (ed.tool) {
    case 'move':
      return (
        <Section title={t('Move')} help={t('Drag the active layer. It snaps to the canvas and other layers; hold Ctrl to move freely.')}>
          <Row wrap>
            <Button size="sm" label={t('Center on canvas')} onClick={() => centerActive(ed)} disabled={!ed.active} />
            <Button size="sm" label={t('Free transform')} title={t('Scale, rotate and flip the layer (Ctrl+Alt+T)')} onClick={() => ed.setTool('transform')} disabled={!ed.active} />
          </Row>
        </Section>
      );
    case 'crop':
      return (
        <Section title={t('Crop')} help={t('Drag a rectangle, move it by its inside, then apply. Shift keeps it square.')}>
          {ed.crop && (
            <p className="fs-ed__facts">
              {Math.round(ed.crop.w)} × {Math.round(ed.crop.h)} px
            </p>
          )}
          <Row>
            <Button size="sm" variant="primary" label={t('Apply crop')} title={t('Enter')} disabled={!ed.crop} onClick={() => ed.applyCrop()} />
            <Button size="sm" variant="ghost" label={t('Cancel')} title={t('Escape')} disabled={!ed.crop} onClick={() => ed.cancelCrop()} />
          </Row>
        </Section>
      );
    case 'transform': {
      const tr = ed.transform;
      return (
        <Section title={t('Transform')} help={t('Drag the corners to scale and the top handle to rotate. Shift snaps the angle to 15°.')}>
          {tr && (
            <>
              <Row>
                <div className="fs-ed__field">
                  <label htmlFor="ed-tr-w">{t('Width')}</label>
                  <input id="ed-tr-w" className="fs-field" type="number" min={1} value={tr.width} onChange={(e) => ed.updateTransform({ width: Math.max(1, Number(e.target.value)) })} />
                </div>
                <div className="fs-ed__field">
                  <label htmlFor="ed-tr-h">{t('Height')}</label>
                  <input id="ed-tr-h" className="fs-field" type="number" min={1} value={tr.height} onChange={(e) => ed.updateTransform({ height: Math.max(1, Number(e.target.value)) })} />
                </div>
              </Row>
              <label className="fs-ed__check">
                <input type="checkbox" checked={tr.aspectLock} onChange={(e) => ed.updateTransform({ aspectLock: e.target.checked })} /> {t('Keep proportions')}
              </label>
              <Slider id="ed-tr-rot" label={t('Rotation')} value={tr.rotation} min={-180} max={180} step={0.5} onChange={(v) => ed.updateTransform({ rotation: v })} format={(v) => `${v}°`} />
              <Row wrap>
                <Button size="sm" label={t('Flip horizontal')} onClick={() => ed.updateTransform({ flipH: !tr.flipH })} />
                <Button size="sm" label={t('Flip vertical')} onClick={() => ed.updateTransform({ flipV: !tr.flipV })} />
                <Button size="sm" variant="ghost" label={t('Reset')} onClick={() => ed.updateTransform({ width: tr.origW, height: tr.origH, rotation: 0, flipH: false, flipV: false })} />
              </Row>
              <Row>
                <Button size="sm" variant="primary" label={t('Apply')} title={t('Enter')} onClick={() => ed.applyTransform()} />
                <Button size="sm" variant="ghost" label={t('Cancel')} title={t('Escape')} onClick={() => { ed.cancelTransform(); ed.setTool('move'); }} />
              </Row>
            </>
          )}
          {!tr && <p className="fs-ed__help">{t('Pick a layer to transform.')}</p>}
        </Section>
      );
    }
    case 'brush':
      return (
        <Section title={t('Brush')} help={ed.active && ed.active.activeMaskId ? t('A mask is active: the brush paints the mask, not the pixels.') : undefined}>
          <div className="fs-ed__field fs-ed__color">
            <label htmlFor="ed-color">{t('Color')}</label>
            <input id="ed-color" type="color" value={ed.color} onChange={(e) => ed.set('color', e.target.value)} />
          </div>
          {brushSize}
          {strokeSliders('brush', ed.brush)}
        </Section>
      );
    case 'eraser':
      return (
        <Section title={t('Eraser')}>
          {brushSize}
          {strokeSliders('eraser', ed.eraser)}
        </Section>
      );
    case 'clone':
      return (
        <Section title={t('Clone stamp')} help={t('Alt-click (or right-click) to set the source, then paint elsewhere. The source follows the brush.')}>
          <p className="fs-ed__facts">
            <Crosshair size={12} aria-hidden="true" /> {ed.cloneSource ? t('Source at {x}, {y}', { x: Math.round(ed.cloneSource.x), y: Math.round(ed.cloneSource.y) }) : t('No source yet')}
          </p>
          {brushSize}
          {strokeSliders('clone', ed.clone)}
        </Section>
      );
    case 'lasso':
      return (
        <Section title={t('Lasso')} help={t('Draw around what you want. Escape cancels.')} aside={visibility}>
          <Segmented label={t('Combine')} value={ed.selectMode} options={modeOptions} onChange={(v) => ed.set('selectMode', v)} />
          {refine}
          {selectionActions}
        </Section>
      );
    case 'wand':
      return (
        <Section title={t('Magic wand')} help={t('Click a region to select the similar pixels around it. Shift adds, Alt subtracts.')} aside={visibility}>
          <Segmented label={t('Combine')} value={ed.selectMode} options={modeOptions} onChange={(v) => ed.set('selectMode', v)} />
          <Slider
            id="ed-wand-tol"
            label={t('Tolerance')}
            value={ed.wandTolerance}
            min={0}
            max={100}
            onChange={(v) => {
              ed.set('wandTolerance', v);
              if (ed.wandLive) ed.retuneWand();
            }}
            onCommit={() => ed.retuneWand()}
          />
          <label className="fs-ed__check">
            <input type="checkbox" checked={ed.wandLive} onChange={(e) => ed.set('wandLive', e.target.checked)} /> {t('Retune while dragging')}
          </label>
          {refine}
          {selectionActions}
        </Section>
      );
    case 'sam':
      return (
        <Section title={t('Smart select')} help={t('Click the object (Alt-click marks what to leave out), or name it and press Find. A segmentation model draws the mask.')} aside={visibility}>
          <Segmented label={t('Combine')} value={ed.selectMode} options={modeOptions} onChange={(v) => ed.set('selectMode', v)} />
          <TextField id="ed-sam-q" label={t('Object')} value={ed.samQuery} placeholder={t('the cup, the dog…')} onChange={(v) => ed.set('samQuery', v)} onEnter={() => void run.samFind()} />
          <Row>
            <Button size="sm" variant="primary" icon={Sparkles} label={busy === 'sam' ? t('Finding…') : t('Find')} loading={busy === 'sam'} disabled={!!busy} onClick={() => void run.samFind()} />
            <Button size="sm" variant="ghost" label={t('Clear points')} onClick={() => ed.clearSelection()} />
          </Row>
          {refine}
          {selectionActions}
        </Section>
      );
    case 'inpaint': {
      const last = ed.doc.layers.find((l) => l.id === ed.lastInpaintLayerId);
      return (
        <>
          <Section title={t('Mask')} help={t('Brush the area to redraw. Paint adds, Erase takes away; Ctrl+Alt flips it for one stroke.')} aside={<Button size="sm" variant="ghost" icon={ed.maskVisible ? Eye : EyeOff} label={ed.maskVisible ? t('Hide mask') : t('Show mask')} onClick={() => ed.setMaskVisible(!ed.maskVisible)} />}>
            <Segmented label={t('Mask brush')} value={ed.inpaintErase ? 'erase' : 'paint'} options={[{ value: 'paint', label: t('Paint') }, { value: 'erase', label: t('Erase') }]} onChange={(v) => ed.set('inpaintErase', v === 'erase')} />
            {brushSize}
            <Row wrap>
              <div className="fs-ed__field fs-ed__color">
                <label htmlFor="ed-mask-tint">{t('Tint')}</label>
                <input id="ed-mask-tint" type="color" value={ed.maskTint} onChange={(e) => ed.set('maskTint', e.target.value)} title={t('Only the preview colour; the model always sees a hard mask')} />
              </div>
              <Button size="sm" label={t('Invert')} icon={RefreshCw} onClick={() => ed.invertMasks()} />
              <Button size="sm" variant="ghost" label={t('Clear')} icon={X} onClick={() => ed.clearMasks()} />
            </Row>
          </Section>
          <Section title={t('Generate')}>
            <TextField id="ed-inpaint-prompt" label={t('Prompt')} value={ed.inpaintPrompt} placeholder={t('What should fill the masked area')} onChange={(v) => ed.set('inpaintPrompt', v)} onEnter={() => void run.inpaint('generate')} />
            <ModelSelect id="ed-inpaint-model" value={ed.inpaintModel} models={models} kind="inpaint" onChange={(v) => ed.set('inpaintModel', v)} />
            <Slider id="ed-strength" label={t('Strength')} value={ed.inpaintStrength} min={10} max={100} onChange={(v) => ed.set('inpaintStrength', v)} format={dec} hint={t('How much is redrawn inside the mask. 0.9–1 replaces an object, 0.6–0.8 changes material or colour, 0.3–0.5 retouches.')} />
            <Row wrap>
              <Button size="sm" variant="primary" icon={Sparkles} label={busy === 'generate' ? t('Generating…') : t('Generate')} loading={busy === 'generate'} disabled={!!busy} title={t('Fill the masked area with what the prompt describes')} onClick={() => void run.inpaint('generate')} />
              <Button size="sm" icon={Sparkles} label={busy === 'remove' ? t('Removing…') : t('Remove')} loading={busy === 'remove'} disabled={!!busy} title={t('Erase the masked content and continue the background; the prompt is ignored')} onClick={() => void run.inpaint('remove')} />
              <Button size="sm" icon={Sparkles} label={busy === 'outpaint' ? t('Outpainting…') : t('Outpaint')} loading={busy === 'outpaint'} disabled={!!busy} title={t('Fill the empty, transparent parts of the canvas; the brush mask is ignored')} onClick={() => void run.inpaint('outpaint')} />
            </Row>
          </Section>
          <Section title={t('After generating')} help={last ? t('Live edge trimming of the last result: {name}', { name: last.name }) : t('Available once a result lands.')}>
            {last && (
              <>
                <Slider id="ed-inpaint-feather" label={t('Edge feather')} value={ed.inpaintFeather} min={0} max={200} onChange={(v) => ed.tuneInpaintEdge(v, ed.inpaintEdge)} format={px} />
                <Slider id="ed-inpaint-edge" label={t('Edge')} value={ed.inpaintEdge} min={-(last.inpaintSource?.padPx ?? 40)} max={last.inpaintSource?.padPx ?? 40} onChange={(v) => ed.tuneInpaintEdge(ed.inpaintFeather, v)} format={signedPx} hint={t('Grow (+) or shrink (−) the result into the buffer generated around the brush')} />
                <Button size="sm" icon={Wand2} label={busy === 'match' ? t('Matching…') : t('Match colour to surroundings')} loading={busy === 'match'} disabled={!!busy} onClick={() => void run.autoMatch()} />
              </>
            )}
          </Section>
        </>
      );
    }
    case 'rembg': {
      const last = ed.doc.layers.find((l) => l.id === ed.lastEdgeLayerId);
      return (
        <>
          <Section title={t('Remove background')} help={t('A model keeps the foreground it recognises (people, products, animals). A lasso or wand selection narrows where it looks.')}>
            {rembgInstalled === false ? (
              <div className="fs-notice" data-tone="warning">
                <p>{t('rembg is not installed on this server.')}</p>
                <Button size="sm" label={t('Install from Cookbook')} onClick={() => onOpenCookbook('rembg')} />
              </div>
            ) : (
              <Button variant="primary" icon={Sparkles} label={busy === 'rembg' ? t('Removing…') : t('Remove background')} loading={busy === 'rembg'} disabled={!!busy} onClick={() => void run.removeBg()} />
            )}
          </Section>
          <Section title={t('Edge cleanup')} help={last ? t('Live on the last cut-out: {name}', { name: last.name }) : t('Available once a cut-out lands.')}>
            {last && (
              <>
                <Slider id="ed-edge-feather" label={t('Feather')} value={ed.edgeFeather} min={0} max={20} onChange={(v) => ed.tuneEdge(v, ed.edgeGrow)} format={px} />
                <Slider id="ed-edge-grow" label={t('Edge')} value={ed.edgeGrow} min={-10} max={10} onChange={(v) => ed.tuneEdge(ed.edgeFeather, v)} format={signedPx} />
              </>
            )}
          </Section>
        </>
      );
    }
    case 'sharpen':
      return (
        <Section title={t('Enhance')} help={t('Each result lands on its own layer, so nothing is lost.')}>
          <Slider id="ed-sharpen" label={t('Sharpen amount')} value={ed.sharpenAmount} min={10} max={100} onChange={(v) => ed.set('sharpenAmount', v)} format={pct} />
          <Row wrap>
            <Button size="sm" variant="primary" icon={Sparkles} label={busy === 'sharpen' ? t('Sharpening…') : t('Sharpen')} loading={busy === 'sharpen'} disabled={!!busy} onClick={() => void run.sharpen()} />
            <Button size="sm" icon={Sparkles} label={busy === 'denoise' ? t('Denoising…') : t('Denoise')} loading={busy === 'denoise'} disabled={!!busy} title={t('Reduce grain and noise')} onClick={() => void run.denoise()} />
            <Button size="sm" icon={Sparkles} label={busy === 'face' ? t('Enhancing…') : t('Enhance faces')} loading={busy === 'face'} disabled={!!busy} title={t('Restore portrait and skin detail')} onClick={() => void run.enhanceFace()} />
          </Row>
        </Section>
      );
    case 'harmonize':
      return (
        <Section title={t('Harmonize')} help={t('Blends the layers above the base photo into it: colour match shifts their lighting and tone; seam fix inpaints the cut-out edge (needs a local inpaint model).')}>
          <ModelSelect id="ed-harm-model" value={ed.harmonizeModel} models={models} kind="inpaint" onChange={(v) => ed.set('harmonizeModel', v)} />
          <TextField id="ed-harm-prompt" label={t('Prompt (seam fix only)')} value={ed.harmonizePrompt} placeholder={t('photorealistic, natural lighting, seamless blend')} onChange={(v) => ed.set('harmonizePrompt', v)} />
          <Slider id="ed-harm-color" label={t('Colour match')} value={ed.harmonizeColor} min={0} max={100} onChange={(v) => ed.set('harmonizeColor', v)} format={dec} />
          <Slider id="ed-harm-seam" label={t('Seam fix')} value={ed.harmonizeSeam} min={0} max={100} onChange={(v) => ed.set('harmonizeSeam', v)} format={dec} />
          <Button variant="primary" icon={Sparkles} label={busy === 'harmonize' ? t('Harmonizing…') : t('Harmonize')} loading={busy === 'harmonize'} disabled={!!busy || ed.doc.layers.filter((l) => l.visible).length < 2} onClick={() => void run.harmonize()} />
        </Section>
      );
    case 'style':
      return (
        <Section title={t('Style')} help={t('Redraws the whole picture in a style (img2img). Needs a running diffusion model.')}>
          <ModelSelect id="ed-style-model" value={ed.styleModel} models={models} kind="generate" onChange={(v) => ed.set('styleModel', v)} />
          <TextField id="ed-style-prompt" label={t('Style prompt')} value={ed.stylePrompt} placeholder={t('oil painting, impressionist…')} onChange={(v) => ed.set('stylePrompt', v)} onEnter={() => void run.style()} />
          <Slider id="ed-style-strength" label={t('Strength')} value={ed.styleStrength} min={10} max={90} onChange={(v) => ed.set('styleStrength', v)} format={dec} />
          <Button variant="primary" icon={Sparkles} label={busy === 'style' ? t('Applying…') : t('Apply style')} loading={busy === 'style'} disabled={!!busy} onClick={() => void run.style()} />
        </Section>
      );
    case 'upscale':
      return (
        <Section title={t('Upscale')} help={t('Resample every layer (fast), or let the local model rebuild detail at 2× (slower, better).')}>
          <p className="fs-ed__facts">
            {ed.doc.width} × {ed.doc.height} px
          </p>
          <Row wrap>
            <Button size="sm" label={t('2× resample')} onClick={() => run.upscaleLocal(2)} />
            <Button size="sm" label={t('4× resample')} onClick={() => run.upscaleLocal(4)} />
            <Button size="sm" variant="primary" icon={Sparkles} label={busy === 'upscale' ? t('Upscaling…') : t('AI upscale 2×')} loading={busy === 'upscale'} disabled={!!busy} onClick={() => void run.upscaleAi()} />
          </Row>
        </Section>
      );
  }
}

function centerActive(ed: PixelEditor): void {
  const layer = ed.active;
  if (!layer) return;
  ed.saveState('Center layer');
  layer.offset = { x: Math.round((ed.doc.width - layer.canvas.width) / 2), y: Math.round((ed.doc.height - layer.canvas.height) / 2) };
  ed.composite();
  ed.notify();
}
